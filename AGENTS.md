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

- **What it is**: see `README.md` for stages, architecture and the status table. Keep the status table truthful: move an item to "run" only after it has actually been run.
- **Models**: `app/models.py` routes tasks to a fast and a reasoning model. Do not change the names without being asked.
- **Control flow lives in code**: decisions (which languages run, whether a review passed, slide timing) belong in the `BaseAgent` gates in `app/workflow_agents.py`, with unit tests in `tests/unit/test_workflow_gates.py`. Do not move them into prompts.
- **Publishing rules**: publishing tools create drafts only, always behind `request_confirmation`, and never write back to the platform a post was fetched from. Naver Blog stays manual (HTML export plus URL verification).
- **Tools**: type hints on every parameter, no default arguments, return a dict; failures go through `tool_error` / `validation_error` in `app/schemas.py` so the model gets an `error_code` and a `recovery_instruction`.
- **Secrets**: only through `app/secrets.py`. Never put a credential, project id or account name in the repository; `.env` is git-ignored.
- **Tests**: `tests/unit` must stay free of model and network calls. Model behaviour is checked with `agents-cli eval run`, not with assertions on generated text.
- **Google account for Slides**: the grant must come from a personal or dedicated account, never a work or school account, and a work account must not be added as an OAuth test user to get past "Access blocked". Keep the account check in `scripts/authorize_google_slides.py` and the account name in the deck approval hint.
