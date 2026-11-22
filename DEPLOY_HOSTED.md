# AutoClip — hosted, no API keys

This deployment needs **no API keys and no model download**. You get the real
AutoClip pipeline (ffmpeg cuts, real clips, Celery, the app's own five analysis
prompts and parsers) with a deterministic analysis engine that runs inside the
backend.

There are two pieces:

| Piece | Host | What it does |
|---|---|---|
| Web app | **Vercel** | The React build. Proxies every API call to the engine. |
| Engine | **A container host** | FastAPI + Celery + Redis + ffmpeg. Does the actual work. |

Vercel alone cannot do the second job: serverless functions have no ffmpeg, a
300 s ceiling, and no long-lived worker to render 90-minute videos. That is why
the engine lives on a container host and Vercel talks to it.

---

## How "no API keys" works

Normally AutoClip stops at the import gate and asks for an LLM key. This build
replaces that with a local child process:

```
backend/core/offline_llm.py        OpenAI-compatible /v1/chat/completions
backend/core/offline_bootstrap.py  starts it + seeds the model settings
```

`AUTOCLIP_OFFLINE_ENGINE=local` turns it on. On startup the backend:

1. listens on `127.0.0.1:8100/v1` (OpenAI wire format, no auth);
2. writes `data/ai-model-settings.json` with an `ollama`-shaped connection
   pointing at it. `ollama` is one of the app's *local presets*, which route to
   `openai` + `base_url` and are considered ready with **no key at all**;
3. sets subtitle analysis mode (no Whisper download, no ASR billing) and a
   `0.55` score threshold, because the deterministic scorer runs lower than a
   chat model would.

The five steps are answered from the subtitle text itself:

| Step | Prompt | What the engine does |
|---|---|---|
| 1 | `大纲.txt` | Groups SRT cues into sentences, then into topic-sized segments |
| 2 | `时间点.txt` | Ranks sentence-aligned windows; every boundary is a real cue time |
| 3 | `推荐理由.txt` | Scores 0–1 on speech density, length and sentence completeness |
| 4 | `标题生成.txt` | Picks the most nameable sentence, strips "um/so/I mean" openers |
| 5 | `主题聚类.txt` | Groups clips sharing vocabulary; only groups of 2+ |

It is deterministic, not clever. It reliably finds well-formed, information-dense
stretches; it cannot understand nuance the way a real model can.

**Want a real model instead?** Open Settings and add your own provider. The seed
never overwrites an existing settings file, so your configuration stays.

---

## 0. Fastest path: run it in this workspace (works right now)

The sandbox already runs the full stack — API, Celery worker, Redis, ffmpeg,
ffprobe — with the key-free engine on. Nothing to deploy, no keys, no card:

**https://3000-i2gb7q8gqa9njzdfg2an5.e2b.app**

Enable the engine in the sandbox (one line, then restart the backend):

```bash
echo 'AUTOCLIP_OFFLINE_ENGINE=local' >> .env
# restart the API so it picks the flag up; the settings file self-seeds
```

Then `curl localhost:8000/api/v1/studio/readiness` must say
`"ready": true` with `analysis: configured`.

Verified on 2026-11-22 by importing a real video through
`POST /api/v1/studio/import`: 5 drafts selected, **4 clips rendered at
1080x1920** (38 s, 62 s, 70 s, 63 s) with burned-in captions. That is the same
endpoint the "Generate videos" button calls, so the button works once the
engine flag is on and ffprobe is present.

> **Why the hosted Vercel URL is not this:** a browser tab cannot reach this
> sandbox, and the Vercel frontend has no engine of its own. Only a container
> host (step 1) gives the hosted URL something to call.

## 1. Deploy the engine (container host)

The image is the repo's own `Dockerfile`, started with
`scripts/host-entrypoint.sh`, which runs Redis, the Celery worker and the API in
one container.

**Railway** (recommended — the free trial does not need a card up front):

1. <https://railway.app/new> → *Deploy from GitHub repo* → `ansariramish35-arch/autoclip`
2. Branch: `arena/7fc10a83-autoclip` (or `main` once merged)
3. Settings → *Deploy* → **Custom Start Command**:
   ```
   bash scripts/host-entrypoint.sh
   ```
4. Variables:
   ```
   AUTOCLIP_OFFLINE_ENGINE=local
   ENVIRONMENT=production
   DEBUG=false
   LOG_LEVEL=INFO
   ```
5. Settings → *Networking* → **Generate Domain**, target port `8000`.
   Keep this URL — it is the engine host.

**Render**: *New → Web Service* → same repo/branch → Runtime **Docker** →
Docker Command `bash scripts/host-entrypoint.sh` → same variables. Note the free
instance spins down after idle and has an ephemeral disk, so projects disappear
on restart; a paid instance with a disk mounted at `/app/data` keeps them.

**Fly.io**: `fly launch --dockerfile Dockerfile` then set
`[processes] app = "bash scripts/host-entrypoint.sh"` and the variables above.

First boot runs `init_database.py` and takes ~40 s. Verify:

```bash
curl https://YOUR-ENGINE-HOST/api/v1/health/
curl https://YOUR-ENGINE-HOST/api/v1/studio/readiness
```

`readiness` must report `"ready": true` with `analysis: configured`. If it says
`missing` for ffmpeg, the image was built without the runtime stage.

## 2. Deploy the frontend (Vercel) — already done

Created and deployed:

| | |
|---|---|
| Project | `autoclip` (`prj_rHBJnM02CoyGq5CQdRM4YpTRr066`), team `ansariramish35-arch` |
| Root directory | `frontend` (framework: Vite, output `dist`) |
| Git | `ansariramish35-arch/autoclip`, auto-deploys on push |
| **Production URL** | **https://autoclip-ansariramish35-arch.vercel.app** |

`frontend/vercel.json` supplies the SPA rewrite that React Router needs.

> **One toggle needed:** the project has Vercel *Deployment Protection*
> ("Vercel Authentication") on, so anyone not signed in to your Vercel account
> gets a login page instead of the app. To share the URL:
> Project → Settings → **Deployment Protection** → *Vercel Authentication* →
> **Disabled** → Save. Your own browser works either way.

Until step 1 is done, the app loads but every API call fails — that is the
frontend waiting for an engine.

Point it at the engine by setting **one environment variable** — no file edit:

> Vercel → project `autoclip` → Settings → **Environment Variables** →
> `AUTOCLIP_API_ORIGIN` = `https://YOUR-ENGINE-HOST` (no trailing slash) →
> all environments → **Redeploy**.

`frontend/middleware.ts` proxies `/api/:path*` to it at request time, so the
browser only ever talks to one origin. That matters because
`backend/core/local_origin_guard.py` accepts a write only when the request's
`Origin` matches the `Host` the backend sees; the proxy forwards the browser's
own `Host` in `X-Forwarded-Host`, so writes (create / import / delete) pass with
no origin allowlist and no CORS preflight.

It is middleware rather than a `vercel.json` rewrite on purpose: rewrite
destinations are static JSON, so changing the engine host would mean editing and
pushing a file. A middleware reads the variable at runtime.

### If it is not configured

`/api/*` answers `503 {"error_code":"engine_not_configured"}` and the UI says
*"Not connected to the AutoClip engine yet: set the engine address in your
deployment settings and try again."*

If the engine is chosen but down, you get `502 {"error_code":"engine_unreachable"}`
(*"Cannot reach the AutoClip engine — check that the container is running."*)

Before this proxy existed, a `POST /api/...` fell through the SPA catch-all and
static hosting rejected it as **405** — the "Request failed with status code 405"
banner. Any bare 405 on an `/api` call now means the middleware did not run.

### Why not call the engine directly

You can, with `VITE_API_BASE_URL=https://YOUR-ENGINE-HOST` as a build variable —
but then you also need `AUTOCLIP_ALLOWED_ORIGINS=https://YOUR-PROJECT.vercel.app`
on the engine, every write gets a CORS preflight, and any URL change needs a
rebuild. The proxy is the simpler contract and is what is deployed.

## 3. Use it

1. Open the Vercel URL.
2. Import a video. With subtitle mode, upload the `.srt` alongside it (or use a
   video whose subtitle file ships next to it).
3. Wait for the progress to hit 100% and download clips.

The first thing to check after import is that Settings shows
`AutoClip 离线引擎（无需密钥）` as the analysis connection.

---

## What this deployment cannot do

- **Speech-to-text on arbitrary uploads.** No Whisper in the image
  (`subtitle` analysis mode). Upload a subtitle file, or set a cloud ASR
  provider in Settings.
- **Nuanced topic detection.** The deterministic engine segments and ranks; it
  does not understand the video.
- **Survive a redeploy without a disk.** Container filesystems are ephemeral;
  mount `/app/data` on a paid plan to keep projects between deploys.

## Supabase (optional clip library)

The Supabase project `tjvpltuphxsfkpagkuwz` has a clip library ready:

- `public.autoclip_projects`, `public.autoclip_clips` — public read, service-role
  write, RLS on
- public storage bucket `autoclip-clips` (250 MB per file)

The app does not need it to run; it is for keeping finished clips beyond the
container's lifetime.

## Verifying the key-free path locally

```bash
AUTOCLIP_OFFLINE_ENGINE=local \
AUTOCLIP_DATA_DIR=/tmp/autoclip-check \
.venv/bin/python -c 'from backend.core.offline_bootstrap import install; install()' &

curl -s localhost:8100/v1/models
cd frontend && VITE_API_BASE_URL=http://127.0.0.1:8100 npm run build
```

Tests:

```bash
.venv/bin/python -m pytest backend/tests/test_offline_engine.py -q   # 33 passed
cd frontend && node --require ./tests/setup.cjs --test tests/hosted-api-base.test.cjs
```
