# trilingual-blog-agent

An ADK multi-agent that takes one technical article and carries it through the
rest of a writer's workflow: localize it into the other two of Korean, English
and Japanese, prepare a draft on each language's platform, and plan a talk with
a timed speaker script.

Built with [Agent Development Kit](https://google.github.io/adk-docs/) and
scaffolded with `agents-cli` (Agent Runtime target).

What the system does is written down in [docs/SPEC.md](docs/SPEC.md)
(Korean companion: [docs/kr-SPEC.md](docs/kr-SPEC.md)): invariants, numbered
requirements, and for each requirement the unit tests that check it. A test
fails when the two drift apart. This README is the guide; the specification is
the reference.

## Problem and solution

Writing a technical post once is work enough. Reaching Korean, English and
Japanese readers means translating it twice without breaking code blocks or
links, re-formatting it for three platforms, and then turning it into slides
when a talk is requested. Most of that is mechanical, and the mechanical parts
are where mistakes creep in: a dropped code block, a changed URL, a slide
script that runs three minutes.

The agent does the mechanical work and leaves every decision that leaves the
machine to a human:

| Stage | What the agent does | Human checkpoint |
| :--- | :--- | :--- |
| 1. Source document | Stores a Markdown draft exactly as handed in, or reads an already published post from Qiita, DEV Community or Naver Blog. In a chat it can also revise a draft with the author. | Author hands in the document (in a chat: says when it is final) |
| 2. Localize | Writes the two missing languages and their titles in parallel, checks structure against the seed in code, has a critic model review meaning, fixes only what failed. | Author reviews the files and titles |
| 3. Publish | Exports paste-ready HTML for Naver Blog (ko). Sends a post to Qiita (ja) or DEV Community (en) through the API. Verifies the resulting URLs. | Author reviews, edits and finalizes each post (title and text). Approval before every write; the post is a non-public draft unless the person approving asks for a public post in that approval. The model can choose neither the title nor the visibility |
| 4. Present | Plans one slide per minute with a speaker script, checks estimated speaking time, re-plans once if it fails. The deck opens with a title slide (greeting, title and what it means, in its speaker notes), follows with an agenda, ends with a conclusion, next steps on their own slide when the article names any, and a thank-you slide. Writes the plan and an artifact manifest. | Author reviews the plan |

Language to platform mapping: Korean → Naver Blog, English → DEV Community,
Japanese → Qiita. A post that was fetched from a platform is never written back
to that platform.

Whether a post may be finalized or sent is decided in one place,
`app/policy.py`, which the tools call. The rule is therefore the same on the
web page, in the command-line script and in a chat with the agent.

## Architecture

```mermaid
flowchart TD
    U[Author] --> C[trilingual_blog_agent<br/>coordinator, fast model]
    C --> SE[seed_editor_agent<br/>reasoning model]
    C --> LP[localization_pipeline]
    C --> SP[slide_planning_pipeline]
    C --> T[15 function tools]
    W[Web page, script] --> P[ArticlePipeline]
    P -->|localize, plan a talk,<br/>requests in words| C
    P -->|everything else,<br/>as a person's action| T
    T --> POL[posting policy<br/>app/policy.py]

    subgraph LP_detail [localization_pipeline]
        SG[SeedGate] --> FAN[ParallelAgent<br/>ko / en / ja localizers + titles]
        FAN --> LOOP[LoopAgent, max 2 rounds]
        LOOP --> FC[FidelityCheckAgent<br/>deterministic]
        FC --> CR[critic agent<br/>reasoning model]
        CR --> RG[ReviewGate]
        RG -->|failed| RF[ParallelAgent<br/>refiners for failed languages]
        RF --> FC
        RG -->|passed| REP[LocalizationReportAgent]
    end
    LP --> SG

    subgraph SP_detail [slide_planning_pipeline]
        BG[BriefGate] --> PL[slide_plan_agent<br/>reasoning model]
        PL --> TG[SlideTimingGate<br/>files the plan under its language]
        TG -->|failed| PL
        TG -->|passed| SR[SlideReportAgent]
    end
    SP --> BG

    T --> SM[Secret Manager]
    T --> ST[(SQLite or Firestore)]
    T --> Q[Qiita API]
    T --> D[DEV Community API]
    T --> N[Naver Blog<br/>read only + HTML export]
    T --> GS[Google Slides API<br/>OAuth, drive.file]
    T -.->|only when enabled| MI[mermaid.ink]
```

Control flow is decided in code, not by a model: the gate agents in
`app/workflow_agents.py` decide which languages run, whether a review passed,
which language a slide plan belongs to and whether it meets the timing rule.
The web page and the script ask the agent only for what needs judgement
(localizing, planning a talk, a change asked for in words). Storing the source
document, exporting, finalizing, creating a deck, posting and the manifest are
calls to the same tool functions, made directly, and each step is checked
before the next one starts.

## Quick start

Requirements: [uv](https://docs.astral.sh/uv/), the Google Cloud SDK with
Application Default Credentials, and `agents-cli`. For a step-by-step walkthrough of issuing platform tokens and Google Slides OAuth credentials, see the setup guide ([English](docs/SETUP.md) · [한국어](docs/kr-SETUP.md) · [日本語](docs/ja-SETUP.md)).

```bash
uv tool install google-agents-cli
agents-cli install            # creates .venv and installs dependencies
cp .env.example .env          # set GOOGLE_CLOUD_PROJECT
```

Platform credentials are read from Secret Manager (never from the repository):

```bash
read -rs TOKEN && printf '%s' "$TOKEN" | gcloud secrets create QIITA_ACCESS_TOKEN --data-file=- && unset TOKEN
read -rs TOKEN && printf '%s' "$TOKEN" | gcloud secrets create DEVTO_API_KEY --data-file=- && unset TOKEN
```

Stages 1, 2 and 4 work without any platform credential.

Run it:

```bash
agents-cli playground                                  # web UI
agents-cli run "Hello, what can you help me with?"     # one-shot from the terminal
```

Or use the web front end: hand in a URL or a draft, press one button, and get a
table with each language's title, files, slides and next step. The next step
follows the order of the work: review the post (Mermaid diagrams are shown as
drawings in the preview when `BLOG_AGENT_RENDER_MERMAID` is on; their source is
then sent to mermaid.ink), edit it in the full-page editor if needed, press
Finalize, then approve the post and choose in that approval whether it is a
draft or public. Changing the title or the text of a post after it was
finalized makes it unfinished again. A Naver Blog URL is registered after you
post by hand. Choosing a slide length and languages before a run approves, in
advance, one new Slides file per language in your own Drive. The speaker notes
can be edited too; their text is carried into the existing deck, or pulled
back from it, only after an approval that lists the slides that change.

```bash
uv run python -m app.web_ui                 # http://127.0.0.1:8502
```

The front end has no login, so it listens on localhost, and answers only
requests addressed to localhost, unless `--host` is given; with `--host` it
serves anyone who can reach it. A run survives a restart of the server: its session is in a local
SQLite file (`.adk/sessions.sqlite3`) and its record and activity log are under
`workspace/.jobs/`. Opening the run's address again, or just the page in the
same browser, shows it as it was; a run that was working when the server
stopped says so.

Or run every stage for one article with a single command:

```bash
uv run python scripts/run_pipeline.py --url https://qiita.com/<user>/items/<id>
uv run python scripts/run_pipeline.py --draft post.md \
    --talk-minutes 10 --talk-language en --talk-language ko \
    --publish devto --tags ai,agents
```

The script goes through the same steps as the page and stops if the structure
check fails. The language of a draft is detected from its text. Without
`--publish` nothing is posted; with it, the terminal asks you to finalize each
post, to approve it, and whether it is a draft or public. Logs go to stderr
(`2>/dev/null` hides them).

A typical conversation:

1. "Save this draft as the seed article" followed by the Markdown, or "Use this post: https://qiita.com/...".
2. "It is final." then "Localize it."
3. "The Japanese post is ready." The agent asks you to confirm that it is final. Then "Export the Naver HTML and create the Qiita draft with the tags AI and LLM." The agent pauses for approval before the Qiita call.
4. "Plan a 5 minute talk in English for backend developers."
5. "Give me the artifact manifest."

Files are written under `workspace/<project-id>/` (`BLOG_AGENT_WORKSPACE_DIR`).

## `agents-cli` commands used in this project

| Command | Purpose |
| :--- | :--- |
| `agents-cli scaffold create ... --agent adk_python --deployment-target agent_runtime` | Generated the project, Terraform and test layout |
| `agents-cli install` | Install dependencies |
| `agents-cli playground` / `agents-cli run "<prompt>"` | Local runs |
| `agents-cli lint` | Ruff check |
| `agents-cli eval run` | Run the agent over `tests/eval/datasets/basic-dataset.json` and grade the traces |
| `agents-cli infra single-project` | Apply `deployment/terraform/single-project` |
| `agents-cli deploy` | Deploy to Agent Runtime |

### Google Slides (optional)

To let the agent create a Google Slides deck with speaker notes, grant access
once. Enable the Slides API, create an OAuth client of type "Desktop app" in
the console, then:

```bash
uv run python scripts/authorize_google_slides.py \
    --client-file ~/client_secret.json --account you@example.com
```

Cautions:

- Use a personal or dedicated Google account, not a work or school account.
  The OAuth app is your own unverified app, and every deck is created in the
  granting account's Drive.
- "Access blocked ... 403 access_denied" during sign-in means the signed-in
  account is not a test user of your consent screen. Switch accounts; do not
  add a work account as a test user to get past it.
- The script names the account that granted access and stores nothing until
  it is confirmed (or matches `--account`); a refused grant is revoked. The
  approval request for a single deck names the account again, and Settings
  on the web page shows it.
- The grant covers only files the agent created (`drive.file`).
- Images already in the article are placed on slides. Mermaid diagrams are
  placed only when `BLOG_AGENT_RENDER_MERMAID=true`: the diagram source is
  then sent to mermaid.ink, a third-party service, to be drawn. Leave it off
  for drafts whose diagrams must not leave your machine.
- Never paste the client file, the redirected address or a screenshot of the
  client secret into a chat or an issue. If that happens, delete the OAuth
  client, create a new one and authorize again.

## Tests and evaluation

```bash
uv run pytest tests/unit          # deterministic, no model or network
uv run pytest tests/integration   # calls the model; needs credentials
agents-cli eval run               # golden dataset, two metrics, thresholds in tests/eval/eval_config.yaml
```

The structure check (`check_fidelity`) treats code blocks by kind: program code
must match the source apart from comments; Mermaid diagrams must keep node ids,
edges and keywords while label text is translated; plain-text blocks may
translate Korean or Japanese lines only, so an English plain-text block stays
as written.

- `tests/unit` covers the posting policy, the tools, the structure and timing
  checks, the pipeline's steps and what it reports when a step fails, the web
  routes, redaction, secret lookup, redirects and error handling of the
  platform clients, the policy plugin, memory, the gate agents and the
  wording of the instructions. Platform HTTP calls are replaced with fakes;
  nothing is posted.
- `tests/unit/test_approvals.py` runs the real ADK `Runner` with the real
  posting tools and the real pause-and-resume machinery; only the model is
  scripted. It shows that an approval reaches the one request it was given
  for, with its draft-or-public choice, and no other.
- `tests/unit/test_spec.py` reads `docs/SPEC.md` and fails when a requirement
  has no test, a named test does not exist, or the Korean companion lists
  different requirements.
- `tests/eval` has six single-turn cases (scope, off-topic, unsupported URL,
  publish with no article, prompt injection inside a draft, saving a Korean
  draft). `custom_response_quality` is an LLM judge scored against a written
  reference per case. `publish_requires_approval` is deterministic: it fails
  the run if any publishing tool succeeded, since no case contains an approval.

## Where each capability lives

| Capability | Where to look |
| :--- | :--- |
| Tool docstrings and naming | `app/tools/articles.py`, `app/tools/publish.py`, `app/tools/slides.py` |
| Explicit schemas | `app/schemas.py` (Pydantic input and output models; `ReviewVerdict` and `SlideDeckPlan` used as `output_schema`) |
| Guided error handling | `tool_error`, `validation_error` in `app/schemas.py`: every failure returns `error_code` and `recovery_instruction` |
| System instructions | `app/prompts.py` |
| History compaction and context caching | `EventsCompactionConfig`, `ContextCacheConfig` in `app/agent.py` |
| Persistent state | Sessions: `app/app_utils/services.py` (SQLite locally, Agent Runtime sessions when deployed), used by the playground, the web page and the script alike. Articles: `app/persistence.py` (`SqliteArticleStore`, `FirestoreArticleStore`). Runs of the web page: `Job.save`, `_reopen` in `app/web_ui.py` |
| Asynchronous memory | `schedule_memory_consolidation` in `app/memory.py`, `PreloadMemoryTool` in `app/agent.py` |
| Multi-agent patterns | Coordinator with sub-agents, `ParallelAgent`, `LoopAgent`, `SequentialAgent` in `app/agent.py`; custom `BaseAgent` gates in `app/workflow_agents.py` |
| Model routing | `MODEL_ROUTES`, `model_for` in `app/models.py` |
| Guardrails and policy | Posting rules in `app/policy.py`, called by the tools; `ContentPolicyPlugin` in `app/plugins.py` (URL allowlist on every tool call, credential scrub on the coordinator's output); `check_fidelity` in `app/markdown_checks.py`; critic and gates in the review loops; article text between random-token markers (`fenced` in `app/prompts.py`); `sanitize_html` for the Naver export in `app/tools/publish.py`; no redirects with credentials in `app/platforms.py` |
| Human in the loop | `_approval` and `finalize_localized_post` in `app/tools/publish.py`, the deck tool in `app/tools/slides.py` (`request_confirmation`), `ResumabilityConfig` in `app/agent.py`; one answer for one request: `ArticlePipeline.answer_approval` in `app/pipeline.py`; changing an existing deck: `sync_deck` in `app/web_ui.py`, `app/deck_sync.py` |
| Structured logging | `get_logger`, `log_event` in `app/observability.py` (JSON, redaction filter) |
| Intent and outcome logging | `ObservabilityPlugin` in `app/observability.py` |
| Tracing | `traced_span` in `app/observability.py`; OpenTelemetry export set up in `app/fast_api_app.py` |
| PII and credential redaction | `app/redaction.py` (regex always; Cloud DLP when `BLOG_AGENT_ENABLE_DLP=true`) |
| Automated evaluation | `tests/eval/`, `tests/unit/` |
| Specification and traceability | `docs/SPEC.md`, `tests/unit/test_spec.py` |
| Infrastructure as code | `deployment/terraform/single-project/` (scaffold) plus `blog_agent.tf` (Firestore, secret containers, least-privilege IAM) |
| Secret management | `get_secret` in `app/secrets.py`: environment → Secret Manager → a file outside the repository |

## Status and known limits

What has been run for real, and what has not:

| Item | Status |
| :--- | :--- |
| Draft → localize → Naver HTML → Qiita private draft → DEV unpublished draft → slide plan → manifest | Run end to end locally against the real model and the real Qiita and DEV APIs, before posting required a finalized post and before the pipeline called the tools directly. Since then, everything up to but not including the two platform writes was run again (next rows) |
| The run as it is now (source stored by code, localization and slide plan by the agent, export and manifest by code, every step checked) | Run three times against the real model, nothing posted, no deck created: a published Qiita post with a three-minute Korean talk; an English draft of 23,000 characters with nine plain-text blocks, which stayed as written in Korean and in Japanese; the final texts passed the structure check and the review after the Japanese text was refined once; a short Korean draft through the web server's HTTP API with a three-minute English talk. In the last one the stored seed was compared with the input and was identical byte for byte |
| Localizing a made-up example, keeping facts | Read in two real runs. A published Japanese article whose sample user prefers a Japanese airline: the Korean text names a Korean airline and the English text a United States airline, each in all eight places, while the real company and city of the same example stayed. A Korean draft with a sample traveller: a different name and airline in English and in Japanese, a statement about a real product unchanged. Which counterpart is chosen is the model's choice and differs between runs; two articles are not a proof |
| Finalize, the refusal to post an unfinalized post, tag rules, a title edit undoing the finalization | Exercised through the real web server's HTTP API on that run. No post was sent |
| A run surviving a restart | The web server was stopped and started again on that run; the page showed the same titles, finalizations, files and activity log, read from the SQLite session file and the run's record. An approval pending at a restart is lost by design and was not exercised |
| One approval for one request | Unit-tested with the real ADK runner and tools and a scripted model: of two posts waiting for approval, the one approved as public was sent as public and the other was declined and not sent. Not run against the real platforms |
| Published Qiita post → localize into Korean and English | Run locally |
| Approval pause and resume for publishing | Run locally against the real platforms through the chat route, before the changes above; that route is now covered by the unit test with the real runner. The page and the script no longer pause: the click or the terminal answer is the approval and the tool is called with it |
| Firestore article store | Read/write/cleanup exercised against a real database |
| Deployment to Agent Runtime, Memory Bank in the cloud | Not deployed. Memory wiring is in place; it has only run locally where the default memory service is in-memory |
| Terraform | Written, never applied or validated with `terraform validate` |
| Cloud DLP path | Implemented behind a flag, not exercised |
| Refinement branch of the localization loop | Covered by unit tests of the gates. It ran in one real run (two review rounds, both languages refined); most runs pass on the first round |
| Translated Mermaid diagrams | The structure check confirms node ids and edges are unchanged; the diagrams have not been rendered to confirm they still draw |
| Google Slides deck with speaker notes | Decks were created through the real Slides API and checked by reading them back and by rendering slide thumbnails: cover slide, titles sized to fit, bullets (Korean and Japanese included), an article image placed beside the bullets, speaker notes. With `BLOG_AGENT_RENDER_MERMAID` on, Mermaid diagrams are rendered by mermaid.ink and placed as images; this ran in real decks. PDF export is not implemented |
| Image placement by shape (wide under the bullets, tall over the full page height, title narrowed) | Unit-tested, then run against the real Slides API on one ten-slide test deck built from a real slide plan and its rendered diagrams: all nine images were placed, the five titles next to full-height images were narrowed (one of them Korean), and every slide was checked by thumbnail. Images that are not rendered diagrams keep the right half, because their size is not read |
| Editing a deck's text from its Markdown plan, and pulling deck edits back | Each direction needs an approval that lists the slides that change; only titles, bullets and speaker notes move. The reading, comparing and writing were run against the real Slides API on the test deck: titles, bullets and notes (Korean and an emoji included) arrived intact and kept their font size, weight and bullets, and a note changed in the deck was pulled back. The path through the web page and its approval dialog was exercised in a browser against a stand-in for the Slides API, not against the real one |
| Talk structure (title-slide greeting, agenda, conclusion, separate next steps, thank-you slide) | Two eight-minute plans were made by the real model, in English and in Korean, and both followed it; one of them was built into a real deck through the Slides API and read back, with the greeting in the title slide's notes and the closing words in the thank-you slide's notes. Longer talks and Japanese were not run |
| Draft or public, chosen by the person approving | Unit-tested with stand-ins for the platforms: an approval that asks for a public post publishes, any other approval saves a non-public draft, and a post that is already public is not posted again. Not run against the real Qiita or DEV APIs |
| Mermaid diagrams drawn in the file preview | The twelve diagrams of one real article were drawn through mermaid.ink by the server, all requested at once. The route that serves them is unit-tested with a stand-in drawer; the finished page was not viewed in a browser with the kept images |
| Naver Blog publishing | Manual by design: Naver has no public write API. The agent exports HTML to paste and then verifies the URL the author gives it |
| Naver Blog reading | Best effort from the public mobile page; heading levels are not recovered |
| Speaking-time check | Estimated from script length with assumed rates (140 words/min English, 300 characters/min Korean, 330 characters/min Japanese), not measured |
| Web front end | A full run, file views, finalizing and editing were exercised through its HTTP API against the real model; creating a draft or a public post from it has not been run against the real platforms since posting was rebuilt. The page of that real run, the editor and a preview were rendered in headless Chrome and looked at. The approval dialogs (draft or public, credential findings, create deck) and the Settings dialog were not clicked through in a browser after the rebuild; their routes are unit-tested |
| Google Slides deck creation after the per-language rebuild | The requests that build a deck were compared, request for request, with those recorded before the rebuild for two real plans and are identical. No new deck was created through the real API after the rebuild |
| Platform clients: no redirects with credentials, unknown outcomes, guided errors | Unit-tested against a mock transport, including mutation checks. Not provoked against the real platforms |
| Independent review of the rebuilt code | A second reviewer read the rebuilt code against the specification and reported 19 findings, two of them against invariants (a yes to finalize could be carried to a swapped text; titles reached the coordinator's instruction raw). All but two were fixed and each fix has a unit test; the two left as they are (the agent's API server checks no host name; a second draft in one chat is a revision of the first) are listed under known limits in the specification. The fixes were not reviewed a second time |
| CI/CD pipeline | Not set up |

`ParallelAgent`, `SequentialAgent` and `LoopAgent` print deprecation warnings in
ADK 2.x in favour of `Workflow`. They are kept because a `Workflow` cannot yet
be used as a sub-agent of an `LlmAgent`.

## Project layout

```
app/
  agent.py              agent composition and App configuration
  workflow_agents.py    deterministic gate, check and report agents
  prompts.py            instructions
  models.py             model routing
  schemas.py            Pydantic schemas and tool error helpers
  tools/                function tools (articles, publish, slides)
  policy.py             when a post may be finalized and sent
  platforms.py          Qiita, DEV Community and Naver HTTP clients
  google_slides.py      Google Slides deck creation and image placement
  deck_sync.py          text kept in step between a deck and its Markdown plan
  markdown_checks.py    structure fidelity and speaking-time checks
  plugins.py            content policy plugin
  observability.py      JSON logging, intent/outcome plugin, spans
  redaction.py          PII and credential redaction
  secrets.py            secret lookup
  persistence.py        article store (SQLite / Firestore)
  pipeline.py           runs the steps for the script and the web UI, and checks each
  web_ui.py, static/    web front end
  memory.py             background memory consolidation
  app_utils/            scaffolded service wiring, A2A, Agent Runtime adapter
deployment/terraform/   infrastructure
docs/                   SPEC.md (specification), kr-SPEC.md (Korean companion);
                        SETUP.md, kr-SETUP.md, ja-SETUP.md (setup guides);
                        kr-AS-BUILT.md (the design and code as they are at
                        b5e1793, and where they differ from SPEC.md) and
                        kr-DESIGN-v1.md (the proposed version 1), in Korean
scripts/                run_pipeline.py: all stages in one command;
                        authorize_google_slides.py: one-time Slides grant
tests/                  unit, integration, eval
```

## License

The file headers that the scaffold generated name Apache-2.0. The repository
has no `LICENSE` file yet; adding one is the owner's decision.
