# PR•VISION

### AI Early-Warning System for Misinformation Spread

> **Which potentially harmful post is likely to spread rapidly next, how rapidly will it spread,
> why is it risky, and which post should a moderator investigate first?**

PR•VISION is a **decision-support system** for moderators, fact-checkers and Trust & Safety
teams. It does not merely classify content as fake/true — it forecasts **how fast content will
propagate in the next 30/60/120 minutes** and combines that with a **misinformation-risk
estimate** into a single **Intervention Priority Score (0–100)** with evidence-based
explanations for human review.

---

## Problem Statement

Misinformation spreads rapidly — often *before* fact-checkers or moderators can intervene.
By the time a post is reviewed, the damage is usually done. PR•VISION shifts the timeline:
it detects **early propagation dynamics** (share velocity, acceleration, engagement growth,
unique-sharer expansion, network signals) and flags content **while intervention is still
possible**.

## Solution

```
SOCIAL / DEMO DATA  →  COLLECTION  →  NORMALIZATION  →  MySQL  →  FEATURE ENGINEERING
                                                                        │
                                              ┌─────────────────────────┴───────────────┐
                                              ▼                                         ▼
                                    XGBOOST FORECAST                        MISINFORMATION RISK
                                    (additional shares,                     (TF-IDF + LogReg
                                     30/60/120 min)                          + lexicon heuristic)
                                              │                                         │
                                              ▼                                         ▼
                                        SPREAD RISK  ────────►  INTERVENTION PRIORITY (0–100)
                                                                            │
                                                                        FastAPI REST
                                                                            │
                                                                  LIVE OPS DASHBOARD
                                                                            │
                                                                     HUMAN MODERATOR
```

Key guarantees:
- **Demo data uses the exact same pipeline as real platform data** (connector → normalizer →
  DB → features → ML → score). The dashboard is never fake.
- **No fabricated metrics.** Anything a platform's official API does not expose is stored as
  `NULL` and surfaced as *unavailable* in the UI.
- **No data leakage.** Features at time `t` use only information available at `t`; training
  targets are strictly future share deltas.
- **Transparent cold start.** Without enough history the system returns a clearly-labelled
  velocity-baseline forecast with reduced confidence instead of inventing an ML prediction.
- **Decision-support only.** PR•VISION never auto-deletes, bans, or declares truth.

---

## Features

**Backend (Python 3.11+ · FastAPI · SQLAlchemy 2 · Alembic · MySQL)**
- Modular architecture: `api/ core/ db/ schemas/ services/ connectors/ ml/` — no monolith `main.py`
- 8 database entities with foreign keys, unique + composite indexes, connection pooling
- Async ingestion scheduler: per-platform loops, configurable interval, exponential backoff,
  rate-limit handling, full failure isolation, `data_source_status` bookkeeping
- Official-API connectors for **X (v2), Reddit (OAuth), Instagram & Facebook (Graph API v21),
  LinkedIn** + a realistic **Demo data provider** (5 behaviour archetypes)
- XGBoost forecasting (one model per horizon), chronological train/val/test split,
  evaluation vs the velocity baseline, versioned model registry with metrics
- Misinformation-risk component (TF-IDF + LogisticRegression on a documented synthetic corpus,
  blended with a transparent lexicon heuristic) — labelled *estimates*, never truth verdicts
- Intervention Priority Score with **configurable weights** and explanations built only from
  observed features
- 17+ REST endpoints with OpenAPI/Swagger docs, pagination, filtering, sorting
- 60 pytest tests: feature maths, scoring, normalization, API contracts, ML behaviour

**Frontend (HTML5 · CSS3 · vanilla JS · Chart.js — zero build step)**
- Dark "Trust & Safety Operations Center" aesthetic: glassmorphism, animated KPI counters,
  pulsing CRITICAL badges, smooth transitions
- Live KPIs (posts monitored, critical alerts, high-risk, predicted shares, average risk)
- Intervention priority queue with platform/priority/search filters and status badges
- Live share-velocity chart (30m/1h/2h windows), forecast chart (actual vs predicted),
  canvas propagation-network diagram
- Post investigation drawer: content, metrics (with honest *unavailable* fields), growth
  signals, 30/60/120m forecasts, risk breakdown, explanation bullets, top factors
- 8-second auto-refresh (polls; pauses on hidden tabs)

**Ops**
- Dockerfile (python:3.12-slim, non-root, healthcheck) + docker-compose (FastAPI + MySQL 8)
- Alembic migration chain valid on both MySQL and SQLite
- `.env.example`, `.gitignore`, Swagger docs at `/docs`

---

## Tech Stack

| Layer | Technology |
|---|---|
| Frontend | HTML5, CSS3, vanilla JavaScript, Chart.js 4 (vendored locally) |
| Backend | Python 3.11+, FastAPI, Uvicorn, Pydantic v2, asyncio, httpx |
| Database | MySQL 8 (primary) · SQLite (dev/demo fallback) via SQLAlchemy 2 + Alembic |
| ML | XGBoost, scikit-learn, pandas, NumPy, joblib |
| Testing | pytest (+ pytest-asyncio) |
| Deployment | Docker, docker-compose, cloud-VM ready |

---

## Installation

### 1. Python setup

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/macOS:
source .venv/bin/activate

pip install -r backend/requirements.txt
```

### 2. MySQL setup

```sql
CREATE DATABASE prvision CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'prvision'@'localhost' IDENTIFIED BY 'your-password';
GRANT ALL PRIVILEGES ON prvision.* TO 'prvision'@'localhost';
FLUSH PRIVILEGES;
```

### 3. Environment setup

```bash
cp .env.example .env
# edit .env:
#   MYSQL_PASSWORD=your-password
#   (platform credentials optional — leave blank to run demo mode)
```

> **No MySQL available?** Set `DB_ENGINE=sqlite` in `.env` to run the entire pipeline on a
> local SQLite file — ideal for hackathon demos and CI. Production uses MySQL.

### 4. Database migration (Alembic)

```bash
cd backend
alembic upgrade head
```

### 5. Demo mode (no social-media credentials needed)

```bash
# from the project root — seeds 10 posts (2 per archetype) through the REAL pipeline:
python scripts/seed_demo_data.py            # --posts 15 / --archetypes viral normal …

# train the forecasting + misinformation models on the ingested data:
python scripts/train_models.py
```

### 6. Model training (any time)

```bash
python scripts/train_models.py
# or via the running API:
curl -X POST http://localhost:8000/api/ml/train
```

### 7. Running the backend

```bash
cd backend
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

The frontend is served by FastAPI at **http://localhost:8000/** — no separate server needed.

### 8. API documentation

- Swagger UI: **http://localhost:8000/docs**
- ReDoc: http://localhost:8000/redoc
- OpenAPI schema: http://localhost:8000/openapi.json

### 9. Testing

```bash
cd backend
pytest tests/ -v                # 60 tests: unit + API + ML
```

### 10. Live demo ingestion (optional)

Start background polling so the dashboard updates itself:

```bash
curl -X POST localhost:8000/api/ingestion/start \
     -H 'Content-Type: application/json' \
     -d '{"platforms":["demo"],"interval_seconds":30}'
```

---

## Docker

Two ready-made stacks (see **DEPLOYMENT.md** for step-by-step hosting guides —
Railway / Render / Fly.io / VPS — and the full environment-variable reference):

```bash
# Single container — SQLite + embedded sidecar (quickest, great for demos
# and one-service hosts like Railway/Render/Fly.io):
docker compose -f docker-compose.lite.yml up -d --build

# Full stack — FastAPI + MySQL 8 + dedicated sidecar service (VPS/production):
cp .env.example .env          # set MYSQL_PASSWORD, SECRET_KEY etc.
docker compose up -d --build
```

- `api` runs Alembic migrations → seeds/trains models → starts Uvicorn on :8000
- The image embeds the **provider sidecar** (Node 20) that powers real-platform
  web-harvest (X/Reddit/Instagram/Facebook/LinkedIn) and web-search/LLM/vision
  evidence — auto-started by the entrypoint, or run standalone via `SIDECAR_EMBEDDED=false`
- `mysql` (8.4) with utf8mb4, healthcheck, persistent volume; model artifacts
  persist in the `models-data` volume
- No secrets in the Compose files — everything comes from `.env` / platform vars

## Production Deployment (cloud VM)

```bash
# on the VM (Docker installed, ports 80/443 open)
git clone <repo> && cd pr-vision
cp .env.example .env && nano .env          # real MySQL password, CORS origins, creds
docker compose up -d --build
docker compose logs -f api                 # verify: "PR•VISION x.y.z ready"

# reverse proxy (nginx/Caddy/ALB) → http://127.0.0.1:8000
# health probe: GET /api/health
```

Scaling notes: Uvicorn runs 2 workers in the container; the ML artifacts are
read-only after training, so workers stay consistent. For >2 workers, train once
(`POST /api/ml/train`) then keep workers read-only.

---

## Project Structure

```
PR-VISION/
├── frontend/                  # vanilla HTML/CSS/JS dashboard (served by FastAPI)
│   ├── index.html
│   ├── css/  styles · dashboard · responsive
│   └── js/    api · utils · charts · posts · alerts · dashboard (+ vendor/Chart.js)
├── backend/
│   ├── app/
│   │   ├── main.py            # app factory, lifespan, static serving
│   │   ├── api/routes/        # health · platforms · posts · predictions · dashboard · ingestion · ml
│   │   ├── core/              # config (pydantic-settings) · logging (secret redaction)
│   │   ├── db/                # database.py · models/ · repositories/
│   │   ├── schemas/           # pydantic contracts
│   │   ├── services/          # ingestion · feature · prediction · scoring · demo
│   │   ├── connectors/        # base · x · reddit · instagram · facebook · linkedin · demo
│   │   └── ml/                # feature_engineering · forecasting · misinformation · training · inference · evaluation
│   ├── alembic/               # migrations (MySQL + SQLite)
│   ├── tests/                 # unit/ · api/ · ml/  (60 tests)
│   └── requirements.txt
├── ml/
│   ├── models/                # versioned artifacts + registry.json
│   └── datasets/              # demo DB / exports
├── scripts/
│   ├── seed_demo_data.py      # seed through the REAL pipeline
│   └── train_models.py        # XGBoost + misinfo training
├── docs/
│   ├── architecture.md        # system design + Mermaid diagrams
│   ├── ml-pipeline.md         # features, targets, splits, metrics, explainability
│   └── api-integrations.md    # honest per-platform capability matrix
├── .env.example
├── .gitignore
├── Dockerfile
├── docker-compose.yml
└── README.md
```

---

## API Overview

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | Real system state (DB, models, ingestion) |
| GET | `/api/platforms` | Connector status — configured vs not_configured |
| GET | `/api/posts` | Monitored posts (filter by platform/search/time, paginated) |
| GET | `/api/posts/{id}` | Post detail |
| GET | `/api/posts/{id}/metrics` | Raw metric time-series (window filtering) |
| GET | `/api/posts/{id}/propagation` | Reshare cascade edges (empty where API hides them) |
| GET | `/api/posts/{id}/features` | Engineered feature history |
| GET | `/api/posts/{id}/prediction` | Full forecast + risks + priority + explanation |
| GET | `/api/posts/{id}/intervention-score` | Latest stored intervention score |
| GET | `/api/dashboard/summary` | KPI counters (computed live) |
| GET | `/api/dashboard/trending` | Fastest-spreading posts |
| GET | `/api/dashboard/high-priority` | Moderator queue (label/platform filters) |
| POST | `/api/demo/generate` | Generate demo posts through the full pipeline |
| GET | `/api/demo/archetypes` | Describe the 5 demo behaviours |
| POST | `/api/ingestion/start` / `stop` | Background polling control |
| GET | `/api/ingestion/status` | Scheduler state |
| POST | `/api/ml/train` | Train forecast + misinformation models |
| POST | `/api/ml/predict` | Re-score a post |
| GET | `/api/ml/status` | Model registry status (versions, metrics, loaded) |

---

## Troubleshooting

| Symptom | Cause & fix |
|---|---|
| `DB_ENGINE=mysql` fails to connect | MySQL not running/credentials wrong — verify with `mysql -u prvision -p`; or use `DB_ENGINE=sqlite` for a quick demo |
| Health shows `forecast_model: not_loaded` | Models not trained yet — run `python scripts/train_models.py` (needs seeded data) |
| `Horizon 120m skipped (need ≥60 rows)` | Not enough long-lived posts — seed more data (`--posts 20`) so `t+120m` anchors exist |
| Predictions all `prediction_type: baseline` | Fewer than 3 snapshots per post (cold start) — wait for ingestion cycles or seed again |
| Dashboard empty on first load | The UI auto-generates demo data on first run; or `POST /api/demo/generate` manually |
| `alembic` errors on SQLite | Use the batch-mode migrations as shipped (they are dialect-aware); do not hand-edit |
| Port 8000 busy | `uvicorn ... --port 8001` and adjust any proxy |

---

## API Limitations (honesty by design)

Full per-platform capability matrix: **[docs/api-integrations.md](docs/api-integrations.md)**.

- X: reshare *graph* and unique sharers are not available on standard API tiers → propagation
  events empty, `unique_sharers = NULL`.
- Reddit: no view counts, no repost network; `score` is net upvotes (used as like analogue);
  `num_crossposts` used as the share analogue where present.
- Instagram: feed posts expose no share counts; views require eligible media insights;
  likes require the business-account relation.
- Facebook: per-post views unavailable; no reshare graph.
- LinkedIn: total share counts removed from the official API; impressions are partner-only.
- Misinformation risk is a **stylistic estimate** trained on a documented synthetic corpus —
  it is not a truth judgment and must inform, never replace, human review.

---

## Ethics

PR•VISION outputs **forecast · risk · priority · evidence · explanation** — inputs for human
decisions. It intentionally avoids automated takedowns, bans, or truth claims, and labels all
demo data as demo.

---

## Production Upgrade — Accounts, Cases, Alerts, Exports & Security

The platform was extended to full production quality. Everything below is
implemented and tested — nothing is a placeholder.

### Authentication (spec: premium website + auth)

| Endpoint | Method | Description |
|---|---|---|
| `/api/auth/register` | POST | Create account (email, display_name, password ≥8 chars w/ letter+digit). First account becomes `admin`. |
| `/api/auth/login` | POST | Bearer-token login |
| `/api/auth/logout` | POST | Revokes the presented session token |
| `/api/auth/me` | GET | Current profile |

- Passwords: **PBKDF2-HMAC-SHA256, 390k iterations, per-user salt** (stdlib, no raw secrets stored)
- Sessions: 32-byte random tokens; **only SHA-256 hashes are stored** (a DB dump cannot be replayed); revocable; expiry via `AUTH_TOKEN_EXPIRE_HOURS` (default 72h)
- Seeded demo account: **demo@prvision.ai / DemoVision!2026** (role admin; disable with `SEED_DEMO_USER=false`)
- Frontend: premium landing page (`/`), login (`/login`), register (`/register`); token kept in `localStorage["prv_token"]` and sent as `Authorization: Bearer …`

### Investigation cases & notes (spec #14)

| Endpoint | Method | Description |
|---|---|---|
| `/api/cases` | POST/GET | Save a completed verification as a case (auth) / list cases |
| `/api/cases/{id}` | GET/PATCH/DELETE | Detail incl. notes / status & title updates (owner or admin) / delete |
| `/api/cases/{id}/notes` | POST | Add investigator note (auth) |

UI: `/cases` workspace page + "Investigation Workspace" panel on every report page
(save-as-case, link to the case manager). Cases snapshot verdict + priority at save time.

### Alert engine (spec #13)

Five automatic triggers evaluated continuously from stored scores/verification
artifacts (never fabricated): `misinfo_risk`, `acceleration_spike`, `forecast_jump`,
`evidence_conflict`, `media_signal`. Severity `LOW|MEDIUM|HIGH|CRITICAL`; repeat
alerts for the same condition are de-duplicated (45 min window).

| Endpoint | Method | Description |
|---|---|---|
| `/api/alerts` | GET | List (filter `severity`, `acknowledged`) |
| `/api/alerts/summary` | GET | Unacknowledged counts by severity |
| `/api/alerts/{id}/ack` | POST | Acknowledge (auth) |

UI: "Live Alert Feed" panel on the dashboard with ack buttons + report deep-links.

### Report export (spec #19)

| Endpoint | Description |
|---|---|
| `/api/verify/{id}/export.pdf` | Investigator PDF (reportlab, Noto font for CJK safety) |
| `/api/verify/{id}/export.json` | Full report + Limitations & provenance |
| `/api/verify/{id}/export.csv` | Claims/evidence/fact-checks/numerical checks (long format) |

Every export embeds a **Limitations** section (provider states, fetch warnings,
uncertainty statements). Download buttons live on the report page.

### Security hardening (spec #17)

- **SSRF guard**: every URL (and each redirect hop) is resolved and validated with
  the `ipaddress` module — private/loopback/link-local/CGNAT/multicast/IPv6-mapped
  addresses refused; embedded credentials refused; redirects followed manually and
  re-validated per hop
- **robots.txt honored**: disallowed paths return `fetch_status=robots_blocked`
- **Upload validation**: extension allowlist + magic-byte sniffing (a `.png` that is
  not a PNG is refused 415) + size cap (`VERIFY_UPLOAD_MAX_MB`); uploads persisted to `ml/uploads/`
- **Rate limiting**: in-process sliding window (`RATE_LIMIT_*_PER_MINUTE`; auth 15/min,
  verify 12/min, export 30/min, general 240/min) with honest `429 + Retry-After`;
  disable with `RATE_LIMIT_ENABLED=false`
- **Security headers**: CSP, X-Frame-Options DENY, nosniff, Referrer-Policy, Permissions-Policy
- **Audit log**: register/login/logout, case mutations, alert acks and exports are recorded (`audit_logs`)
- XSS: all API data is HTML-escaped in the frontend; CSP as defense-in-depth

### Model registry (DB-backed)

`model_versions` table mirrors `ml/models/registry.json` at startup (spec #15) —
query via `GET /api/ml/status` and the DB; the JSON file remains the source cache.

### Page map

| Path | Page |
|---|---|
| `/` | Landing page (hero network animation, pipeline, capabilities) |
| `/dashboard` | Early-warning operations dashboard |
| `/verify` | VERIFY ANYTHING — 7 input modes |
| `/report/{job_id}` | Evidence-first investigation report + workspace |
| `/cases` | Investigation cases workspace |
| `/login`, `/register` | Auth |
| `/docs` | OpenAPI/Swagger |

### Rate-limit & deployment notes

The rate limiter is single-process (in-memory). Running multiple uvicorn workers
(Docker compose uses 2) means per-process limits; put a shared limiter (Redis) in
front if you need cluster-wide budgets. In tests set `RATE_LIMIT_ENABLED=false`.
