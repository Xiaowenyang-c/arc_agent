# Requirement Compiler — ARC-Bench agent

Compile `requirements.yaml` and its reference images into a running React + Express + SQLite
application through 13 passes: requirements and visual evidence, database schema and obligations,
backend API contracts and tests, UI design, then frontend triggers with browser acceptance.
Intermediate artifacts are written to `<output>/.arc/`, and each pass records a Git checkpoint
inside the output directory.

This repository is the uploaded **agent**, not a generated application: the evaluator runs it to
produce an app, then tests that app.

## Upload contract

The runner starts the agent like this, from the agent root:

```bash
python main.py <requirement directory> --output-dir <application output directory>
```

| Requirement | Where it is satisfied |
| --- | --- |
| `main.py` and `requirements.txt` at the archive root | `scripts/package.py` verifies both, then re-opens the ZIP |
| Read the requirement directory supplied by the runner, never a fixed `input/` | `main.py` resolves the positional path; nothing reads `input/` at run time |
| Write the app to the output directory only | `Compiler` owns `output_dir`; the repo stays untouched |
| `OPENAI_BASE_URL` / `OPENAI_API_KEY` from the environment only | `infrastructure/shared/models/text.py`, `vision.py` |
| No model name required from the platform | `DEFAULT_MODEL = "deepseek-flash"`, used when `MODEL` is unset |
| Never override injected values with a local `.env` | `load_dotenv` (non-overriding) loads `.env` only when it exists; the archive ships none |
| Output has no nested `templates/` directory | Pass 00 copies `infrastructure/shared/project/templates/*` to the output root |
| `deploy.sh` kept, service uses the platform `PORT` | `templates/deploy.sh`, `templates/backend/src/server.ts` (`process.env.PORT`) |

## Package and upload

```bash
python scripts/package.py            # -> dist/arc_agent.zip  (main.py + requirements.txt at the root)
```

The script excludes `.git`, `.venv`, `node_modules`, `output*/`, `dist/`, `.arc/`, caches, and any
`.env`, then fails if the archive violates the contract above.

Upload at arc-bench.com → Competitions → **Smoke Competition** → upload your own agent → language
**Python** → the ZIP → submission name, Base URL, API Key. Uploading does not start a run: open the
task and press **Run**. Only the team captain can submit.

## Run locally

Python 3.11+ and Node.js 22.12+ with npm are required. `npm` must be able to build native modules
(`better-sqlite3`): on Windows install Visual Studio Build Tools with **Desktop development with
C++**; on Debian/Ubuntu install `python3 build-essential`.

```bash
python -m venv .venv
.venv/Scripts/activate            # Windows;  source .venv/bin/activate elsewhere
pip install -r requirements.txt

cp .env.example .env              # then set OPENAI_API_KEY, OPENAI_BASE_URL, MODEL
python main.py ./input --output-dir ./outputs/demo
```

Pass 01 downloads Chromium through the generated project's `@playwright/test`; the first run needs
network access.

Start the generated app:

```bash
cd outputs/demo
bash deploy.sh                    # npm install, build, then start on $PORT (default 3000)
```

Open `http://localhost:3000`.

Useful flags: `--model-log` shows and saves every model request; `--port 33000` changes the
generated backend port (defaults to `$PORT` when the runner supplies one). Token usage and latency
are always printed. Pass 12 accepts `--max-turns`, `--max-tests` and `--acceptance-turns`
(default 4) to bound the most expensive repair loop.

### Run one pass at a time

The numbered scripts call the same passes with the same defaults; pass 01 needs `--no-browse` to stay
non-interactive.

```bash
python 00_project_initialization.py --input input --output outputs/demo
python 01_explore_requirement.py --input input --output outputs/demo --no-browse
python 02_build_db_schema.py --input input --output outputs/demo
# ... 03 through 12 follow the same pattern
```

## What this project changed relative to the upstream repository

| Change | Why |
| --- | --- |
| `DEFAULT_MODEL` fallback in `text.py`, `vision.py` | The platform injects only the base URL and key; a missing `MODEL` used to abort every pass. |
| Per-image failure tolerance in `01_explore_requirement.py` | `visual_analysis.json` is hard-required by passes 09–12, so one unsupported image no longer fails five passes. |
| `--port` defaults to `$PORT` in `main.py` | Keeps the generated app aligned with the runner's port contract. |
| `check_endpoint_uniqueness` in `stages/backend/primitives.py` + one-endpoint rule in pass 06 | One smoke run spent four of six acceptance repair rounds reconciling `/api/register` with `/api/register-traveler` for the same operation. Duplicate endpoints are now rejected at design time. |
| Acceptance budgets and `stop_on_repeat` in `agent/loop.py`, `triggers/acceptance.py` | Acceptance repair inherits its own 4-round budget instead of 10, and a loop whose feedback repeats stops immediately instead of repeating the same failure. |
| Permitted-path list in the `READ_NOT_ALLOWED` feedback | Repeated "cannot read this file" rejections each cost a round; the rejection now names what may be read. |
| `readable_note()` appended to both acceptance prompts | The rejection list arrived after the fact and the agent kept asking; naming the readable paths up front stops the loop instead of paying for it. |
| Shared-ownership allowance in `coverage()` and `validate_coverage()` | Excess writes used to be measured against one requirement, which made extending an endpoint for a later requirement impossible and pushed the model into forking a second path. Excess is now measured against every requirement that shares the API; `missing` stays per requirement. |
| One endpoint, several owners in pass 06 | The prompt now says to keep one API, extend it, and list its id for both requirements, because the coverage check accepts the union. |
| Acceptance asserts one mutating request, not a borrowed path | Two suites waiting on different paths for one form forced mirrored submissions; the path is an implementation choice, the single-operation outcome is the requirement. |
| Repair gate runs the failing suite first | Replaying every accepted suite on each failed round doubled the cost of a loop that usually fails for the same reason; the full set still runs before the round is accepted. |
| Acceptance budgets default to review 1 / repair 2 | Measured convergence is 2–3 rounds; later rounds only restated the same failure. |
| `scripts/package.py` rewritten without Git | Produces and self-verifies the upload archive on any machine. |
| Development-only material removed (`tests/`, `assets/`, `uv.lock`, empty `arc-bench/`) | Smaller, cleaner archive; none of it is imported by the pipeline. |

## Code map

Numbered scripts orchestrate each pass. Domain logic lives in `infrastructure/stages/`; shared agent
and project services live in `infrastructure/shared/`.

| Module | Entry | Purpose |
| --- | --- | --- |
| Pipeline | [main.py](main.py), [compiler.py](infrastructure/compiler.py) | Configuration, pass order, and reports. |
| Requirements · 00–01 | [requirements/](infrastructure/stages/requirements/), [project/](infrastructure/shared/project/) | Requirement tree, visual evidence, project initialization. |
| Database · 02–05 | [database/](infrastructure/stages/database/) | Schema, obligations, DDL, and seed data. |
| Backend · 06–08 | [backend/](infrastructure/stages/backend/) | API contracts, HTTP tests, and implementation. |
| Frontend · 09–12 | [ui/](infrastructure/stages/frontend/ui/), [triggers/](infrastructure/stages/frontend/triggers/) | UI design, interactions, and browser acceptance. |
| Agent loops | [proposals.py](infrastructure/shared/agent/proposals.py), [loop.py](infrastructure/shared/agent/loop.py) | Model requests, file edits, checks, and repair. |
| Runtime services | [project/](infrastructure/shared/project/), [presentation/](infrastructure/utils/presentation/) | Templates, Git checkpoints, logs, and execution evidence. |

A failing pass is reported and the pipeline continues, so a partial application is still produced;
`.arc/compilation.json` records every pass status with its error and traceback. The design rationale
is in [principle.md](principle.md).
