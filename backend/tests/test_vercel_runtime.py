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
# The Vercel project root is frontend/, so the function lives there.
API_DIR = ROOT / "frontend" / "api"


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
    requirements = _read(ROOT / "frontend" / "requirements.txt")
    active = [
        line.strip()
        for line in requirements.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert not any("celery" in line for line in active), active
    assert not any("redis" in line for line in active), active
    # The image has no system ffmpeg, but the bundle carries a static build
    # (scripts/vercel-build.sh) so a huge wheel is not needed.
    assert any("fastapi" in line for line in active), active


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


# ------------------------------------------------------- key-free analysis ---
def test_packaging_prompt_has_a_key_free_answer():
    """The offline engine must understand the packaging prompt, not return an empty array.

    Without this the pipeline logs "Packaging response rejected ... not an object"
    twice per clip and every video loses its title, captions and emphasis.
    """
    from backend.core import offline_llm
    from backend.services.studio import packaging
    payload = {
        "template": "interview_zh", "audience_language": "zh", "translate": False,
        "title_limit": 12, "lines": [{"id": 0, "text": "为什么大多数公司做不好AI转型？"},
                                     {"id": 1, "text": "因为把AI当成了IT项目。"}],
    }
    raw = offline_llm.answer(packaging.PROMPT, payload)
    assert raw != "[]"
    import json
    result = json.loads(raw)
    assert result["title_lines"], result
    assert all(len(line) <= 40 for line in result["title_lines"])


def test_packaging_builds_through_the_real_validator_key_free():
    """End to end: offline call -> validator -> a usable Packaging dict."""
    import json
    from backend.core import offline_llm
    from backend.services.platform_strategy import platform_strategy
    from backend.services.studio import packaging

    call = lambda prompt, body: json.loads(offline_llm.answer(prompt, body))  # noqa: E731
    lines = [{"text": "为什么大多数公司做不好AI转型？", "start": 0, "end": 2},
             {"text": "因为把AI当成了IT项目。", "start": 2, "end": 4}]
    result = packaging.build_packaging({"title": "为什么做不好AI转型"}, lines,
                                      platform_strategy("douyin"), call=call)
    assert result["fallback"] is False
    assert result["title_lines"] and all(len(t) <= 14 for t in result["title_lines"])
    assert result["cues"], "captions are what make the clip watchable"
    assert result["mood"]


def test_title_never_cuts_inside_a_latin_token():
    """`……做不好AI转型` at a 12-char cap must not become `……做不好A`."""
    from backend.core.offline_llm import _pack_title_lines
    lines = _pack_title_lines("为什么大多数公司做不好AI转型？先看数据。", 12, "zh")
    assert lines and all(len(line) <= 12 for line in lines), lines
    assert not lines[0].endswith("A"), lines


def test_offline_endpoint_is_rewritten_to_the_port_bound_this_start(tmp_path, monkeypatch):
    """A stale port in the settings file is why analysis failed with `connection`."""
    import json
    from backend.core import offline_bootstrap

    target = tmp_path / "ai-model-settings.json"
    target.write_text(json.dumps({
        "version": 1,
        "connections": [
            {"id": "offline-engine", "provider": "ollama", "base_url": "http://127.0.0.1:43669/v1"},
            {"id": "mine", "provider": "openai", "base_url": "https://api.example.com/v1"},
        ],
        "analysis": {"connection_id": "offline-engine", "model": "autoclip-offline-v1"},
    }), encoding="utf-8")

    assert offline_bootstrap._resync_endpoint(target, "http://127.0.0.1:40417/v1") is True
    document = json.loads(target.read_text(encoding="utf-8"))
    urls = {c["id"]: c["base_url"] for c in document["connections"]}
    assert urls["offline-engine"] == "http://127.0.0.1:40417/v1"
    assert urls["mine"] == "https://api.example.com/v1", "an operator's own provider is never touched"
    # Idempotent: no rewrite when nothing changed.
    assert offline_bootstrap._resync_endpoint(target, "http://127.0.0.1:40417/v1") is False


def test_every_render_dispatch_is_counted():
    """Background work must be drainable, or Vercel freezes it after the response."""
    source = _read(ROOT / "backend" / "services" / "studio" / "jobs.py")
    assert "_submit(" in source
    assert "__submit(" not in source, "a mis-renamed dispatch would raise NameError at render time"
    for call in ("_submit(render_executor", "_submit(cover_executor"):
        assert call in source, call


def test_clip_finder_prompt_is_understood_key_free():
    """The importer's first model call. An empty array here loses every window."""
    import json
    from backend.core import offline_llm
    from backend.pipeline import clip_finder

    prompt = clip_finder.PROMPT.format(min_sec=15, max_sec=60, target_lo=25, target_hi=45,
                                       count_lo=2, count_hi=5, long_rule="")
    rows = ("0|0:01|为什么大多数公司做不好AI转型？\n"
            "1|0:05|首先是因为他们把AI当成一个IT项目。\n"
            "2|0:12|交给技术部门去试点，但AI转型本质上是组织变革。\n"
            "3|0:24|需要业务部门主导，一把手必须亲自推动。")
    result = json.loads(offline_llm.answer(prompt, {"rows": rows}))
    clips = result["clips"]
    assert clips, result
    for clip in clips:
        assert 0 <= clip["start"] <= clip["end"] <= 3
        assert clip["title"] and len(clip["title"]) <= 20
        assert 0.0 <= clip["score"] <= 1.0


def test_boundary_prompt_never_ends_on_an_unfinished_row():
    """A cut on a half sentence is the most visible editing defect in a clip."""
    import json
    from backend.core import offline_llm
    from backend.services.studio import boundaries

    lines = [{"id": n, "text": text} for n, text in enumerate(
        ["为什么做不好？", "因为把AI当成项目。", "交给技术部门", "去试点。"])]
    result = json.loads(offline_llm.answer(boundaries.BOUNDARY_PROMPT,
                                           {"lines": lines, "start_line": 0, "end_line": 2}))
    assert result["end_line"] == 3, result  # extended past the unfinished row


def test_same_language_clip_keeps_its_captions_key_free():
    """A Chinese clip on a Chinese platform must have captions and a title."""
    import json
    from backend.core import offline_llm
    from backend.services.platform_strategy import platform_strategy
    from backend.services.studio import packaging

    call = lambda prompt, body: json.loads(offline_llm.answer(prompt, body))  # noqa: E731
    lines = [{"text": "为什么大多数公司做不好AI转型？", "start": 0, "end": 2},
             {"text": "因为把AI当成了IT项目。", "start": 2, "end": 4}]
    result = packaging.build_packaging({"title": "AI 转型"}, lines,
                                      platform_strategy("douyin"), call=call)
    assert result["fallback"] is False
    assert len(result["cues"]) == 2
    assert all(cue["text"] for cue in result["cues"])
    assert result["title_lines"]


def test_english_clip_on_an_english_platform_keeps_its_captions():
    import json
    from backend.core import offline_llm
    from backend.services.platform_strategy import platform_strategy
    from backend.services.studio import packaging

    call = lambda prompt, body: json.loads(offline_llm.answer(prompt, body))  # noqa: E731
    lines = [{"text": "Why do most companies get AI transformation wrong?", "start": 0, "end": 2},
             {"text": "Because they treat AI as an IT project.", "start": 2, "end": 4}]
    result = packaging.build_packaging({"title": "AI transformation"}, lines,
                                      platform_strategy("tiktok"), call=call)
    assert result["fallback"] is False
    assert len(result["cues"]) == 2
    assert result["title_lines"] and all(len(line) <= 40 for line in result["title_lines"])


def test_burned_caption_detection_answers_none_without_a_vision_model():
    """The render asks whether the source already has burned captions."""
    import json
    from backend.core import offline_llm
    from backend.services.studio import jobs

    answer = json.loads(offline_llm.answer(jobs.BURNED_LANGUAGE_PROMPT, {}))
    assert answer["language"] in ("zh", "en", "ja", "ko", "other", "none")


def test_describe_clip_never_invents_words():
    """A label may only use words the clip actually says."""
    from backend.core.offline_llm import describe_clip
    text = "很多公司把预算花在买模型上，但真正的转型是组织变革。"
    line = describe_clip(text, "zh")
    assert line and all(word in text for word in line[0].split("、"))


def test_a_variant_orphaned_without_a_claim_is_settled_so_it_can_be_retried(tmp_path, monkeypatch):
    """A function frozen between queueing a variant and dispatching it must not spin forever.

    Nothing owns such a variant, `produce` refuses it as "already being generated",
    and the card would show a queued state that never changes. Recovery settles it
    as an interrupted run, which is what the UI turns into a retry button.
    """
    from backend.services.studio import store

    assert store.render_alive({"status": "queued", "queued_by_instance": store.INSTANCE})
    assert not store.render_alive({"status": "queued", "queued_by_instance": "an-instance-that-is-gone"})
    assert store.render_alive({"status": "queued", "render_dispatch_claimed": True})
    assert store.render_alive({"status": "queued", "render_job_id": "abc"})


def test_a_freshly_queued_variant_gets_a_dispatch_grace_window():
    """A browser poll can land on another instance between queueing and dispatch."""
    import datetime as dt
    from backend.services.studio import store

    fresh = dt.datetime.now(dt.timezone.utc).isoformat()
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=store.ORPHAN_GRACE_SEC + 5)).isoformat()
    assert not store._queued_long_enough({"created_at": fresh})
    assert store._queued_long_enough({"created_at": old})
    assert store._queued_long_enough({})


def test_the_studio_import_batch_fits_one_invocation():
    """One request may hold the function for `maxDuration`; the batch must fit.

    The drain wrapper keeps the invocation alive until the renders it started are
    done, under a 280 s budget inside the 300 s cap. Measured on this build
    (iwr, 90 s source, 3 platforms): 9 clips in 180 s. A regression that makes a
    clip much slower would push a normal batch past the cap, so the per-clip
    budget is pinned here rather than discovered in production.
    """
    from backend.services.studio import jobs

    # Encodes are serial by design (one render thread): parallelism here is what
    # turns 180 s into a timeout on the single vCPU a function gets.
    source = _read(ROOT / "backend" / "services" / "studio" / "jobs.py")
    assert "render_executor = ThreadPoolExecutor(max_workers=1" in source
    assert "ThreadPoolExecutor" in source
