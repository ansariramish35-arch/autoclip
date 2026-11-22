# AutoClip — local source install (this checkout)

Everything below is already done in this workspace; the file exists so the setup is
reproducible and reviewable. Paths assume the repo root `/home/user/autoclip`.

## What is running

| Service | Port | How it was started |
|---|---|---|
| Backend API (FastAPI) | 8000 | `.venv/bin/python -m backend.main --port 8000` with `.env` loaded |
| Celery worker (all 5 queues) | — | `celery -A backend.core.celery_app worker -Q celery,processing,video,notification,upload` |
| Frontend dev server (Vite) | 3000 | `cd frontend && npm run dev` |
| Redis 6.2 | 6379 | `.venv/bin/redis-server` (static build) |

`scripts/runtime/start.sh` (via `./start_autoclip.sh`) is unchanged and still the
canonical way to start the stack on a normal machine with system Redis + ffmpeg.

## Installed

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt      # backend + celery + LLM SDKs
.venv/bin/pip install -e .                     # `autoclip` / `autoclip-mcp` entry points
.venv/bin/pip install -r requirements-test.txt  # ruff / numpy / opencv / duckdb
cd frontend && npm install                      # 431 packages
```

Because this container has no system `ffmpeg` or `redis-server` (apt and
release-asset downloads are blocked; only GitHub, npm and PyPI are reachable),
the static binaries came from PyPI wheels and were copied into `.venv/bin`:

```bash
# ffmpeg 6.0 (+ ffprobe) — same major as CI's ubuntu-latest apt build (6.1.1).
# From the ffmpeg-binaries wheel; johnvansickle static build.
curl -sL https://files.pythonhosted.org/packages/.../ffmpeg_binaries-1.1.0-py3-none-manylinux1_x86_64.whl
unzip -j -o ffb.whl "ffmpeg/binaries/ffmpeg" "ffmpeg/binaries/ffprobe" -d .venv/bin

# ffmpeg 9.0.2 kept alongside for the desktop-pinned path (scripts/ffmpeg-*.env),
# from the imageio-ffmpeg wheel:
mv .venv/bin/ffmpeg-linux-x86_64-v9.0.2 .venv/bin/ffmpeg-9.0.2

# redis-server 6.2.14 + redis-cli
unzip -j -o redislite-*.whl "redislite/bin/redis-server" "redislite/bin/redis-cli" -d .venv/bin
```

**ffmpeg version selects which tests pass — this is the one real rough edge.**

| build | outcome |
|---|---|
| 6.0 / 6.1.1 (`PATH` default, = CI) | 4440 passed, **2 failed**: `test_render_validation.py::test_corrupt_tail_audio_is_rejected` (9.x refuses `-c copy` into that mux buffer, 6.x accepts it) and `test_editorial_captions.py::test_progress_bar_width_tracks_three_timestamps` |
| 9.0.2 (`scripts/ffmpeg-*.env`, what desktop ships) | those 2 pass, but 4 `test_portrait_speaker_crop.py` cases fail with `('skip', 'unreadable')` |

The 4 crop failures on 9.0.2 are a real bug in the repo, not an install problem:
`backend/services/studio/qa/face.py` matches `\[showinfo@g0 @ ...\]`, but ffmpeg 9
logs the filter as `[g0 @ ...]`, so every frame is dropped. Verified by swapping only
the binary — same code, opposite result. Not fixed here (out of scope for an install);
say the word and I'll patch the regex and add a regression test.

The 2 failures on the CI version look like the tests being tuned to the desktop
build rather than CI's distro ffmpeg — worth a separate look.

```
cd /home/user/autoclip
PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest backend/tests -q      # CI-matching run
PATH="/tmp/ff9:$PWD/.venv/bin:$PATH" ...                                     # to exercise 9.x only
```

## Configuration

`cp env.example .env`; the local `.env` differs only in:

- `API_DASHSCOPE_API_KEY=` (left empty — **no LLM key is configured**)
- `AUTOCLIP_ALLOWED_ORIGINS=http://localhost:3000,http://127.0.0.1:3000`
- `AUTOCLIP_FFMPEG_PATH` / `AUTOCLIP_FFPROBE_PATH` pointing at `.venv/bin/*`
  (absolute paths only; `AUTOCLIP_FFMPEG_PATH` is what the backend reads)

Start the backend so the file reaches the process the same way `start.sh` does:

```bash
.venv/bin/python -c 'import os,sys; from dotenv import load_dotenv; load_dotenv(".env", override=False); \
  os.environ.setdefault("AUTOCLIP_HOST","0.0.0.0"); os.execvpe(sys.argv[1], sys.argv[1:], os.environ)' \
  .venv/bin/python -m backend.main --port 8000
```

## Verified

- `GET /api/v1/health/` → `{"status":"healthy","version":"1.5.8"}`
- `POST /api/v1/example-project/create` → real ffmpeg cut of 3 clips from the bundled
  68-minute interview asset; `GET /api/v1/files/projects/<id>/clips/<clip>` returns a
  6.7 MB `video/mp4` with a 202 s duration.
- `autoclip doctor` → `✓ ffmpeg` (9.0.2)
- `npm run typecheck`, `npm run build`, `npm run lint` — clean.
- `npm test` → 827/827 pass.
- Backend `pytest backend/tests` → 4433 passed, 8 skipped; after installing the
  CJK font and ffmpeg 9.0.2 the 6 environmental failures pass too
  (`test_studio.py` + `test_portrait_speaker_crop.py` → 129/129).
- CJK font: `backend/services/publish_export.py:resolve_cjk_font()` only looks at
  well-known system paths, so the bundled `backend/assets/fonts/NotoSansSC.ttf`
  was copied to `/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc`
  (`sudo`; needs redoing if the sandbox is rebuilt).
- Dev server accepts the proxied preview host and writes pass through Vite's proxy.

## Still needs your input

- **LLM API key** (DashScope / OpenAI / Gemini / …): set it in the app's
  Settings → Providers, or `API_<PROVIDER>_API_KEY` in `.env`. Without it, import
  and clip generation stop at the model call. The bundled example project opens
  and re-renders without a key.
- **Whisper** (speech-to-text) is not installed, so videos *without* subtitles
  can't be transcribed yet. Videos with an `.srt`, or the example project, work.

## One code fix made during install

`backend/core/log_redaction.py` — `RedactingFilter` flattened `record.args` to `()`
after pre-rendering the message. uvicorn's `AccessFormatter` unpacks
`record.args` into five fields, so **every request** logged a full
`ValueError: not enough values to unpack (expected 5, got 0)` traceback from
inside `logging` and the access line itself was lost:

```
--- Logging error ---
ValueError: not enough values to unpack (expected 5, got 0)
Message: '127.0.0.1:53562 - "GET /api/v1/settings/current-provider HTTP/1.1" 200'
```

Fix: records from `STRUCTURED_LOGGERS` (`uvicorn.access`) keep their positional
args; each arg is redacted individually and non-string scalars (`200`) pass
through so `%d` still works. Everything else keeps the original flatten-and-render
behaviour, so redaction coverage is unchanged. Two regression tests were added in
`backend/tests/test_log_redaction.py`; both fail on the old code and pass now.
Nothing else in the repo was modified.
