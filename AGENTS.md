# AGENTS.md — QuantDinger

Local-first, private AI-driven quantitative trading workspace. This open-source tree contains the Flask backend, the MCP server, deployment stacks, and docs. The web/mobile UI source lives in the **private QuantDinger-Vue repo** — never look for Vue code here; its image (`ghcr.io/openbyteinc/quantdinger-frontend`) is pulled by the Compose files.

## Layout

- `backend_api_python/` — Flask API. `app/` holds `routes/` (Blueprints), `services/` (business logic, incl. `live_trading/` broker adapters), `data_sources/` + `data_providers/` (market data adapters), `openapi/` (flask-smorest schemas), `config/`, `utils/` (db, auth, cache), `tasks/`, `workers/`. `migrations/` is raw SQL schema/seed. `scripts/` holds backend CI guardrails.
- `mcp_server/` — published PyPI package `quantdinger-mcp` (stdio MCP server for the Agent Gateway). Deps are deliberately minimal (`mcp` + `httpx`).
- `docs/` — `architecture/`, `agent/` (MCP/agent gateway docs + `agent-openapi.json`), `api/openapi.yaml`, `deployment/`, `trading/` (strategy dev guide, live-trading safety).
- `scripts/` — repo-level CI guards: `check_docs.py`, `check_mojibake.py`, `check_version.py`.
- `ops/` — Prometheus/Grafana/Alertmanager configs.
- `docker-compose*.yml` — deployment stacks (`ghcr` = zero-clone deploy; `build` = local frontend build override; `observability`, `production` overlays).

## Read by task

Read only the documentation relevant to the change:

- Agent Gateway or MCP: [Agent documentation](docs/agent/README.md), [Agent OpenAPI](docs/agent/agent-openapi.json), and [MCP package documentation](mcp_server/README.md).
- Strategy API, backtests, or trading behavior: [Strategy development guide](docs/trading/STRATEGY_DEV_GUIDE.md) and [live-trading safety](docs/trading/LIVE_TRADING_SAFETY.md).
- Runtime ownership, concurrency, Kafka, workers, or durable state: [architecture index](docs/architecture/README.md) and the task-specific document it links.
- HTTP contracts: [API conventions](docs/architecture/API_CONVENTIONS.md) and the applicable OpenAPI document.
- Installation or operations: the root [README](README.md) and the applicable guide under `docs/deployment/`.

## Commands (mirror CI in `.github/workflows/`)

All backend commands run from `backend_api_python/`:

```bash
ruff check app scripts tests                      # critical-errors-only lint (E9,F63,F7,F82; line-length 120, py312)
python -m compileall -q app scripts tests         # syntax check
python scripts/backend_quality_check.py           # structure guardrail vs scripts/backend_quality_baseline.json
python scripts/check_requirements_lock.py         # every direct dep in requirements.txt must be pinned in requirements.lock
python -m pytest tests -m "not integration and not stress" -q
python -m pytest tests/release_gate -q            # release gates run separately in CI
QD_PROCESS_ROLE=migration python -m app.commands.migrate
python scripts/export_openapi.py                  # regenerate docs/api/openapi.yaml after openapi/ route changes
```

Backend tests require **PostgreSQL 18 + Redis** (CI provisions both as services; env in `basic-ci.yml`: `DATABASE_URL`, `REDIS_HOST`, `CACHE_ENABLED=false`, `SKIP_STARTUP_HOOKS=1`). There is no SQLite fallback. Tests marked `integration`/`stress` need live testnet keys and are skipped by default.

MCP server (from repo root, tested on Python 3.10/3.12/3.13):

```bash
python -m pytest mcp_server/tests -q
python -m pip install -e './mcp_server[dev]'
```

Repo-root guards (CI runs these; run before committing doc/version changes):

```bash
python scripts/check_docs.py      # docs structure + link validation
python scripts/check_version.py   # root VERSION must match backend_api_python/VERSION
python scripts/check_mojibake.py  # tracked-text encoding check
```

## Architecture boundaries

Read `docs/architecture/MODULE_BOUNDARIES.md`, `ARCHITECTURE.md`, `EXTENSION_GUIDE.md`, and `API_CONVENTIONS.md` before larger backend changes. Key rules:

- **Routes stay thin**: validate input → call a service → shape the response. No background threads, no exchange-specific order sizing, no multi-step transactions inline.
- **Services** take plain Python values (not Flask `request`), return plain dicts/dataclasses, and define idempotency when mutating state.
- **Adapters** (`services/live_trading/`, `data_sources/`) normalize external APIs; they must not know about Flask, users, or frontend response shapes.
- **Don't grow legacy hotspot files** (`app/routes/strategy.py`, `app/__init__.py`, `services/trading_executor.py`, `services/backtest.py`, etc. — see the baseline table in `MODULE_BOUNDARIES.md` and `backend_quality_baseline.json`). Put new behavior in a focused sibling module.
- Adding a data source: `app/data_sources/<name>.py` + register in `data_sources/factory.py`. Adding an exchange: `app/services/live_trading/<exchange>.py` inheriting `BaseLiveTrading` + register in `live_trading/factory.py`.

## Runtime and contract invariants

- Preserve the process ownership documented in the architecture index. Do not move persistent trading loops into the HTTP backend or let evaluator processes submit exchange orders directly.
- PostgreSQL is the durable source of truth. Kafka transports versioned events; Redis cache data is evictable, while the jobs Redis has a separate durability role. Do not silently substitute one for another.
- Keep tenant isolation, idempotency, leases, fencing, and audit behavior intact when changing distributed or trading workflows.
- MCP capabilities must retain the same authentication, scopes, idempotency, limits, and live-trading safeguards as the backing API.

## Two API surfaces (OpenAPI is the contract SSOT)

- **Human Web API** `/api/...` — user JWT auth; response envelope `{code, msg, data}` with `code: 1` = success; spec `docs/api/openapi.yaml` from `app/openapi/` (flask-smorest).
- **Agent Gateway** `/api/agent/v1/...` — agent tokens (`qd_agent_...`, scoped R/W/B); spec `docs/agent/agent-openapi.json`. Don't mix agent routes into the human spec without an `x-agent-only` tag. Update the agent spec and its alignment tests whenever that HTTP contract changes.
- `mcp_server/src/quantdinger_mcp/tool_contract.py` mirrors `app/routes/agent_v1/` and `app/services/ai_tool_registry.py`. MCP CI triggers on all three paths — keep them in sync in the same PR. Write-risks are declared in `WRITE_TOOLS`; MCP errors must be returned as MCP error results (see `mcp_server/tests/`).
- `openapi-ci.yml` runs Spectral lint, export-diff, and oasdiff breaking-change checks. Read `docs/architecture/API_CONVENTIONS.md` before adding public endpoints.

## Safety and repository hygiene

- Never commit real secrets, production `.env` files, API keys, or database passwords. Use `.env.example` and placeholders.
- Do not weaken live-trading safeguards or bypass explicit authorization and human review unless the user requests and scopes that change.
- Do not add upgrade-time data rewrites for one-off local cleanup. Use an explicit, reviewed migration only when shipped user data must change.
- Preserve unrelated working-tree changes.
- Keep machine-readable contracts and identifiers in English. When a human guide has English and `_CN` editions, update both when the change affects both audiences.

## Verification

- Run focused backend tests from `backend_api_python/` with `python -m pytest tests/<test_file>.py -q`.
- Agent contract coverage lives in `backend_api_python/tests/test_ai_agent_contract_alignment.py` and related `test_agent_*.py` files.
- MCP tests live in `mcp_server/tests/`.
- Validate Compose changes with `docker compose config --quiet` before exercising the affected services.
- Match verification depth to risk; trading, migrations, tenancy, and distributed ownership require targeted regression tests.

## Conventions & gotchas

- Comments, docstrings, and log messages in **English** (except user-facing translations or external provider fields).
- Commits: conventional style (`fix:`, `feat:`, `chore:`, `docs:`). Branches: `fix/`, `feat/`, `docs/`, `chore/`.
- Docs are bilingual (`README.md` + `README_CN.md`); `check_docs.py` enforces structure and only allows those two files at `docs/` root. Keep all tracked text valid UTF-8 (`check_mojibake.py` fails CI on mojibake).
- Versioning: root `VERSION` and `backend_api_python/VERSION` must agree (`check_version.py`). `quantdinger-mcp` is versioned/released independently in `mcp_server/pyproject.toml`.
- `ruff` here only checks critical errors — passing it does not mean style-clean, but never introduce new failures.
- Release Docker images take their version from the git tag; `VERSION` is only the local/dev fallback.
- Backend env comes from `backend_api_python/env.example`; `SECRET_KEY` (32+ bytes) is mandatory. Worker startup is disabled via `SKIP_STARTUP_HOOKS=1` for tests, OpenAPI export, and one-off scripts.
