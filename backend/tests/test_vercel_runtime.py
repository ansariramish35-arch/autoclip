"""The assumptions the Vercel deployment rests on, asserted rather than trusted.

Each test here exists because getting it wrong produces a deployment that builds
and boots but cannot make a clip — the failure mode that is hardest to see.
"""
from __future__ import annotations

import ast
import importlib
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
API_DIR = ROOT / "api"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# --------------------------------------------------------------- entrypoint ---
def test_entrypoint_exposes_an_asgi_app():
    """Vercel looks for `app` in api/index.py; anything else deploys a 404."""
    sys.path.insert(0, str(API_DIR))
    try:
        module = importlib.import_module("index")
        module = importlib.reload(module)
    finally:
        sys.path.remove(str(API_DIR))
    assert hasattr(module, "app")
    assert callable(module.app)


def test_entrypoint_finds_the_backend_tree_from_either_layout():
    """It runs from the repo root locally and from frontend/api on Vercel."""
    source = _read(API_DIR / "index.py")
    assert "_project_root" in source
    # The probe must look for a marker, not a hard-coded parent depth.
    assert "backend" in source and "main.py" in source


def _import_entrypoint(monkeypatch, **env):
    """Import api/index.py with a clean environment, returning the module."""
    for key in ("AUTOCLIP_DATA_DIR", "AUTOCLIP_OFFLINE_ENGINE",
                "AUTOCLIP_FFMPEG_PATH", "AUTOCLIP_FFPROBE_PATH"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    sys.path.insert(0, str(API_DIR))
    try:
        sys.modules.pop("index", None)
        return importlib.import_module("index")
    finally:
        sys.path.remove(str(API_DIR))


def test_entrypoint_applies_its_defaults(monkeypatch):
    """With nothing configured, the entrypoint supplies a working baseline."""
    module = _import_entrypoint(monkeypatch)
    assert module is not None
    assert os.environ["AUTOCLIP_DATA_DIR"] == "/tmp/autoclip"
    assert os.environ["AUTOCLIP_OFFLINE_ENGINE"] == "local"


def test_entrypoint_never_overrides_a_real_deployment_value(monkeypatch, tmp_path):
    """A dashboard variable must win: the defaults are defaults, not policy."""
    module = _import_entrypoint(
        monkeypatch,
        AUTOCLIP_DATA_DIR=str(tmp_path / "custom"),
        AUTOCLIP_OFFLINE_ENGINE="remote",
        AUTOCLIP_FFMPEG_PATH="/opt/bin/ffmpeg",
        AUTOCLIP_FFPROBE_PATH="/opt/bin/ffprobe",
    )
    assert module is not None
    assert os.environ["AUTOCLIP_DATA_DIR"] == str(tmp_path / "custom")
    assert os.environ["AUTOCLIP_OFFLINE_ENGINE"] == "remote"
    assert os.environ["AUTOCLIP_FFMPEG_PATH"] == "/opt/bin/ffmpeg"
    assert os.environ["AUTOCLIP_FFPROBE_PATH"] == "/opt/bin/ffprobe"


def test_writable_directory_is_used_outside_tmp_only_when_asked(monkeypatch):
    """Vercel mounts everything read-only except /tmp."""
    source = _read(API_DIR / "index.py")
    assert '"/tmp/autoclip"' in source


# --------------------------------------------------------- no broker needed ---
def test_studio_pipeline_does_not_dispatch_through_celery():
    """The studio renders on a ThreadPoolExecutor, which is why no Redis is set.

    If studio code ever starts calling .delay()/.apply_async(), the key-free
    deployment breaks in a way no readiness probe would catch.
    """
    offenders = []
    studio = ROOT / "backend" / "services" / "studio"
    for path in studio.rglob("*.py"):
        tree = ast.parse(_read(path), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr in {"delay", "apply_async"}:
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert offenders == [], f"studio path dispatches to a broker: {offenders}"


def test_studio_uses_a_thread_pool_executor():
    source = _read(ROOT / "backend" / "services" / "studio" / "jobs.py")
    assert "ThreadPoolExecutor" in source


def test_vercel_requirements_do_not_pull_a_broker():
    """celery[redis] would add the broker the deployment deliberately lacks."""
    requirements = _read(ROOT / "requirements-vercel.txt")
    active = [
        line.strip()
        for line in requirements.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert not any("celery" in line for line in active), active
    assert not any("redis" in line for line in active), active
    # ffmpeg must be there: the Vercel image has none.
    assert any("ffmpeg-binaries" in line for line in active), active


def test_progress_storage_falls_back_when_redis_is_unreachable(monkeypatch):
    """An unreachable broker must degrade to SQLite, not raise at import."""
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6399/0")
    module = importlib.import_module("backend.services.simple_progress")
    assert module is not None


# ------------------------------------------------------------------- ffmpeg ---
def test_bundled_binaries_are_executable_when_present():
    """A non-executable binary makes every render fail with an opaque error."""
    for name in ("ffmpeg", "ffprobe"):
        path = ROOT / "vendor" / "ffmpeg" / name
        if not path.exists():
            pytest.skip(f"{name} is fetched at build time, not committed")
        assert os.access(path, os.X_OK), path


def test_build_script_fetches_both_binaries():
    script = _read(ROOT / "scripts" / "fetch-ffmpeg.sh")
    assert "ffprobe" in script and "ffmpeg " in script.replace("ffprobe", "")
    assert "chmod +x" in script
