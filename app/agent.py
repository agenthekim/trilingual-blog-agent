"""Agent composition for the trilingual tech-blog studio.

Structure (see README for the diagram):

    trilingual_blog_agent              coordinator, FAST model (Flash tier), owns the tools
    ├── seed_editor_agent              REASONING model (Pro-class tier), revises the draft
    ├── localization_pipeline          SequentialAgent, gated on a finalized seed
    │   ├── localization_fanout        ParallelAgent: ko / en / ja localizers + titles
    │   ├── localization_review_loop   LoopAgent: check -> critic -> gate -> refine
    │   └── localization_report_agent  saves versions, reports to the user
    └── slide_planning_pipeline        SequentialAgent, gated on a presentation brief
        ├── slide_review_loop          LoopAgent: planner -> timing gate
        └── slide_report_agent         writes the plan file, reports to the user

The coordinator runs one stage per user request; the user reviews the result
before the next stage starts. Model choice per agent comes from
``app.models.MODEL_ROUTES``: the FAST model is the Flash tier, the REASONING
model the Pro-class tier.
"""

from __future__ import annotations

import os
from typing import Any

from google.adk.agents import Agent, LoopAgent, ParallelAgent, SequentialAgent
from google.adk.agents.context_cache_config import ContextCacheConfig
from google.adk.apps import App, ResumabilityConfig
from google.adk.apps.app import EventsCompactionConfig
from google.adk.apps.llm_event_summarizer import LlmEventSummarizer
from google.adk.tools.preload_memory_tool import PreloadMemoryTool

from app import prompts, state
from app.memory import schedule_memory_consolidation
from app.models import model_for
from app.observability import ObservabilityPlugin
from app.plugins import ContentPolicyPlugin, ToolFailurePlugin
from app.schemas import LocalizedTitles, ReviewVerdict, SlideDeckPlan
from app.settings import LANGUAGES
from app.tools.articles import (
    fetch_published_article_markdown,
    finalize_seed_article,
    load_markdown_draft_file,
    read_article_post,
    replace_passage_in_article_post,
    save_seed_article_revision,
    set_article_post_title,
)
from app.tools.publish import (
    export_naver_blog_paste_ready_html,
    finalize_localized_post,
    publish_devto_article_draft,
    publish_qiita_article_draft,
    record_manually_posted_url,
    verify_published_article_urls,
)
from app.tools.slides import (
    check_slide_script_timing,
    compile_artifact_manifest_markdown,
    create_google_slides_deck_with_speaker_notes,
    export_slide_plan_markdown,
    request_approval_to_create_slide_decks,
    set_presentation_brief,
)
from app.workflow_agents import (
    BriefGate,
    FidelityCheckAgent,
    LanguageGate,
    LocalizationReportAgent,
    ReviewGate,
    SeedGate,
    SlideReportAgent,
    SlideTimingGate,
)

MAX_REVIEW_ROUNDS = 2


# --- Stage 1: seed revision ------------------------------------------------
seed_editor_agent = Agent(
    name="seed_editor_agent",
    description="Revises the seed article draft with the author until it is final.",
    model=model_for("revise_seed"),
    instruction=prompts.seed_editor_instruction,
    tools=[save_seed_article_revision, finalize_seed_article],
)


# --- Stage 2: parallel localization with a self-evaluation loop -------------
def _localizer(language: str) -> LanguageGate:
    return LanguageGate(
        name=f"localize_{language}_gate",
        language=language,
        mode="localize",
        sub_agents=[
            Agent(
                name=f"localizer_{language}_agent",
                description=f"Localizes the seed article into '{language}'.",
                model=model_for("localize"),
                instruction=prompts.localizer_instruction(language),
                include_contents="none",
                output_key=state.localized_key(language),
            )
        ],
    )


def _refiner(language: str) -> LanguageGate:
    return LanguageGate(
        name=f"refine_{language}_gate",
        language=language,
        mode="refine",
        sub_agents=[
            Agent(
                name=f"refiner_{language}_agent",
                description=f"Fixes review findings in the '{language}' localization.",
                model=model_for("refine_localization"),
                instruction=prompts.refiner_instruction(language),
                include_contents="none",
                output_key=state.localized_key(language),
            )
        ],
    )


title_localizer_agent = Agent(
    name="title_localizer_agent",
    description="Writes the article title in each language.",
    model=model_for("localize_titles"),
    instruction=prompts.title_localizer_instruction,
    include_contents="none",
    output_schema=LocalizedTitles,
    output_key=state.LOCALIZED_TITLES,
)

localization_fanout = ParallelAgent(
    name="localization_fanout",
    description="Writes every target-language version and the titles at the same time.",
    sub_agents=[
        *[_localizer(language) for language in LANGUAGES],
        title_localizer_agent,
    ],
)

localization_critic_agent = Agent(
    name="localization_critic_agent",
    description="Grades each localization against the source article.",
    model=model_for("critique_localization"),
    instruction=prompts.critic_instruction,
    include_contents="none",
    output_schema=ReviewVerdict,
    output_key=state.REVIEW_VERDICT,
)

localization_review_loop = LoopAgent(
    name="localization_review_loop",
    description="Self-evaluation: structure check, critic, then targeted fixes.",
    max_iterations=MAX_REVIEW_ROUNDS,
    sub_agents=[
        FidelityCheckAgent(name="fidelity_check_agent"),
        localization_critic_agent,
        ReviewGate(name="review_gate"),
        ParallelAgent(
            name="refinement_fanout",
            description="Fixes only the languages that failed review, in parallel.",
            sub_agents=[_refiner(language) for language in LANGUAGES],
        ),
    ],
)

localization_pipeline = SequentialAgent(
    name="localization_pipeline",
    description=(
        "Stage 2. Localizes the finalized seed article into the other two "
        "languages in parallel, reviews them against the seed, and reports. "
        "Transfer here when the user asks to localize or translate."
    ),
    sub_agents=[
        SeedGate(
            name="seed_gate",
            sub_agents=[
                SequentialAgent(
                    name="localization_steps",
                    sub_agents=[
                        localization_fanout,
                        localization_review_loop,
                        LocalizationReportAgent(name="localization_report_agent"),
                    ],
                )
            ],
        )
    ],
)


# --- Stage 4: slide planning with a timing check ----------------------------
def build_slide_planning_pipeline(planner_model: Any = None) -> SequentialAgent:
    """One instance of the slide planning pipeline.

    An agent has one parent, so each place that runs the pipeline gets an
    instance of its own: the coordinator, which a chat user transfers to, and
    ``slide_planning_app``, which the page and the script run in a working
    session per language (SLD-5). ``planner_model`` replaces the planner's
    model in tests; the route in ``app.models`` is used otherwise.
    """
    slide_plan_agent = Agent(
        name="slide_plan_agent",
        description="Plans the slides and a one-minute speaker script per slide.",
        model=planner_model or model_for("plan_slides"),
        instruction=prompts.slide_planner_instruction,
        include_contents="none",
        output_schema=SlideDeckPlan,
        output_key=state.SLIDE_DECK_PLAN,
    )
    return SequentialAgent(
        name="slide_planning_pipeline",
        description=(
            "Stage 4. Plans a slide deck with a one-minute speaker script per "
            "slide from the article and checks its timing. Transfer here after "
            "set_presentation_brief, or when the user asks to redo the slides."
        ),
        sub_agents=[
            BriefGate(
                name="brief_gate",
                sub_agents=[
                    SequentialAgent(
                        name="slide_planning_steps",
                        sub_agents=[
                            LoopAgent(
                                name="slide_review_loop",
                                description="Self-evaluation: plan, then check "
                                "the one-minute rule.",
                                max_iterations=MAX_REVIEW_ROUNDS,
                                sub_agents=[
                                    slide_plan_agent,
                                    SlideTimingGate(name="slide_timing_gate"),
                                ],
                            ),
                            SlideReportAgent(name="slide_report_agent"),
                        ],
                    )
                ],
            )
        ],
    )


slide_planning_pipeline = build_slide_planning_pipeline()


# --- Coordinator -----------------------------------------------------------
root_agent = Agent(
    # Keep in sync with create_params.root_agent_name in agents-cli-manifest.yaml.
    name="trilingual_blog_agent",
    description="Coordinates the trilingual tech-blog workflow, one stage at a time.",
    model=model_for("coordinate"),
    instruction=prompts.COORDINATOR_INSTRUCTION,
    sub_agents=[seed_editor_agent, localization_pipeline, slide_planning_pipeline],
    tools=[
        fetch_published_article_markdown,
        load_markdown_draft_file,
        save_seed_article_revision,
        finalize_seed_article,
        read_article_post,
        set_article_post_title,
        replace_passage_in_article_post,
        finalize_localized_post,
        export_naver_blog_paste_ready_html,
        publish_qiita_article_draft,
        publish_devto_article_draft,
        record_manually_posted_url,
        verify_published_article_urls,
        set_presentation_brief,
        request_approval_to_create_slide_decks,
        check_slide_script_timing,
        export_slide_plan_markdown,
        create_google_slides_deck_with_speaker_notes,
        compile_artifact_manifest_markdown,
        PreloadMemoryTool(),
    ],
    after_agent_callback=schedule_memory_consolidation,
)

app = App(
    root_agent=root_agent,
    # Must equal the agent directory name; evaluation infers it from the path.
    name="app",
    # In this order: the policy may refuse a call, the log records what
    # happened, and only then is an exception answered with a guided error.
    plugins=[ContentPolicyPlugin(), ObservabilityPlugin(), ToolFailurePlugin()],
    # Long revision sessions carry whole articles in history. Once the prompt
    # passes the token threshold, older events are summarised by the fast model
    # and only the most recent events are kept verbatim.
    events_compaction_config=EventsCompactionConfig(
        token_threshold=int(os.environ.get("COMPACTION_TOKEN_THRESHOLD", "60000")),
        event_retention_size=int(os.environ.get("COMPACTION_RETAIN_EVENTS", "8")),
        summarizer=LlmEventSummarizer(llm=model_for("summarize_history")),
    ),
    # The coordinator's long, stable instruction and tool declarations are
    # cached server-side instead of being re-sent on every turn.
    context_cache_config=ContextCacheConfig(
        min_tokens=4096, ttl_seconds=1800, cache_intervals=10
    ),
    # Required for publish tools to pause for human approval and resume.
    resumability_config=ResumabilityConfig(is_resumable=True),
)


def build_slide_planning_app(planner_model: Any = None) -> App:
    """The slide planning pipeline as the root of an application of its own.

    The page and the script plan each language in a working session of this
    application, so a planning run never writes to the record session of a
    run while a person works on the posts (RUN-2). Same plugins as ``app``:
    the content policy, the intent and outcome log and the guided error for
    a tool that raises apply here too.
    """
    return App(
        root_agent=build_slide_planning_pipeline(planner_model),
        name="slide_planning",
        plugins=[ContentPolicyPlugin(), ObservabilityPlugin(), ToolFailurePlugin()],
    )


slide_planning_app = build_slide_planning_app()
