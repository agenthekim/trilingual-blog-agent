# Coding Agent Guide

## Prerequisites

Install the CLI (one-time):
```bash
uv tool install google-agents-cli
```

---

## Development Phases

### Phase 1: Understand Requirements
Before writing any code, understand the project's requirements, constraints, and success criteria.

### Phase 2: Build and Implement
Implement agent logic in `app/`. Use `agents-cli playground` for interactive testing. Iterate based on user feedback.

### Phase 3: The Evaluation Loop (Main Iteration Phase)
Start with 1-2 eval cases, run `agents-cli eval run`, iterate by making changes and rerunning it until satisfied. Expect 5-10+ iterations. Once you have a baseline, reach for `agents-cli eval compare` (regression diffs), `agents-cli eval analyze` (cluster failure modes), and `agents-cli eval optimize` (auto-tune prompts). See the **Evaluation Guide** for metrics, dataset schema, LLM-as-judge config, and common gotchas.

### Phase 4: Pre-Deployment Tests
Run `uv run pytest tests/unit tests/integration`. Fix issues until all tests pass.

### Phase 5: Deploy to Dev
**Requires explicit human approval.** Run `agents-cli deploy` only after user confirms. See the **Deployment Guide** for details.

### Phase 6: Production Deployment
Ask the user: Option A (simple single-project) or Option B (full CI/CD pipeline with `agents-cli infra cicd`).

## Development Commands

| Command | Purpose |
|---------|---------|
| `agents-cli playground` | Interactive local testing |
| `uv run pytest tests/unit tests/integration` | Run unit and integration tests |
| `agents-cli eval dataset synthesize` | Synthesize multi-turn eval scenarios for your agent |
| `agents-cli eval run` | Run the agent over the eval dataset and grade the traces |
| `agents-cli eval generate` / `agents-cli eval grade` | Decoupled form: produce traces, then grade them |
| `agents-cli eval compare` | Compare two grade-results files (regression check) |
| `agents-cli eval analyze` | Cluster failure modes from grade results |
| `agents-cli eval metric list` | List built-in metrics available in the SDK |
| `agents-cli eval optimize` | Auto-tune agent prompts using eval data |
| `agents-cli lint` | Check code quality |
| `agents-cli infra single-project` | Set up project infrastructure (Terraform) |
| `agents-cli deploy` | Deploy to dev |
| `agents-cli scaffold enhance` | Add deployment target or CI/CD to project |
| `agents-cli scaffold upgrade` | Upgrade project to latest version |

---

## Operational Guidelines for Coding Agents

- **Code preservation**: Only modify code directly targeted by the user's request. Preserve all surrounding code, config values (e.g., `model`), comments, and formatting.
- **NEVER change the model** unless explicitly asked.
- **Model 404 errors**: Fix `GOOGLE_CLOUD_LOCATION` (e.g., `global` instead of `us-central1`), not the model name.
- **ADK tool imports**: Import the tool instance, not the module: `from google.adk.tools.load_web_page import load_web_page`
- **Run Python with `uv`**: `uv run python script.py`. Run `agents-cli install` first.
- **Stop on repeated errors**: If the same error appears 3+ times, fix the root cause instead of retrying.
- **Terraform conflicts** (Error 409): Use `terraform import` instead of retrying creation.

---

## Project-specific notes: trilingual tech-blog agent

These notes describe the project as it is at version 1.0 of `docs/SPEC.md`.

- **What it is**: see `README.md` for stages, architecture and the status table. Keep the status table truthful: move an item to "run" only after it has actually been run.
- **Specification first**: `docs/SPEC.md` says what the system does; its Appendix B names the unit test for each requirement and `tests/unit/test_spec.py` keeps the two in step. Change the specification, then the code, then the test, in one commit. `docs/kr-SPEC.md` is the Korean companion and must list the same requirement ids.
- **History**: `docs/kr-AS-BUILT.md` records the code at commit `b5e1793`, before version 1, and `docs/kr-DESIGN-v1.md` is the plan whose work packages (WP-00 to WP-23) made version 1.0. Both are in Korean and both are closed; do not work through them again. New work starts from `docs/SPEC.md`.
- **Models**: `app/models.py` routes kinds of task to a fast and a reasoning model. Do not change the names without being asked. Models are called from two places only: the agents in `app/agent.py` and the gateway `generate_text` in `app/llm.py` (OPS-4); a test fails when a third place appears.
- **Control flow lives in code**: decisions (which languages run, whether a review passed, slide timing) belong in the `BaseAgent` gates in `app/workflow_agents.py`, with unit tests in `tests/unit/test_workflow_gates.py`. Do not move them into prompts.
- **Lanes**: a run has two lanes, `main` (source document, localization, posts, requests to the agent) and `slides` (plans, decks, deck text sync). `app/runs.py` owns them: one action at a time per lane, both lanes at the same time, and every action started with `runs.start` or `runs.claim` ends as done or as a notice (RUN-1, RUN-3). Do not start work with a bare `asyncio.create_task`, and do not add a check that lets an action run beside its own lane.
- **The record session**: the session of a run is its record and has one writer at a time (RUN-2). An agent turn holds the write lock of `ArticlePipeline`; code writes through `ArticlePipeline.act`, which reads the record again and merges only the entries it changed (`MERGED_KEYS` in `app/pipeline.py`). Slide planning runs in a working session per language (`ArticlePipeline.plan_slides`, `build_slide_planning_app` in `app/agent.py`) and only its result reaches the record.
- **State keys**: every session-state key is a constant in `app/state.py`. Slide plans, timing reports, decks and sync baselines are kept per language; do not add a key that holds "the" plan or "the" deck. A new dictionary keyed by language or platform goes into `MERGED_KEYS`.
- **One way to edit a post**: the title and the text of a post are changed through `apply_post_edit` in `app/tools/articles.py`, whoever changes them: the editor page (`ArticlePipeline.save_edit`) and the agent's tools `set_article_post_title` and `replace_passage_in_article_post` (PUB-11). Do not add a second path, and do not give the agent a tool that takes a whole post.
- **Publishing rules**: whether a post may be finalized or sent is decided in `app/policy.py` and nowhere else. Posting tools send only a post a person finalized (title and text), always behind `request_confirmation`, as a non-public draft unless the approval itself asks for a public post, and never to the platform a post was read from. They take no title and no visibility argument; do not add one. Naver Blog stays manual (HTML export plus URL verification). Medium is read only.
- **Nothing leaves the machine without an approval**: a post and a new file in Drive start only after a person approved them, in the dialog of a button or in the confirmation a tool pauses for (INV-1, INV-6). On the page a slide request typed to the agent goes through `request_approval_to_create_slide_decks`; the run takes the approved request and makes the slides in the slides lane.
- **The page and the script do not talk through the model**: `app/pipeline.py` calls the tool functions directly for everything that needs no judgement (`ArticlePipeline.act`). Only localization, slide planning, requests typed in words and the structure of a draft (SRC-7) go to a model.
- **What the web layer must not do**: `app/web_ui.py` holds routes. It calls no model and no platform, and it reads no meaning into what a person typed (no word lists, no regular expressions over a request, no translation of it). It starts actions of the pipeline through `app/runs.py` and refuses with `Refused` and a notice code.
- **Sources**: the kinds of address a source can be read from are the table `SOURCE_KINDS` in `app/sources.py`; the hosts that may be fetched and the sentences that name them are made from it (INV-8). Add a kind there, not a host check somewhere else. The text as supplied is kept as `source.original.md` (INV-11).
- **Catalogs**: every text the page, the editor and the previews show comes from `app/static/i18n/en.json`, `ko.json` and `ja.json`, with the same keys and placeholders in each (`tests/unit/test_i18n.py`). No sentence for people is written in `app/static/*.js`; a sentence is whole in the catalog, not put together from pieces. A sentence the server makes for the page is a code in `TEMPLATES` of `app/notices.py`, made with `notice(code, ...)`, with its text under `server.<code>` in the three catalogs. What a person wrote or typed, what the agent answered and what a platform returned are never translated.
- **Tools (OPS-5)**: a name that says what the tool does and to what; a docstring with the purpose, `Args:` for every argument and `Returns:`; type hints on every parameter and no default arguments; languages, platforms and code modes declared as `LanguageCode`, `PlatformName` and `CodeMode` from `app/schemas.py`, so the model is shown the allowed values; arguments checked through a Pydantic input model before anything happens; a dict as the result, and failures through `tool_error` / `validation_error` so the model gets an `error_code` and a `recovery_instruction`. `tests/unit/test_tool_contract.py` reads the tools from the agent tree, so a new tool is held to this without being listed.
- **Observability**: logs are structured records through `log_event` and `log_activity` in `app/observability.py`; nothing in `app/` uses `print`. An outbound call gets a `traced_span`; an action gets its root span from `app/runs.py`. Identifiers are not redacted; everything else in a record is.
- **Secrets**: only through `app/secrets.py`. Never put a credential, project id or account name in the repository; `.env` is git-ignored. A variable the code reads is named in `.env.example`.
- **Tests**: `tests/unit` stays free of model and network calls; a fixture in `tests/unit/conftest.py` refuses both, and `.env` is not read there. A test that needs a model's answer puts a stand-in at `llm._call_model` or scripts the model of an agent. Model behaviour is checked with `agents-cli eval run`, not with assertions on generated text. Before a commit: `uv run pytest tests/unit -q`, `uv run ruff check .`, `uv run ruff format --check .`; CI runs the same on every push, without credentials.
- **README table**: the table "Where each capability lives" in `README.md` is checked by `test_every_place_the_readme_points_to_exists` (OPS-7). When a file or a name it gives moves, change the table in the same commit.
- **Operating rules**:
  - Never restart a server a person is using; a restart interrupts the run that is working.
  - To look at a change, start another server on another port with a workspace and a session file of its own, for example `BLOG_AGENT_WORKSPACE_DIR=/tmp/tba-check LOCAL_SESSION_DB=/tmp/tba-check/sessions.sqlite3 uv run python -m app.web_ui --port 8503`. Give an evaluation run the same two settings.
  - Never edit a run record or a log under `workspace/.jobs/` by hand, and do not edit anything else under `workspace/`.
  - Anything that posts to a platform, creates a file in Drive or calls a real model for an evaluation is left to a person, or announced before it is run.
  - Do not regenerate `uv.lock` without being asked (item 4 of section 8 of the specification).
- **Google account for Slides**: the grant must come from a personal or dedicated account, never a work or school account, and a work account must not be added as an OAuth test user to get past "Access blocked". Keep the account check in `scripts/authorize_google_slides.py` and the account name in the deck approval hint.
