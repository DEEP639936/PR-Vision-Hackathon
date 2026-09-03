# PR•VISION — Public Deployment Guide

This guide gets your PR•VISION instance running on a **public URL that anyone
can open** — not just the sandbox preview.

> **Deploying on the Z.ai platform itself?** Since the publish-shell was added
> (see **`../PLATFORM-DEPLOY.md`** at the project root), the platform's
> one-click **Publish** button now deploys PR•VISION natively: click Publish,
> and the app goes live at `https://<your-name>.space-z.ai`. The guides below
> are for hosting **outside** the platform (persistent, self-managed).

> **Why did Publish fail before?** The pipeline builds Next.js-style projects
> and couldn't run a raw Python app. The root-level shell (Next front door +
> mini-service backend runner + Caddy routing) makes the project natively
> deployable — see `PLATFORM-DEPLOY.md` for the architecture.
>
> **Trained models on the published instance:** the platform runtime is
> intentionally slim (no numpy/xgboost — they made Publish stall). The bundled
> TRAINED models still serve there through the portable exported-weights
> engine (`backend/app/ml/portable.py`), verified against the native models by
> `scripts/validate_portable_parity.py` (forecast relative diff < 1e-4; the
> TF-IDF/LogReg risk model is float64-exact). `/api/ml/status` reports
> `engine: "portable"`, and `/api/health` reports `forecast_model: "loaded"` —
> no "models not trained" banner. Re-training still needs the full runtime
> (Docker below); after any retraining, re-export with
> `python scripts/export_portable_models.py` (also run automatically by
> `scripts/train_models.py`).

---

## What you can deploy

| Asset | Purpose |
|---|---|
| `Dockerfile` | Single image: FastAPI API + ML models + **embedded provider sidecar** (Node) |
| `docker-compose.lite.yml` | **One container**, SQLite, embedded sidecar — the easiest path |
| `docker-compose.yml` | API + MySQL 8 + dedicated sidecar service — for VPS / production |
| `sidecar/Dockerfile` | Standalone provider sidecar (when you don't want it embedded) |

**Recommended by situation**

| Situation | Use |
|---|---|
| Hackathon demo, easiest possible hosting | **Railway** (below) with `docker-compose.lite.yml` semantics |
| Free hosting, short-lived demos OK | **Render free tier** (sleeps + ephemeral disk — caveats below) |
| Production / long-term | **VPS + docker-compose.yml** (MySQL, volumes, full control) |

---

## Before you deploy — prepare the repo

```bash
unzip pr-vision-complete.zip -d pr-vision && cd pr-vision
git init && git add . && git commit -m "PR•VISION initial deploy"
# Create an empty repo on GitHub (github.com/new), then:
git remote add origin https://github.com/<you>/pr-vision.git
git push -u origin main
```

All platforms below can deploy **from this Git repo** (or upload the zip on a VPS).

---

## Option A — Railway (recommended, ~5 minutes)

Railway runs Docker images natively and gives you a persistent volume + public
HTTPS URL.

1. Go to **railway.app** → *New Project* → *Deploy from GitHub repo* → pick `pr-vision`.
2. In the service → **Settings**:
   - **Builder**: Dockerfile (auto-detected)
   - **Networking → Generate Domain**: you get `https://<name>.up.railway.app`
   - Set the **start command** (Settings → Deploy):
     ```bash
     sh -c "alembic upgrade head && python ../scripts/seed_demo_data.py --posts 12 || true; (python ../scripts/train_models.py || true) && uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 2"
     ```
3. **Variables** tab — add (minimum viable set):
   ```
   APP_ENV=production
   DB_ENGINE=sqlite
   SQLITE_PATH=/data/prvision.db
   INGESTION_ENABLED_ON_STARTUP=true
   SECRET_KEY=<run: python3 -c "import secrets; print(secrets.token_urlsafe(48)")>
   SEED_DEMO_USER=true
   SIDECAR_ENABLED=true
   SIDECAR_EMBEDDED=true
   SIDECAR_URL=http://127.0.0.1:8787
   ```
4. **Volumes**: add a volume mounted at `/data` (keeps the SQLite DB across deploys).
5. Redeploy. First boot: migrations run → demo seed → models train → sidecar starts →
   ingestion loops come up (demo + web-harvest + HackerNews + Mastodon).
6. Open your Railway URL → log in with `demo@prvision.ai / DemoVision!2026`
   (register your own admin, then set `SEED_DEMO_USER=false`).

> Railway free trial gives limited one-off credit; the Hobby plan ($5/mo) covers
> a small always-on service.

---

## Option B — Render.com (free tier caveats)

1. **render.com** → *New* → *Web Service* → connect the GitHub repo.
2. Runtime **Docker** (auto-detected). Instance: Free.
3. **Start command**: same as Railway step 2 above.
4. **Environment variables**: same set as Railway step 3.
5. Create → Render builds and gives `https://<name>.onrender.com`.

**Honest caveats of the free tier**: the service **sleeps after ~15 min idle**
(first visitor waits ~50 s cold start) and the **disk is ephemeral** — the DB
resets on every redeploy/restart. Fine for demos; use Railway/VPS for
persistence. Paid instance types support persistent disks and no sleep.

---

## Option C — Fly.io

```bash
# once: brew install flyctl && flyctl auth login
flyctl launch --no-deploy --internal-port 8000 --http-port 80      # creates fly.toml
flyctl volumes create prvision_data --size 3                       # persistent disk
# in fly.toml [mounts] set: source="prvision_data", destination="/data"
flyctl secrets set SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(48))")
flyctl secrets set DB_ENGINE=sqlite SQLITE_PATH=/data/prvision.db INGESTION_ENABLED_ON_STARTUP=true
flyctl deploy
```

Set the same start command under `[processes]` in `fly.toml`. Fly's free
allowance covers one small always-on machine.

---

## Option D — VPS + full stack (MySQL, production)

Any Ubuntu 22.04+ box (Hetzner/DigitalOcean/AWS Lightsail, ~$4–6/mo):

```bash
sudo apt-get update && sudo apt-get install -y docker.io docker-compose-plugin git
git clone https://github.com/<you>/pr-vision.git && cd pr-vision
cp .env.example .env            # EDIT: passwords + SECRET_KEY (required)
nano .env
docker compose up -d --build    # MySQL + sidecar + API, migrations auto-run
curl http://localhost:8000/api/health
```

Then put it on the internet:

- **Quick**: open port 80 and add a reverse proxy:
  ```bash
  sudo apt-get install -y caddy
  echo "yourdomain.com {\n  reverse_proxy 127.0.0.1:8000\n}" | sudo tee /etc/caddy/Caddyfile
  sudo systemctl reload caddy      # automatic HTTPS via Let's Encrypt
  ```
- **Or without a domain**: `http://<server-ip>:8000` (open port 8000 in the
  firewall panel; fine for a demo, use Caddy/nginx + HTTPS for production).

---

## Environment variable reference

| Variable | Default | Meaning |
|---|---|---|
| `DB_ENGINE` | `mysql` | `sqlite` for lite deploys, `mysql` for production |
| `SQLITE_PATH` | `ml/datasets/prvision_demo.db` | Absolute path recommended (`/data/prvision.db`) |
| `SECRET_KEY` | — | **Set in production** — session signing (`token_urlsafe(48)`) |
| `SEED_DEMO_USER` | `true` | Seeds `demo@prvision.ai / DemoVision!2026` on first run; set `false` once you have your own admin |
| `INGESTION_ENABLED_ON_STARTUP` | `false` | `true` → 8 platform pipelines start with the API |
| `INGESTION_STARTUP_PLATFORMS` | all 8 | Comma list: `demo,x,reddit,instagram,facebook,linkedin,mastodon,hackernews` |
| `SIDECAR_ENABLED` | `true` | Backend uses the provider sidecar |
| `SIDECAR_EMBEDDED` | `true` | `false` when sidecar runs as a separate container |
| `SIDECAR_URL` | `http://127.0.0.1:8787` | Sidecar address (`http://sidecar:8787` in compose) |
| `ZAI_CONFIG_JSON` | — | Full JSON of the z-ai SDK `.z-ai-config` (see below) |
| `CORS_ORIGINS` | `*` | Restrict to your frontend origin in production |
| `X_BEARER_TOKEN`, `REDDIT_CLIENT_ID/SECRET`, `META_ACCESS_TOKEN`, `LINKEDIN_ACCESS_TOKEN` | — | Official platform APIs — upgrade big-5 from web-harvest to official streams |
| `GOOGLE_FACTCHECK_API_KEY`, `NEWSAPI_KEY` | — | Extra evidence providers (report DISABLED without) |

### About `ZAI_CONFIG_JSON` (z-ai SDK credentials)

The provider sidecar wraps `z-ai-web-dev-sdk`, which reads a `.z-ai-config`
JSON (`{"apiKey": "...", "baseUrl": "..."}`). **Inside the Z.ai sandbox this
file exists automatically.** On external hosts you must supply it yourself:

```bash
# Railway/Render/Fly: add variable
ZAI_CONFIG_JSON={"apiKey":"YOUR_KEY","baseUrl":"https://api.example.com/v1"}
# VPS compose: put the same in .env
```

**Without it** the deployment still works — the sidecar reports *degraded* and
the system **honestly disables** what it cannot reach (nothing is fabricated):

| Capability | Without z-ai config | With it |
|---|---|---|
| Demo stream, ML scoring, alerts, dashboard | ✅ | ✅ |
| HackerNews + Mastodon (free public APIs, real metrics) | ✅ | ✅ |
| X/Reddit/Instagram/Facebook/LinkedIn real posts (web harvest) | ❌ shows unavailable | ✅ |
| Web-search / page-reader / LLM / vision evidence | ❌ DISABLED | ✅ |
| Wikipedia evidence (keyless public API) | ✅ | ✅ |

---

## Post-deploy checklist

1. `curl https://your-url/api/health` → `{"status": "healthy", ...}`
2. Log in (`demo@prvision.ai / DemoVision!2026` unless changed) → **register your own admin → set `SEED_DEMO_USER=false`** → redeploy.
3. Dashboard: KPIs populate within ~30 s of boot (ingestion loops start staggered).
4. **Sources panel** shows each platform's real state — big-5 as `HARVEST`
   (keyless web-harvest) when the sidecar is healthy, `CONNECTED` for
   Mastodon/HackerNews, honest `DISABLED` + fix hints otherwise.
5. Verify → paste any post URL → evidence panel fills from reachable providers.
6. `docker compose logs api | grep -i error` on a VPS if anything looks off.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `forecast_model: not_loaded` in `/api/health` | Models train on the DB snapshot; on a fresh empty DB the first training needs data — keep the `seed_demo_data.py` step in the start command, or `docker exec -it <api> python ../scripts/train_models.py` after some hours of ingestion |
| Platforms stuck in error state | Sidecar unhealthy → check `ZAI_CONFIG_JSON`, logs: `docker compose logs sidecar` |
| Login fails after redeploy (Render free) | Ephemeral disk wiped the DB — expected on free tier; use Railway/VPS |
| `alembic upgrade head` fails on MySQL | Check `MYSQL_*` vars; the MySQL container must be healthy first (compose handles ordering) |
| 502 after idle (Render free) | Service slept; first request wakes it (~50 s) |
