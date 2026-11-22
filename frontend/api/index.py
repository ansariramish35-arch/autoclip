"""Vercel entrypoint for the AutoClip API.

Vercel's Python runtime looks for an ASGI app named ``app`` in this module.

Three runtime differences from a normal container are handled here, before the
application is imported, because the app reads them at import time:

1. **Only /tmp is writable.** The default data directory is ``<project>/data``,
   which is read-only in a function. Point AUTOCLIP_DATA_DIR at /tmp.
2. **No system ffmpeg.** ffmpeg/ffprobe ship inside the bundle (see
   requirements-vercel.txt); the app needs their paths in the environment.
3. **No Redis.** The studio pipeline runs on a ThreadPoolExecutor, so Redis is
   optional — the progress layer falls back to SQLite by itself. Nothing to do,
   but it is why no broker is configured here.

Every setting is applied with ``setdefault`` so a real deployment variable always
wins. This module must stay cheap: it runs on every cold start.
"""
from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent


def _project_root() -> Path:
    """Find the tree that holds backend/.

    Two layouts exist: the repository root (local runs, tests) and
    frontend/api/ (the Vercel build, whose project root directory is frontend).
    Probe for the marker instead of assuming a depth, so both work unchanged.
    """
    for candidate in (HERE.parent, HERE.parent.parent, HERE.parent.parent.parent):
        if (candidate / "backend" / "main.py").is_file():
            return candidate
    return HERE.parent


ROOT = _project_root()


def _bundled_ffmpeg(name: str) -> str | None:
    """Locate a bundled binary, allowing for build-layout differences."""
    for candidate in (
        HERE / "vendor" / "ffmpeg" / name,
        ROOT / "vendor" / "ffmpeg" / name,
        ROOT / "vendor" / "ffmpeg" / "binaries" / name,
        ROOT / "ffmpeg" / "binaries" / name,
    ):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _configure() -> None:
    # 1. Writable storage. Vercel provides 500 MB at /tmp per instance.
    os.environ.setdefault("AUTOCLIP_DATA_DIR", "/tmp/autoclip")

    # 1b. The database URL defaults to a *relative* path (sqlite:///./data/…),
    #     which resolves against the working directory and is read-only in a
    #     function. Without this, creating a project fails with
    #     "unable to open database file" and every table read says "no such
    #     table". Pin it inside the writable directory.
    data_dir = Path(os.environ["AUTOCLIP_DATA_DIR"])
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault("DATABASE_URL", f"sqlite:///{data_dir / 'autoclip.db'}")
    except OSError as error:
        # Crashing here would make every route fail with an opaque 500 and no
        # log line. Leave DATABASE_URL alone and let the app report the real
        # problem through its own error path.
        logger.warning("data directory %s is not writable (%s); using configured database URL", data_dir, error)

    # 2. Key-free analysis engine, unless a real provider is configured.
    os.environ.setdefault("AUTOCLIP_OFFLINE_ENGINE", "local")

    # 3. ffmpeg + ffprobe from the bundle, then PATH, then leave it to the app.
    #    setdefault is applied to the resolved path too, so a deployment variable
    #    always wins over discovery.
    for env_name, binary in (
        ("AUTOCLIP_FFMPEG_PATH", "ffmpeg"),
        ("AUTOCLIP_FFPROBE_PATH", "ffprobe"),
    ):
        found = _bundled_ffmpeg(binary) or shutil.which(binary)
        if found:
            os.environ.setdefault(env_name, found)

    # 4. Do not phone home, and do not look for a desktop shell.
    os.environ.setdefault("AUTOCLIP_MODE", "web")
    os.environ.setdefault("ENVIRONMENT", "production")
    os.environ.setdefault("DEBUG", "false")

    # The project root holds backend/ and prompt/, which the app reads relative
    # to its package; make it importable regardless of the cwd.
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))


_configure()

from backend.main import app  # noqa: E402  (must follow _configure)

__all__ = ["app"]
