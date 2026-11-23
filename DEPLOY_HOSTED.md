# AutoClip — hosted on Vercel, no API keys

The **whole app runs on Vercel**: the React build and the FastAPI engine, as one
Python function. No API keys, no model download, no container host, no database
to provision. Import a video (or a link), and you get real rendered clips.

| Piece | Where | What it does |
|---|---|---|
| Web app | Vercel static + CDN | The React build |
| Engine | Vercel Python function (`frontend/api/index.py`) | FastAPI, ffmpeg, the five analysis prompts, rendering |
| Analysis model | in-process, same function | `backend/core/offline_llm.py` (deterministic, key-free) |

Production URL: **https://autoclip-gules.vercel.app**

Vercel's Python function has no system ffmpeg, so the build ships a static
ffmpeg/ffprobe inside the bundle (`scripts/vercel-build.sh`, 150 MB) and the whole
function measures ~474 MB against the 500 MB Python limit. Do not add packages to
`frontend/requirements.txt` without checking `du -sm` afterwards.

## What a hosted function changes (and how it is handled)

| Limitation | Handling |
|---|---|
| Only `/tmp` is writable | `api/index.py` points `AUTOCLIP_DATA_DIR` and `DATABASE_URL` at `/tmp/autoclip` |
| Everything freezes when the response is sent | the ASGI app is wrapped in `_DrainAfterResponse`, which keeps the invocation alive until the render work it started has finished (280 s budget, under the 300 s cap) |
| No Redis | the studio pipeline runs on a `ThreadPoolExecutor`; progress falls back to SQLite |
| No system ffmpeg | static binaries inside the bundle, pathed by `AUTOCLIP_FFMPEG_PATH` / `AUTOCLIP_FFPROBE_PATH` |
| Request body capped at 4.5 MB | the UI explains it before uploading and points at the link import; `VITE_UPLOAD_LIMIT_MB` is set by the Vercel build only, so a self-hosted API keeps unlimited uploads |
| No Whisper in the bundle | import a video link (YouTube / Bilibili, yt-dlp ships in the function) or attach a `.srt` next to the file |

**What fits in one invocation:** a 90 s source, 3 platforms → 9 clips, all
rendered in ~180 s (measured on the production build). One to three platforms on a
one- to two-minute source is the comfortable range; very long sources belong on a
container host (§5).

## How "no API keys" works

Normally AutoClip stops at the import gate and asks for an LLM key. This build
starts a local OpenAI-compatible endpoint inside the function instead:

```
backend/core/offline_llm.py        OpenAI-compatible /v1/chat/completions
backend/core/offline_bootstrap.py  starts it + seeds the model settings
```

`AUTOCLIP_OFFLINE_ENGINE=local` turns it on (the entrypoint sets it by default).
On startup the backend:

1. binds a free port on `127.0.0.1` (8100 when free) and serves the OpenAI wire
   format with no auth;
2. writes `data/ai-model-settings.json` with an `ollama`-shaped connection
   pointing at it. `ollama` is one of the app's *local presets*, which route to
   `openai` + `base_url` and are ready with **no key at all**. On every later
   start it rewrites only that connection's URL to the port bound this time — a
   stale port from a previous instance is why analysis would otherwise fail with
   `connection`;
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

It also answers the prompts the studio layer sends, which is what makes the
output look finished rather than merely cut:

| Prompt | Answer |
|---|---|
| clip finder | complete-answer windows with Chinese titles, a reason and a score |
| boundaries | moves a cut off a half sentence, never starts on a continuation row |
| packaging | 1–2 title lines wrapped by the caption layout code (never mid-word), sentence segments, per-line emphasis that is a literal substring, tags, a mood that picks palette and style |
| post copy | title, description and tags from the clip's own words |
| burned-caption check | `none` (no vision model, so nothing is claimed) |

It is deterministic, not clever. It reliably finds well-formed,
information-dense stretches; it cannot understand nuance the way a real model
can. It also cannot translate: a clip on a platform with a different audience
language keeps its source captions rather than inventing a translation.

**Want a real model instead?** Open Settings and add your own provider. The seed
never overwrites an existing settings file; only its own connection is re-pointed.

---

## 1. Deploy

The project is already created (`autoclip`, root directory `frontend`, framework
`vite`). To deploy the current branch:

```bash
# in the repo
git push origin arena/7fc10a83-autoclip
```

then either push to the project's production branch, or create a deployment for
that branch (this is what the connector does), and wait for **READY / PROMOTED**.

Project settings that matter (all live in `frontend/vercel.json` plus the project):

| Setting | Value |
|---|---|
| Root Directory | `frontend` |
| Framework | `vite` |
| Build Command | `bash ../scripts/vercel-build.sh` |
| Install Command | `npm ci` |
| Output Directory | `dist` |
| Function | `api/index.py`, `maxDuration` 300, `memory` 2000 |
| Environment variables | **none required** |

`AUTOCLIP_API_ORIGIN` must stay **unset**: when it is set, `frontend/middleware.ts`
proxies `/api/*` to that origin instead of this project's own function. With it
unset, the middleware passes through and the function serves the API.

### Deployment Protection (why the URL may ask you to log in)

The project has `ssoProtection: all_except_custom_domains`, so a `*.vercel.app`
URL redirects a browser to a Vercel login — that is Vercel's default for team
projects, not a fault in the app. Two ways out:

- turn Protection off: Vercel → Project → Settings → Deployment Protection → Vercel
  Authentication → **Disabled**; or
- attach a custom domain, which is exempt from that rule by default.

---

## 5. Container host (for very long sources)

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

## 2. Use it

1. Open the production URL.
2. **Import a video**: paste a YouTube / Bilibili link, or upload a file with its
   `.srt` (see the 4.5 MB note above). The link route needs no upload at all.
3. Pick platforms (一 to three is the comfortable range) and press generate.
4. The clips render inside the same request chain; when the cards flip to
   completed, play or download them. Each clip is 1080×1920 H.264 + AAC with the
   title, captions and post copy the packaging step produced.

No settings, no keys, no sign-in (from the app's point of view).

## 3. Verify the deployment end to end

```bash
# readiness: needs ready=true and analysis=configured
curl -s https://autoclip-gules.vercel.app/api/v1/studio/readiness

# import a link (no upload, so no body limit) and watch it
curl -s -X POST https://autoclip-gules.vercel.app/api/v1/studio/import \
  -F 'url=https://www.youtube.com/watch?v=...' -F 'platforms=douyin' \
  -F 'goal=content' -F 'auto_start=true'

# then poll until generation.status is completed, and download
curl -s https://autoclip-gules.vercel.app/api/v1/studio/<project_id>
curl -o clip.mp4 https://autoclip-gules.vercel.app/api/v1/studio/<project_id>/exports/<job_id>/video
```

A `200` from `/import` on its own proves nothing — the response is sent before the
rendering finishes. `generation.status: completed` plus a playable mp4 is the
pass condition.

## 4. Deploying changes

`frontend/api/index.py` is the ASGI app; `frontend/requirements.txt` is the
function's dependency list; `scripts/vercel-build.sh` stages the function tree,
fetches ffmpeg and builds the frontend. Push to the branch, then create a
production deployment for it and wait for **READY / PROMOTED**.

Before pushing, the local checks that cover this deployment:

```bash
pytest backend/tests/test_vercel_runtime.py     # the deployment's assumptions
cd frontend && npm test && npx tsc --noEmit     # UI + types
```

## What this deployment cannot do

- **Speech-to-text.** No Whisper in the bundle (`subtitle` analysis mode), so an
  upload needs its `.srt`; a link import gets the platform's subtitles. Set a
  cloud ASR provider in Settings if you want audio transcription.
- **Uploads over 4.5 MB.** A Vercel cap, not an app limit — use the link import
  or a smaller file. Self-hosted/container deploys have no such cap.
- **Translation.** A key-free engine has no second language, so a clip whose
  audience language differs from the source keeps its source captions instead of
  an invented translation.
- **Nuanced topic detection.** The deterministic engine segments and ranks; it
  does not understand the video the way a real model does.
- **Very long sources.** One invocation has 300 s and `/tmp` only. A 90 s source
  on three platforms renders in ~180 s; a 90-minute source belongs on §5.
- **Durable storage.** `/tmp` is per-instance: a project does not survive a new
  instance. Finished clips are downloadable immediately; the Supabase library
  below is the durable option.

## Supabase (optional clip library)

The Supabase project `tjvpltuphxsfkpagkuwz` has a clip library ready:

- `public.autoclip_projects`, `public.autoclip_clips` — public read, service-role
  write, RLS on
- public storage bucket `autoclip-clips` (250 MB per file)

The app does not need it to run; it is for keeping finished clips beyond the
container's lifetime.

## Verifying the key-free path locally

The same function the deployment runs, on this machine, with no keys:

```bash
cd frontend
PATH="/path/to/.venv/bin:$PATH" \
  AUTOCLIP_OFFLINE_ENGINE=local \
  /path/to/.venv/bin/python -m uvicorn api.index:app --host 127.0.0.1 --port 8097

curl -s localhost:8097/api/v1/studio/readiness        # ready: true, analysis configured
curl -s -X POST localhost:8097/api/v1/studio/import \
  -F "video=@source.mp4" -F "subtitle=@source.srt" \
  -F 'platforms=douyin' -F 'goal=content' -F 'auto_start=true'
curl -s localhost:8097/api/v1/studio/<project_id>      # poll generation.status
```

That is the exact ASGI app (`app`, drain wrapper included) Vercel serves, so a
pass here means the deployment's own code path passed.

Tests that cover this deployment specifically:

```bash
.venv/bin/python -m pytest backend/tests/test_vercel_runtime.py -q   # 24 passed
.venv/bin/python -m pytest backend/tests/test_offline_engine.py -q
cd frontend && npm test && npx tsc --noEmit
```

`test_vercel_runtime.py` asserts the things that fail silently in production: the
entrypoint exists and exposes `app`, the middleware does not shadow it, every
render dispatch is counted and drainable, the offline engine understands each
prompt the studio sends, and a stale offline-engine port is rewritten on startup.
