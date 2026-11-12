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
| 1. Source document | Keeps the text exactly as handed in (`source.original.md`) and stores it as the seed. A draft can be pasted, attached as a file, or given as the address of a Google Docs document or of a Medium post that is still in its author's feed; a post already published on Qiita, DEV Community or Naver Blog is read from there, and that platform is then never written to. A draft without headings, and text read from a Naver page, is given Markdown structure by a model; the structured copy is used only when code confirms that its letters and digits are those of the original, and a notice says which text went on. In a chat the agent can also revise a draft with the author. | Author hands in the document (in a chat: says when it is final) |
| 2. Localize | Writes the two missing languages and their titles in parallel, checks structure against the seed in code, has a critic model review meaning, fixes only what failed. | Author reviews the files and titles |
| 3. Publish | Exports paste-ready HTML for Naver Blog (ko). Sends a post to Qiita (ja) or DEV Community (en) through the API. Verifies the resulting URLs. | Author reviews, edits and finalizes each post (title and text). Approval before every write; the post is a non-public draft unless the person approving asks for a public post in that approval. The model can choose neither the title nor the visibility |
| 4. Present | Plans one slide per minute with a speaker script, checks estimated speaking time, re-plans once if it fails. The deck opens with a title slide (greeting, title and what it means, in its speaker notes), follows with an agenda, ends with a conclusion, next steps on their own slide when the article names any, and a thank-you slide. Writes the plan and an artifact manifest. | Author reviews the plan |

Language to platform mapping: Korean → Naver Blog, English → DEV Community,
Japanese → Qiita. A post that was fetched from a platform is never written back
to that platform. Medium and Google Docs are read from only.

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
    C --> T[function tools]
    W[Web page] --> R[runs<br/>main lane: source, posts, requests<br/>slides lane: plans, decks, PDF, sync]
    R --> P[ArticlePipeline]
    S[Script] --> P
    P -->|localize,<br/>requests in words| C
    P -->|plan a talk: one working session<br/>per language, several at once| SP
    P -->|everything else,<br/>as a person's action| T
    P -->|structure for a draft<br/>without headings| G[model gateway<br/>app/llm.py]
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
    T --> RO[Google Docs, Medium<br/>read only, as drafts]
    T --> GS[Google Slides API<br/>OAuth, drive.file]
    T -.->|only when enabled| MI[mermaid.ink]
```

Control flow is decided in code, not by a model: the gate agents in
`app/workflow_agents.py` decide which languages run, whether a review passed,
which language a slide plan belongs to and whether it meets the timing rule.
The web page and the script ask a model only for what needs judgement:
localizing and a change asked for in words go to the coordinator; a talk is
planned by the slide planning pipeline in a working session per language, so
several languages are planned at once and the record of the run is not written
to meanwhile; the structure of a draft without headings goes through one
gateway (`app/llm.py`). Storing the source document, exporting, editing,
finalizing, creating a deck, posting and the manifest are calls to the same
tool functions, made directly, and each step is checked before the next one
starts.

On the page a run has two lanes (`app/runs.py`). The main lane holds the
source document, the localization, the posts and requests to the agent; the
slides lane holds plans, decks, PDF export and deck text sync. One action runs
at a time in a lane and the two lanes run at the same time, so posts can be
reviewed, finalized and posted while slides are made. Every action ends as
done or as a notice on the page.

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
requests addressed to localhost, unless `--host` is given; with `--host` for
another address every route requires an access token (`BLOG_AGENT_UI_TOKEN`, or
one made at start), and the address to open, with the token in it, is shown once
in the terminal. A run survives a restart of the server: its session is in a local
SQLite file (`.adk/sessions.sqlite3`) and its record and activity log are under
`workspace/.jobs/`. Opening the run's address again, or just the page in the
same browser, shows it as it was; a run that was working when the server
stopped says which lane was interrupted and offers to make the slides that
are missing.

More of the page:

- The Slides column offers, per language, what its state allows: "Create
  slides…", "Speaker notes", "Create deck…", "Google Slides", "PDF" with the
  time it was exported or "Export PDF", "Re-create…" and "Update deck…".
  Creating a deck again makes a new file and keeps the address of the earlier
  one. `BLOG_AGENT_SLIDE_PARALLELISM` (3 unless set) is how many languages
  are planned at once.
- "Ask the agent for a change" sends one line to the agent. The agent changes
  a title, or replaces a passage it quotes from a post. A request for slides
  typed there creates nothing by itself: the agent calls
  `request_approval_to_create_slide_decks`, the page shows the request with
  Approve and Decline, and the slides lane makes the slides once approved.
  The suggestions under the box come from the page's own texts; what you
  typed is kept in the browser as typed.
- The page, the editor and the previews are shown in English, Korean or
  Japanese (Settings). Every text of the page comes from one catalog per
  language (`app/static/i18n/`), and notices from the server carry a code so
  that they are shown in that language too. Article text, what you typed, the
  agent's replies and what a platform returned are not translated.
- "Recent sources…" lists the runs on this server, from their records.
- Every action is one trace, and structured log records carry its ids. With
  `BLOG_AGENT_TRACE_EXPORT=cloud` the spans are exported to Cloud Trace;
  without it nothing is exported.

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
  wording of the instructions, the lanes and the record session, slide
  planning in working sessions, the contract of the tools, the catalogs of
  the page, tracing and the access token. Platform HTTP calls are replaced
  with fakes; nothing is posted. A fixture (`tests/unit/conftest.py`)
  refuses network connections and model calls, and `.env` is not read.
- `tests/unit/test_approvals.py` runs the real ADK `Runner` with the real
  posting tools and the real pause-and-resume machinery; only the model is
  scripted. It shows that an approval reaches the one request it was given
  for, with its draft-or-public choice, and no other.
- `tests/unit/test_spec.py` reads `docs/SPEC.md` and fails when a requirement
  has no test, a named test does not exist, or the Korean companion lists
  different requirements. It also checks the table "Where each capability
  lives" below: every path in it exists, and every name it gives as code is
  found in a file its row names.
- CI (`.github/workflows/ci.yml`) runs the linter, the format check, the unit
  tests and `terraform fmt -check` and `terraform validate` on every push,
  without credentials. It installs from PyPI: `uv.lock` names a package index
  that the runner cannot reach.
- `tests/eval` has eight single-turn cases (scope, off-topic, an address on a
  site that is not a source, publish with no article, slides with no article,
  a public post asked for without the draft step, prompt injection inside a
  draft, saving a Korean draft). `custom_response_quality` is an LLM judge
  scored against a written reference per case; the judge is the fast model
  (`FAST_MODEL_NAME`). `publish_requires_approval` is deterministic: it fails
  the run if any tool that pauses for a person succeeded, since no case
  contains an approval.

## Where each capability lives

| Capability | Where to look |
| :--- | :--- |
| Tool docstrings and naming | `app/tools/articles.py`, `app/tools/publish.py`, `app/tools/slides.py`. The contract every tool is held to (name, docstring, typed arguments, checked input, guided errors) is tested by `tests/unit/test_tool_contract.py`, which reads the tools from the agent tree in `app/agent.py` |
| Explicit schemas | `app/schemas.py`: Pydantic input and output models; `LanguageCode`, `PlatformName` and `CodeMode` show the model the allowed values. `LocalizedTitles`, `ReviewVerdict` and `SlideDeckPlan` are the `output_schema` of agents in `app/agent.py` |
| Guided error handling | `tool_error`, `validation_error` in `app/schemas.py`: every failure returns `error_code` and `recovery_instruction` |
| System instructions | `COORDINATOR_INSTRUCTION` and the instructions of the other agents in `app/prompts.py` |
| History compaction and context caching | `EventsCompactionConfig`, `ContextCacheConfig` in `app/agent.py` |
| Persistent state | Sessions: `get_session_service` in `app/app_utils/services.py` (a SQLite file locally, Agent Runtime sessions when deployed), used by the playground, the web page and the script alike. Articles: `SqliteArticleStore`, `FirestoreArticleStore` in `app/persistence.py`. Runs of the web page: `Job.save`, `reopen` in `app/runs.py` |
| Asynchronous memory | `schedule_memory_consolidation` in `app/memory.py`, `PreloadMemoryTool` in `app/agent.py` |
| Multi-agent patterns | Coordinator with sub-agents, `ParallelAgent`, `LoopAgent`, `SequentialAgent` in `app/agent.py`; the slide planning pipeline is built twice by `build_slide_planning_pipeline`, under the coordinator and as an application of its own (`build_slide_planning_app`); custom `BaseAgent` gates in `app/workflow_agents.py` |
| Model routing | `MODEL_ROUTES`, `model_for` in `app/models.py`; the one model call outside the agents, `generate_text` in `app/llm.py`, takes its model from the same table |
| Guardrails and policy | Posting rules in `app/policy.py`, called by the tools; `ContentPolicyPlugin` in `app/plugins.py` (URL allowlist on every tool call, credential scrub on the coordinator's output); `check_fidelity` in `app/markdown_checks.py`; critic and gates in the review loops; article text between random-token markers (`fenced` in `app/prompts.py`); a structured copy of a draft checked letter for letter (`same_letters` in `app/structurer.py`); one function for every change to a post (`apply_post_edit` in `app/tools/articles.py`); `sanitize_html` for the Naver export in `app/tools/publish.py`; the hosts that may be fetched, from one table (`SOURCE_KINDS` in `app/sources.py`); no redirects with credentials in `app/platforms.py` |
| Human in the loop | `_approval` and `finalize_localized_post` in `app/tools/publish.py`; the deck tool and `request_approval_to_create_slide_decks` in `app/tools/slides.py` (`request_confirmation`); `ResumabilityConfig` in `app/agent.py`; one answer for one request: `ArticlePipeline.answer_approval` in `app/pipeline.py`; an approved slide request is started in the slides lane by `start_requested_slides` in `app/runs.py`; changing an existing deck: `sync_deck` in `app/web_ui.py`, `app/deck_sync.py` |
| Structured logging | `get_logger`, `log_event`, `log_activity` in `app/observability.py` (JSON, redaction filter); the activity lines of a run are records too, with the run id |
| Intent and outcome logging | `ObservabilityPlugin` in `app/observability.py` for the agent's tool calls; `ArticlePipeline.act` in `app/pipeline.py` for a person's actions; `generate_text` in `app/llm.py` for the direct model call |
| Tracing | `setup_tracing`, `root_span` and `traced_span` in `app/observability.py`: one trace per action started on the page (`app/runs.py`) or with the script (`scripts/run_pipeline.py`), with a span for every outbound call (`app/platforms.py`, `app/google_slides.py`, `app/llm.py`); exported to Cloud Trace when `BLOG_AGENT_TRACE_EXPORT=cloud`; the agent API server's export is set up in `app/fast_api_app.py` |
| PII and credential redaction | `app/redaction.py` (regex always; Cloud DLP when `BLOG_AGENT_ENABLE_DLP=true`); identifiers are left as they are (`_is_identifier` in `app/observability.py`) |
| Automated evaluation | `tests/eval/` (eight cases, two metrics, thresholds in `tests/eval/eval_config.yaml`); `tests/unit/`, kept away from models and the network by `tests/unit/conftest.py` |
| Specification and traceability | `docs/SPEC.md`, `tests/unit/test_spec.py` |
| Infrastructure as code | `deployment/terraform/single-project/` (scaffold) plus `deployment/terraform/single-project/blog_agent.tf` (Firestore, secret containers, least-privilege IAM); formatted and validated on every push by `.github/workflows/ci.yml` |
| Secret management | `get_secret` in `app/secrets.py`: environment → Secret Manager → a file outside the repository |

## Status and known limits

What has been run for real, and what has not:

| Item | Status |
| :--- | :--- |
| Version 1.0 (lanes, slide planning in working sessions, the slide request with its approval, one function for edits, the structuring check and the model gateway, Medium as a source, the deck record and PDF, the catalogs and coded notices, "Recent sources", the offer after an interruption, the access token, tracing) | Verified with unit tests and stand-ins only, and by looking at the page in a headless browser on a throwaway server with made-up data. Not run: slide planning by a real model in working sessions (three languages at once; whether the quota carries it is not known); the structuring gateway with a real model; a real Medium address; any call to the real Google Slides API after these changes; export to Cloud Trace; Cloud DLP; the access token on a host name behind a proxy. The Korean and Japanese texts of the catalogs were not read by a native reader |
| Posting and decks from the page, before the version 1 work | On 2026-11-12 one run on the page posted to DEV Community and to Qiita through the real APIs, registered a Naver URL, and created decks in three languages through the real Slides API (read from that run's activity log; `docs/kr-AS-BUILT.md`, section 10). That was the code at `b5e1793`. Nothing was posted and no deck was created with the code as it is now |
| CI | GitHub Actions runs the linter, the format check, the unit tests and `terraform fmt -check` and `terraform validate` on every push, and passes on `main`. It uses no credential and deploys nothing |
| Evaluation (`agents-cli eval run`) | Run on 2026-11-12 against the real model with the eight cases: `RESULT: PASS (custom_response_quality 5.00 >= 4.00; publish_requires_approval 1.00 >= 1.00)`. The run before the two cases were added, the same day with six cases, passed with the same scores |
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
| Terraform | Formatted and validated in CI on every push (`terraform fmt -check`, `terraform validate`). Never planned or applied |
| Cloud DLP path | Implemented behind a flag. Unit-tested with a stand-in client (called only when enabled; a failure keeps the regex result); not run against Cloud DLP |
| One trace per action, with the trace id in every log record | Unit-tested with spans collected in memory: the action's root span, ADK's agent and tool spans and the outbound-call spans share one trace. Export to Cloud Trace (`BLOG_AGENT_TRACE_EXPORT=cloud`) has not been run |
| Refinement branch of the localization loop | Covered by unit tests of the gates. It ran in one real run (two review rounds, both languages refined); most runs pass on the first round |
| Translated Mermaid diagrams | The structure check confirms node ids and edges are unchanged; the diagrams have not been rendered to confirm they still draw |
| Google Slides deck with speaker notes | Decks were created through the real Slides API and checked by reading them back and by rendering slide thumbnails: cover slide, titles sized to fit, bullets (Korean and Japanese included), an article image placed beside the bullets, speaker notes. With `BLOG_AGENT_RENDER_MERMAID` on, Mermaid diagrams are rendered by mermaid.ink and placed as images; this ran in real decks. PDF export of a deck is unit-tested with a stand-in for the export call; no export through the real API is recorded here |
| Image placement by shape (wide under the bullets, tall over the full page height, title narrowed) | Unit-tested, then run against the real Slides API on one ten-slide test deck built from a real slide plan and its rendered diagrams: all nine images were placed, the five titles next to full-height images were narrowed (one of them Korean), and every slide was checked by thumbnail. Images that are not rendered diagrams keep the right half, because their size is not read |
| Editing a deck's text from its Markdown plan, and pulling deck edits back | Each direction needs an approval that lists the slides that change; only titles, bullets and speaker notes move. The reading, comparing and writing were run against the real Slides API on the test deck: titles, bullets and notes (Korean and an emoji included) arrived intact and kept their font size, weight and bullets, and a note changed in the deck was pulled back. The path through the web page and its approval dialog was exercised in a browser against a stand-in for the Slides API, not against the real one |
| Talk structure (title-slide greeting, agenda, conclusion, separate next steps, thank-you slide) | Two eight-minute plans were made by the real model, in English and in Korean, and both followed it; one of them was built into a real deck through the Slides API and read back, with the greeting in the title slide's notes and the closing words in the thank-you slide's notes. Longer talks and Japanese were not run |
| Draft or public, chosen by the person approving | Unit-tested with stand-ins for the platforms: an approval that asks for a public post publishes, any other approval saves a non-public draft, and a post that is already public is not posted again. The run of 2026-11-12 (second row) posted to both platforms; which choice was made there is not recorded here, and the choice was not run against the real APIs with the code as it is now |
| Mermaid diagrams drawn in the file preview | The twelve diagrams of one real article were drawn through mermaid.ink by the server, all requested at once. The route that serves them is unit-tested with a stand-in drawer; the finished page was not viewed in a browser with the kept images |
| Naver Blog publishing | Manual by design: Naver has no public write API. The agent exports HTML to paste and then verifies the URL the author gives it |
| Naver Blog reading | Best effort from the public mobile page; heading levels are not recovered |
| Speaking-time check | Estimated from script length with assumed rates (140 words/min English, 300 characters/min Korean, 330 characters/min Japanese), not measured |
| Web front end | A full run, file views, finalizing and editing were exercised through its HTTP API against the real model; posting from it ran once against the real platforms, on 2026-11-12 with the code before version 1 (second row), and not since. The page of that real run, the editor and a preview were rendered in headless Chrome and looked at. The approval dialogs (draft or public, credential findings, create deck) and the Settings dialog were not clicked through in a browser after the rebuild; their routes are unit-tested |
| Google Slides deck creation after the per-language rebuild | The requests that build a deck were compared, request for request, with those recorded before the rebuild for two real plans and are identical. Decks were then created through the real API from the page on 2026-11-12 (second row); none since the version 1 work |
| Platform clients: no redirects with credentials, unknown outcomes, guided errors | Unit-tested against a mock transport, including mutation checks. Not provoked against the real platforms |
| Independent review of the rebuilt code | A second reviewer read the rebuilt code against the specification and reported 19 findings, two of them against invariants (a yes to finalize could be carried to a swapped text; titles reached the coordinator's instruction raw). All but two were fixed and each fix has a unit test; the two left as they are (the agent's API server checks no host name; a second draft in one chat is a revision of the first) are listed under known limits in the specification. The fixes were not reviewed a second time |
| Deployment pipeline (CD) | Not set up |

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
  llm.py                the one gateway for model calls outside the agents
  schemas.py            Pydantic schemas and tool error helpers
  state.py              names of the session-state keys
  settings.py           settings read from the environment
  env_file.py           reading .env, in one place
  tools/                function tools (articles, publish, slides)
  policy.py             when a post may be finalized and sent
  sources.py            the table of source kinds and the hosts they are read from
  structurer.py         Markdown structure for a draft, and the check of the copy
  platforms.py          Qiita, DEV Community, Naver, Google Docs and Medium HTTP clients
  google_slides.py      Google Slides deck creation, image placement, PDF export
  deck_sync.py          text kept in step between a deck and its Markdown plan
  markdown_checks.py    structure fidelity and speaking-time checks
  plugins.py            content policy plugin
  observability.py      JSON logging, intent/outcome plugin, traces and spans
  redaction.py          PII and credential redaction
  secrets.py            secret lookup
  persistence.py        article store (SQLite / Firestore)
  pipeline.py           runs the steps for the script and the web UI, and checks each
  runs.py               runs of the web page: lanes, records, starting actions
  notices.py            the sentences the server makes for the page, each with a code
  web_ui.py             web front end: routes, access token, previews
  static/               index.html, app.css, app.js (the page); edit.html, edit.css,
                        edit.js (the editor); i18n.js and i18n/en.json, ko.json,
                        ja.json (the texts of the page, one catalog per language)
  fast_api_app.py       the agent's API server
  memory.py             background memory consolidation
  app_utils/            scaffolded service wiring, A2A, Agent Runtime adapter
deployment/terraform/   infrastructure
docs/                   SPEC.md (specification, version 1.0), kr-SPEC.md (Korean
                        companion); SETUP.md, kr-SETUP.md, ja-SETUP.md (setup
                        guides); kr-AS-BUILT.md (the design and code as they were
                        at b5e1793) and kr-DESIGN-v1.md (the plan of the work that
                        made version 1.0), both in Korean and both closed
scripts/                run_pipeline.py: all stages in one command;
                        authorize_google_slides.py: one-time Slides grant
tests/                  unit, integration, eval
.github/workflows/      ci.yml: linter, unit tests, Terraform validation on every push
LICENSE                 Apache License 2.0
```

## License

Apache License 2.0; the text is in [LICENSE](LICENSE). The files that the
scaffold generated carry a licence header; the files written for this project
do not (item 1 of section 8 of the specification).
