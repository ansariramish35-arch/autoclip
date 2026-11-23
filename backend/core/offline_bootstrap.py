"""Zero-setup deployment: serve key-free analysis and seed the model settings.

When ``AUTOCLIP_OFFLINE_ENGINE=local`` is set, a hosted AutoClip would otherwise
stop at the import gate asking for an API key. This module removes that step for
deployments that opt in, and only for them:

1. start the in-process OpenAI-compatible endpoint (``backend.core.offline_llm``)
2. seed ``ai-model-settings.json`` with an ``ollama``-shaped connection pointing
   at it, so the readiness gate reports ``configured`` and the pipeline routes
   its five analysis calls here. ``local_compat_remap`` means provider ``ollama``
   needs no key at all.

An operator who saves their own provider in Settings replaces this connection —
the seed never overwrites an existing document.
"""
from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger(__name__)

CONNECTION_ID = "offline-engine"
CONNECTION_NAME = "AutoClip 离线引擎（无需密钥）"


def install() -> None:
    from backend.core import offline_llm

    url = offline_llm.start_if_enabled()
    if not url:
        return
    try:
        _seed_settings(url)
    except Exception as error:  # noqa: BLE001 - never block startup on the seed
        logger.warning("离线引擎设置写入失败（可继续手动配置）: %s", type(error).__name__)


def _resync_endpoint(target, url: str) -> bool:
    """Point an existing offline-engine connection at the port bound this start.

    The engine binds a free port (8100 when it is free, otherwise whatever the
    kernel hands out) and the settings document stores that URL. After a restart
    the stored port is dead, so every analysis call fails with ``connection``
    even though the engine is running. This rewrites only our own connection;
    an operator's own provider is never touched.
    """
    import json

    try:
        with open(target, encoding="utf-8") as stream:
            document = json.load(stream)
    except (OSError, ValueError) as error:
        logger.warning("离线引擎设置无法读取（保留原文件）: %s", type(error).__name__)
        return False
    if not isinstance(document, dict):
        return False
    connections = document.get("connections")
    if not isinstance(connections, list):
        return False
    changed = False
    for connection in connections:
        if isinstance(connection, dict) and connection.get("id") == CONNECTION_ID:
            if connection.get("base_url") != url:
                connection["base_url"] = url
                changed = True
    if not changed:
        return False
    _atomic_write(target, document)
    logger.info("离线引擎地址已同步: %s", url)
    return True


def _seed_settings(url: str) -> None:
    from backend.services import ai_model_settings

    target = ai_model_settings.path()
    if target.exists() and target.stat().st_size > 0:
        _resync_endpoint(target, url)  # load() reads the file, so no cache to clear
        logger.info("已存在模型设置，保留用户配置，不写入离线引擎")
        return

    document = {
        "version": 1,
        "connections": [{
            "id": CONNECTION_ID,
            "name": CONNECTION_NAME,
            # A local preset: routes to openai + base_url and needs no api_key.
            "provider": "ollama",
            "base_url": url,
            "api_key": None,
            "validation_status": "ok",
            "trial_status": "passed",
            "base_url_source": "user",
        }],
        "analysis": {
            "connection_id": CONNECTION_ID,
            "model": "autoclip-offline-v1",
            "capability": "auto",
            "model_source": "user",
        },
        "vision": None,
        "cover": None,
        "cover_enabled": False,
        # Subtitle route: no ASR binary in this deployment, and no paid calls.
        "analysis_mode": "subtitle",
        "allow_visual_screening": False,
        "allow_send_frame": False,
        "chunk_size": 5000,
        # The offline scorer is conservative; 0.7 would reject most windows and
        # lean on the pipeline's fallback top-up. 0.55 keeps real highlights.
        "min_score_threshold": 0.55,
        "max_clips_per_collection": 5,
        "acked_catalog_version": 0,
        "seen_notices": [],
    }

    target.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(target, document)
    settings = ai_model_settings.load()
    logger.info(
        "离线引擎已写入模型设置 (analysis=%s, threshold=%s)",
        bool(settings and settings.analysis), settings.min_score_threshold if settings else "?",
    )


def _atomic_write(target, document: dict) -> None:
    import uuid

    temp = target.with_suffix("." + uuid.uuid4().hex + ".tmp")
    try:
        with open(temp, "w", encoding="utf-8") as stream:
            json.dump(document, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)
