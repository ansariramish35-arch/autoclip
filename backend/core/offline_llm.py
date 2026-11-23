"""Key-free deterministic analysis: a local OpenAI-compatible endpoint.

AutoClip's five analysis steps (outline, timeline, scoring, title, clustering)
all talk to a model through one OpenAI-compatible chat call. For a hosted
demo that must not ask anyone for an API key, this module serves that same
endpoint from inside the backend process and answers each step with a
deterministic, rule-based engine over the subtitle text.

It is deliberately not an LLM and does not pretend to be one. What it does:

* outline  — group subtitle cues into segments at natural pauses
* timeline — pick the densest segments inside a size band, with real times
* scoring  — score on information density, cue count and speech rate
* title    — take the most salient sentence, trimmed to a headline
* cluster  — group by shared vocabulary across the chosen clips

The result is watchable, honest highlight detection that costs nothing and
needs no credentials. Setting a real provider in Settings overrides it; the
pipeline steps, parsers and guards all stay the original code.

Enable with ``AUTOCLIP_OFFLINE_ENGINE=local`` (see ``scripts/entrypoint.sh``).
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Sequence

logger = logging.getLogger(__name__)

ENV_ENABLE = "AUTOCLIP_OFFLINE_ENGINE"
ENV_HOST = "AUTOCLIP_OFFLINE_ENGINE_HOST"
ENV_PORT = "AUTOCLIP_OFFLINE_ENGINE_PORT"
LOCAL_HOST = "127.0.0.1"
DEFAULT_PORT = 8100
MODEL_ID = "autoclip-offline-v1"

# Prompt markers, from backend/prompt/**/*.txt. The pipeline sends the whole
# prompt as the user message, so the step is identified by its own preamble.
_OUTLINE_MARKER = "视频内容结构分析师"
_TIMELINE_MARKER = "定位出每个话题精确的"
_SCORING_MARKER = "进行综合评估，并给出一个最终分数"
_TITLE_MARKER = "生成**1个**最佳的"
_CLUSTER_MARKER = "对它们进行主题聚类"
_POST_COPY_MARKER = "为 platforms 里的每个平台写发布文案"
_PACKAGING_MARKER = "为这段访谈做包装"
_CLIP_FINDER_MARKER = "挑出所有值得单独发布成短视频的片段"
_BOUNDARY_MARKER = "请调整首尾"
_BURNED_MARKER = "判断画面上有没有烧录的对白字幕"

# Sentence-final punctuation, plus soft breaks CJK subtitles actually use.
_SENTENCE_END = "。！？!?；;….．"
_SOFT_END = "，,、"

_JSON_INPUT = re.compile(r"输入内容：\s*(.+)\Z", re.DOTALL)
_TIMESTAMP = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})")


def enabled() -> bool:
    return os.getenv(ENV_ENABLE, "").strip().lower() in {"local", "1", "true", "yes", "on"}


def base_url() -> str:
    host = os.getenv(ENV_HOST, LOCAL_HOST)
    port = os.getenv(ENV_PORT, str(DEFAULT_PORT))
    return f"http://{host}:{port}/v1"


# ------------------------------------------------------------------ helpers ---
def _to_seconds(stamp: str) -> float:
    match = _TIMESTAMP.search(stamp or "")
    if not match:
        return 0.0
    hours, minutes, seconds, millis = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(millis.ljust(3, "0")) / 1000.0


def _to_stamp(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, rest = divmod(rest, 60)
    whole = int(rest)
    millis = int(round((rest - whole) * 1000))
    if millis == 1000:  # rounding carried into the next second
        whole, millis = whole + 1, 0
    return f"{int(hours):02d}:{int(minutes):02d}:{whole:02d},{millis:03d}"


# Speaker-turn markers some subtitle exports put inline (">> Guest:").
_SPEAKER = re.compile(r"^>+\s*|\[\s*\d+\s*\]")
# Function words that start a sentence badly; used to score candidate openers.
_WEAK_OPENER = re.compile(r"^(?:and|but|or|so|because|which|that|if|when|then|also|uh|um|well)\b", re.IGNORECASE)


def _cues(text: str) -> list[dict[str, Any]]:
    """Parse SRT-style text (index / range / lines) into cue dicts."""
    cues: list[dict[str, Any]] = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n").strip()):
        lines = [line.strip() for line in block.split("\n") if line.strip()]
        if len(lines) < 2:
            continue
        range_line = next((line for line in lines[:2] if "-->" in line), "")
        if not range_line:
            continue
        start_text, _, end_text = range_line.partition("-->")
        body = lines[lines.index(range_line) + 1:]
        spoken = " ".join(body).strip()
        if not spoken:
            continue
        cues.append({
            "start": _to_seconds(start_text),
            "end": _to_seconds(end_text),
            "text": spoken,
        })
    return cues


def _sentences(cues: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Join cues into sentences, keeping the cue range each sentence came from.

    Subtitle cues are short (1-3s) and break mid-sentence, so sentence
    boundaries — not cue gaps — are what makes a clip start and end cleanly.
    """
    sentences: list[dict[str, Any]] = []
    buffer: list[str] = []
    first: dict[str, Any] | None = None
    last: dict[str, Any] | None = None
    for cue in cues:
        spoken = _SPEAKER.sub("", cue["text"]).strip()
        if not spoken:
            continue
        if first is None:
            first = cue
        buffer.append(spoken)
        last = cue
        if spoken[-1] in _SENTENCE_END:
            sentences.append({
                "text": " ".join(buffer).strip(),
                "start": first["start"],
                "end": last["end"],
            })
            buffer, first, last = [], None, None
    if buffer and first is not None and last is not None:
        sentences.append({"text": " ".join(buffer).strip(), "start": first["start"], "end": last["end"]})
    return [item for item in sentences if item["text"]]


def _salient(text: str, limit: int = 46) -> str:
    # Speaker markers appear mid-line in some exports ("... efforts? >> No."),
    # so strip them everywhere rather than only at the start.
    text = re.sub(r">>+\s*", " ", str(text or ""))
    text = _SPEAKER.sub("", re.sub(r"\s+", " ", text)).strip()
    # A title names one thought: keep the first sentence and drop the rest.
    for index, char in enumerate(text):
        if char in _SENTENCE_END and index < len(text) - 1:
            text = text[:index]
            break
    for end in _SENTENCE_END:
        if text.endswith(end):
            text = text[:-1].strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for index in range(len(cut) - 1, max(8, limit // 2), -1):
        if cut[index] in _SOFT_END or cut[index] == " ":
            return cut[:index].strip(" ，,、")
    return cut.strip()


# Disfluencies that make a poor opening: stutters ("I I"), fillers ("um, so")
# and discourse markers ("I mean", "first of all") that carry no information.
_REPEAT = re.compile(r"\b([A-Za-z']+)(?:\s+\1\b)+", re.IGNORECASE)
_FILLER = re.compile(
    r"^(?:um+|uh+|er+|ah+|hmm+|like|so|well|okay|ok|right|yeah|yep|no|now|look|oh|hey)\b[\s,.:;—–-]*",
    re.IGNORECASE,
)
_DISCOURSE = re.compile(
    r"^(?:i mean|you know|i think|i guess|i feel like|first of all|to be honest|"
    r"kind of|sort of|at the end of the day|the thing is)\b[\s,]*",
    re.IGNORECASE,
)


def _clean(text: str) -> str:
    """Strip stutters, fillers and discourse markers from the front of a sentence."""
    cleaned = _SPEAKER.sub("", str(text or ""))
    for _ in range(5):
        before = cleaned
        cleaned = _FILLER.sub("", cleaned)
        cleaned = _DISCOURSE.sub("", cleaned)
        cleaned = _REPEAT.sub(r"\1", cleaned)
        if cleaned == before:
            break
    return cleaned.strip()


def _tidy(title: str) -> str:
    """Capitalise the opener and never end on a dangling word fragment."""
    title = title.strip(" ,，、-—–\"'")
    if not title:
        return title
    # A trailing "12" that came from "12 months" reads as a typo; drop a final
    # bare number or conjunction the trimmer left behind.
    title = re.sub(r"\s+(?:and|or|but|the|a|an|to|of|in|for|with|that|is|was|i|we|you)$", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\s+\d{1,2}$", "", title).strip()
    if title and title[0].islower():
        title = title[0].upper() + title[1:]
    return title


def _best_title(candidates: Sequence[str], limit: int = 46) -> str:
    """Pick the most nameable sentence: substantive, long enough, clean opening."""
    scored: list[tuple[float, str]] = []
    for candidate in candidates:
        cleaned = _clean(candidate)
        if len(cleaned) < 12:
            continue
        weight = min(len(cleaned), 70) / 70.0 - _opener_penalty(cleaned)
        scored.append((weight, cleaned))
    if not scored:
        return _tidy(_salient(_clean(candidates[0]), limit)) if candidates else ""
    scored.sort(key=lambda item: item[0], reverse=True)
    return _tidy(_salient(scored[0][1], limit))


def _opener_penalty(sentence: str) -> float:
    """A subject-led first sentence makes a far better title than 'and then ...'."""
    return 0.35 if _WEAK_OPENER.match(sentence.strip()) else 0.0


# ------------------------------------------------------------------ outline ---
TARGET_TOPIC_SEC = 150.0
MIN_TOPIC_SEC = 60.0


def build_outline(text: str) -> list[dict[str, Any]]:
    """Group sentences into topic-sized segments and title each at its opening."""
    sentences = _sentences(_cues(text))
    if not sentences:
        plain = re.sub(r"\s+", " ", text).strip()
        return [{"title": _salient(plain), "subtopics": []}] if plain else []

    segments: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for sentence in sentences:
        current.append(sentence)
        span = current[-1]["end"] - current[0]["start"]
        # Close a segment once it is topic-sized and the next sentence opens a
        # question or a fresh thought (a natural seam for a clip boundary).
        if span >= TARGET_TOPIC_SEC or (span >= MIN_TOPIC_SEC and sentence["text"].rstrip().endswith("?")):
            segments.append(current)
            current = []
    if current:
        segments.append(current)

    outline: list[dict[str, Any]] = []
    seen: set[str] = set()
    for segment in segments:
        sentence_pool = [item["text"] for item in segment]
        # Name the topic from the segment's opening sentences, skipping fillers.
        title = _best_title(sentence_pool[:4]) or _best_title(sentence_pool)
        if not title:
            continue
        key = title.lower()
        if key in seen:
            continue
        seen.add(key)
        subtopics = [_salient(_clean(item)) for item in sentence_pool[1:4] if _clean(item)]
        subtopics = [item for item in subtopics if item and item != title][:2]
        outline.append({"title": title, "subtopics": subtopics})
    return outline


# ----------------------------------------------------------------- timeline ---
DEFAULT_MIN_CLIP_SEC = 20.0
DEFAULT_MAX_CLIP_SEC = 90.0


def build_timeline(outlines: Sequence[dict[str, Any]], srt_text: str,
                   min_sec: float = DEFAULT_MIN_CLIP_SEC,
                   max_sec: float = DEFAULT_MAX_CLIP_SEC) -> list[dict[str, Any]]:
    """Cut the subtitle block into sentence-aligned windows and rank them.

    Every boundary is a real subtitle time, so the clip starts at the beginning
    of a sentence and ends at the end of one.
    """
    sentences = _sentences(_cues(srt_text))
    if not sentences or not outlines:
        return []
    if not min_sec or min_sec <= 0:
        min_sec = DEFAULT_MIN_CLIP_SEC

    windows: list[dict[str, Any]] = []
    for start_index, sentence in enumerate(sentences):
        end_index = start_index
        while end_index + 1 < len(sentences) and sentences[end_index]["end"] - sentence["start"] < max_sec:
            end_index += 1
        span = sentences[end_index]["end"] - sentence["start"]
        # Boundaries are whole sentences, so aim for the band rather than
        # guaranteeing it: a single sentence can exceed max_sec on its own.
        if span < min_sec * 0.75 or span > max_sec * 1.35:
            continue
        body = sentences[start_index:end_index + 1]
        joined = " ".join(item["text"] for item in body)
        windows.append({
            "start": sentence["start"],
            "end": sentences[end_index]["end"],
            "seconds": max(1.0, sentences[end_index]["end"] - sentence["start"]),
            "sentences": len(body),
            "chars": len(joined),
            "opener": sentence["text"],
            "text": joined,
        })
    if not windows:
        return []

    target = (min_sec + max_sec) / 2
    for window in windows:
        density = window["chars"] / window["seconds"]
        length_fit = 1.0 - min(1.0, abs(window["seconds"] - target) / max(1.0, max_sec))
        window["score"] = (
            min(density / 12.0, 1.0) * 0.50
            + min(window["sentences"] / 10.0, 1.0) * 0.25
            + length_fit * 0.25
            - _opener_penalty(window["opener"])
        )

    used: list[tuple[float, float]] = []
    timeline: list[dict[str, Any]] = []
    remaining = list(outlines)
    for window in sorted(windows, key=lambda item: item["score"], reverse=True):
        if len(timeline) >= len(outlines):
            break
        # Distinct clips: no more than 25% overlap with an already chosen window.
        if any(min(window["end"], end) - max(window["start"], start) > 0.25 * window["seconds"]
               for start, end in used):
            continue
        topic = remaining.pop(0) if remaining else {"title": "", "subtopics": []}
        used.append((window["start"], window["end"]))
        timeline.append({
            "outline": topic.get("title") or _salient(window["opener"]),
            "start_time": _to_stamp(window["start"]),
            "end_time": _to_stamp(window["end"]),
            "content": topic.get("subtopics") or [_salient(window["opener"])],
        })

    timeline.sort(key=lambda item: _to_seconds(item["start_time"]))
    return timeline


# ------------------------------------------------------------------ scoring ---
def score_clips(clips: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Score 0-1 on speech density, sentence completeness and content spread.

    Calibrated against the default 0.55 threshold seeded by the bootstrap: a
    solid talking segment lands near 0.65-0.8, a thin one near 0.45.
    """
    results: list[dict[str, Any]] = []
    for clip in clips:
        transcript = str(clip.get("transcript") or "")
        try:
            seconds = max(1.0, _to_seconds(clip.get("end_time")) - _to_seconds(clip.get("start_time")))
        except (TypeError, ValueError):
            seconds = 1.0
        chars = len(transcript)
        pieces = [item for item in re.split(r"(?<=[。！？!?])", transcript) if item.strip()]
        complete = sum(1 for item in pieces if item.strip() and item.strip()[-1] in _SENTENCE_END)
        density = chars / seconds

        raw = (
            min(density / 13.0, 1.0) * 0.40
            + min(chars / 500.0, 1.0) * 0.35
            + (complete / max(1, len(pieces))) * 0.25
        )
        score = round(0.40 + raw * 0.45, 3)
        reason = _best_title(pieces, 56) if pieces else "信息密度较高"
        results.append({
            "id": clip.get("id"),
            "outline": clip.get("outline"),
            "final_score": min(0.95, max(0.05, score)),
            "recommend_reason": reason,
        })
    return results


# -------------------------------------------------------------------- title ---
def build_titles(clips: Sequence[dict[str, Any]]) -> dict[str, str]:
    titles: dict[str, str] = {}
    for clip in clips:
        content = clip.get("content")
        pool = [str(clip.get("title") or "")]
        pool += [str(item) for item in content] if isinstance(content, list) else [str(content or "")]
        candidate = _best_title(pool, 38)
        titles[str(clip.get("id"))] = candidate or f"片段 {clip.get('id')}"
    return titles


# ---------------------------------------------------------------- clustering ---
_STOP = set("的了呢吗吧啊哦嗯这那个我你他她它是不没有就都也很还再又和与及在上下之其所以因为但是如果可以什么怎么一个我们他们你们".replace(" ", ""))


def _tokens(text: str) -> set[str]:
    cleaned = re.sub(r"[^\w\u4e00-\u9fff]+", " ", str(text or "").lower())
    words: set[str] = set()
    for token in cleaned.split():
        if len(token) >= 3:
            words.add(token)
        # CJK has no spaces: use character bigrams, which is a decent topic proxy.
        if re.fullmatch(r"[\u4e00-\u9fff]+", token):
            for index in range(len(token) - 1):
                pair = token[index:index + 2]
                if not (set(pair) & _STOP):
                    words.add(pair)
    return words - _STOP


def cluster_clips(clips: Sequence[dict[str, Any]], max_per: int = 5) -> list[dict[str, Any]]:
    """Greedy vocabulary clustering; only keeps groups of two or more."""
    groups: list[dict[str, Any]] = []
    for clip in clips:
        tokens = _tokens(f"{clip.get('title', '')} {clip.get('summary', '')}")
        best, best_overlap = None, 0
        for group in groups:
            overlap = len(tokens & group["tokens"])
            if overlap > best_overlap:
                best, best_overlap = group, overlap
        if best is not None and best_overlap >= 2 and len(best["clips"]) < max_per:
            best["clips"].append(clip)
            best["tokens"] |= tokens
        else:
            groups.append({"tokens": tokens, "clips": [clip]})
    return [
        {
            "collection_title": _salient(str(group["clips"][0].get("title", "")), 24) or "合集",
            "collection_summary": f"{len(group['clips'])} 个主题相近的片段",
            "clips": [str(clip.get("title", "")) for clip in group["clips"]],
        }
        for group in groups
        if len(group["clips"]) >= 2
    ]


# ------------------------------------------------------------------ routing ---
def answer(prompt: str, input_data: Any = None) -> str:
    """Return the reply body for one pipeline call."""
    payload = input_data
    if payload is None:
        match = _JSON_INPUT.search(prompt)
        if match:
            try:
                payload = json.loads(match.group(1))
            except ValueError:
                payload = None

    if _OUTLINE_MARKER in prompt:
        source = payload.get("text") if isinstance(payload, dict) else (payload or "")
        return json.dumps(build_outline(str(source or "")), ensure_ascii=False)

    if _TIMELINE_MARKER in prompt:
        outlines, srt_text = [], ""
        if isinstance(payload, dict):
            outlines = payload.get("outline") or []
            srt_text = str(payload.get("srt_text") or "")
        return json.dumps(build_timeline(outlines, srt_text), ensure_ascii=False)

    if _SCORING_MARKER in prompt:
        clips = payload if isinstance(payload, list) else []
        return json.dumps(score_clips(clips), ensure_ascii=False)

    if _TITLE_MARKER in prompt:
        clips = payload if isinstance(payload, list) else []
        return json.dumps(build_titles(clips), ensure_ascii=False)

    if _CLUSTER_MARKER in prompt:
        clips = _parse_cluster_prompt(prompt)
        return json.dumps(cluster_clips(clips), ensure_ascii=False)

    if _CLIP_FINDER_MARKER in prompt:
        return json.dumps(build_clip_windows(payload if isinstance(payload, dict) else {}), ensure_ascii=False)

    if _BURNED_MARKER in prompt:
        return json.dumps({"language": "none"}, ensure_ascii=False)

    if _BOUNDARY_MARKER in prompt:
        return json.dumps(build_boundary(payload if isinstance(payload, dict) else {}), ensure_ascii=False)

    if _PACKAGING_MARKER in prompt:
        return json.dumps(build_packaging(payload if isinstance(payload, dict) else {}), ensure_ascii=False)

    if _POST_COPY_MARKER in prompt:
        return json.dumps(build_posts(payload if isinstance(payload, dict) else {}), ensure_ascii=False)

    logger.info("offline engine: unrecognised prompt, returning empty array")
    return "[]"


def _parse_cluster_prompt(prompt: str) -> list[dict[str, Any]]:
    """The clustering prompt embeds its clips as a numbered prose list."""
    clips: list[dict[str, Any]] = []
    for line in prompt.split("\n"):
        match = re.match(r"\s*(\d+)\.\s*标题：(.*)", line)
        if match:
            clips.append({"id": match.group(1), "title": match.group(2).strip()})
        elif clips and "摘要：" in line:
            clips[-1]["summary"] = line.split("摘要：", 1)[1].strip()
        elif clips and "评分：" in line:
            try:
                clips[-1]["score"] = float(line.split("评分：", 1)[1].strip())
            except ValueError:
                pass
    return clips




# --------------------------------------------------------------- clip finder ---
def _clock(seconds: float) -> str:
    seconds = int(max(0.0, seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def build_clip_windows(payload: dict[str, Any]) -> dict[str, Any]:
    """Complete-answer windows from the ``rows`` block of the clip-finder prompt.

    Each row is ``index|m:ss|text``. A window starts where a question or a new
    statement starts and ends after the last sentence of the answer, so a cut
    never lands mid-sentence and never swallows the next question.
    """
    rows = []
    for line in str(payload.get("rows") or "").splitlines():
        parts = line.split("|", 2)
        if len(parts) != 3 or not parts[0].strip().isdigit():
            continue
        rows.append((int(parts[0]), parts[1].strip(), parts[2].strip()))
    if not rows:
        return {"clips": []}

    # Sentences, each remembering the row span it covers.
    sentences: list[tuple[int, int, str]] = []
    start, buffer = rows[0][0], ""
    for index, _, text in rows:
        buffer = f"{buffer}{text}".strip()
        if buffer and (buffer[-1] in "。！？!?." or len(buffer) >= 90):
            sentences.append((start, index, buffer))
            start, buffer = index + 1, ""
    if buffer:
        sentences.append((start, rows[-1][0], buffer))
    if not sentences:
        return {"clips": []}

    # A window is 1-2 sentences: long enough to carry a claim, short enough to post.
    windows: list[dict[str, Any]] = []
    step = 2
    for position in range(0, len(sentences), step):
        group = sentences[position:position + step]
        first_row, last_row = group[0][0], group[-1][1]
        body = "".join(item[2] for item in group)
        title = _pack_title_lines(body, 20, "zh")
        windows.append({
            "start": first_row,
            "end": last_row,
            "title": (title[0] if title else _salient(body, 20))[:20],
            "reason": f"完整观点：{_salient(body, 24)}",
            "score": 0.78 if len(body) >= 24 else 0.6,
        })
    return {"clips": windows[:5]}


def build_boundary(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep the proposed cut, moved off a half sentence when that is possible.

    The engine has no semantic judgement, so it does the one thing it can prove:
    never end on a row that is clearly unfinished, and never start on a
    continuation row. Everything else is left exactly as proposed.
    """
    lines = payload.get("lines") if isinstance(payload.get("lines"), list) else []
    texts = {item.get("id"): str(item.get("text") or "") for item in lines if isinstance(item, dict)}
    start = payload.get("start_line")
    end = payload.get("end_line")
    if not isinstance(start, int) or not isinstance(end, int):
        return {"start_line": start or 0, "end_line": end or 0, "reason": "保持原切点"}
    last = max(texts) if texts else 0
    # Do not end on an unfinished clause: extend while the next row continues it.
    while isinstance(end, int) and end < last and texts.get(end) and texts[end][-1] not in "。！？!?.":
        end += 1
    # Do not start on a continuation row that opens with a connective.
    while start < end and str(texts.get(start) or "").lstrip().startswith(("但是", "而且", "然后", "所以", "那")):
        start += 1
    return {"start_line": start, "end_line": end, "reason": "已对齐到完整句子"}


# ----------------------------------------------------------------- packaging ---
_LATIN_RUN = re.compile(r"[A-Za-z0-9]")


def _pack_line(text: str, limit: int) -> str:
    """Shorten to `limit` characters without cutting a word or leaving a dangling letter.

    ``为什么大多数公司做不好AI转型？`` at a 12-character cap must not become
    ``为什么大多数公司做不好A``: the Latin token is atomic, so it is dropped whole
    (and then any trailing connective) rather than halved into a meaningless letter.
    """
    text = str(text or "").strip().rstrip("，,、；;：:")
    if len(text) <= limit:
        return text
    cut = text[:limit]
    # Never stop inside a Latin/number run.
    while cut and _LATIN_RUN.match(cut[-1]) and len(cut) < len(text) and _LATIN_RUN.match(text[len(cut)]):
        cut = cut[:-1]
    cut = cut.rstrip("，,、；;：: 和与的了是在把对从").rstrip("。.！!？?") 
    return cut or text[:limit]


def _pack_title_lines(text: str, limit: int, audience: str) -> list[str]:
    """One or two title lines, each inside the platform's cap, broken at a natural joint.

    The packaging validator re-wraps anything longer than the cap and drops titles it
    cannot balance, so a title built here is wrapped by the same layout code the
    captions use: punctuation first, then soft joints, never through a CJK word.
    """
    from backend.services.studio.caption_layout import lines_for
    for sentence in _post_sentences(text)[:8]:
        clean = _salient(_clean(sentence), max(limit * 2, 24))
        if len(clean) < 4:
            continue
        clean = _pack_line(clean, max(limit * 2, 24))
        if not clean:
            continue
        if len(clean) <= limit:
            return [clean]
        if audience == "zh":
            wrapped = [line.strip().rstrip("，,、；;：:") for line in lines_for(clean, limit) if line.strip()]
            # Two lines are only worth it when both carry a phrase; a 4-character
            # tail ("好AI转型") is a broken word, so shorten to one line instead.
            if len(wrapped) == 2 and min(len(line) for line in wrapped) >= max(6, limit // 2):
                return wrapped
            return [_pack_line(clean, limit)]
        import textwrap
        wrapped = [line.strip() for line in textwrap.wrap(clean, width=limit) if line.strip()]
        if wrapped and len(wrapped) <= 2 and all(len(line) <= limit for line in wrapped):
            return wrapped
        return [_pack_line(clean, limit)]
    return []


_HAN = re.compile(r"[\u4e00-\u9fff]")
_KANA = re.compile(r"[\u3040-\u30ff]")
_HANGUL = re.compile(r"[\uac00-\ud7af]")
_STOPWORDS_EN = frozenset("""a an the and but or so because since while with without for from into
over under this that these those it its is are was were be been being do does did not no
you your we our they their he she his her them us i me my of to in on at as by about
what why how when where who which more most very really just also than then there here
can could would should will shall may might must have has had one two""".split())


def script_of(text: str) -> str:
    """Dominant writing system of a line: zh / en / other."""
    text = str(text or "")
    han, kana, hangul = len(_HAN.findall(text)), len(_KANA.findall(text)), len(_HANGUL.findall(text))
    if kana or hangul:
        return "other"
    if han:
        return "zh"
    latin = len(re.findall(r"[A-Za-z]", text))
    return "en" if latin >= 3 else "other"


def keywords(text: str, count: int = 4) -> list[str]:
    """Topic words that literally occur in `text`, most specific first.

    A key-free engine cannot write new words, so an honest label for a clip is built
    from the words the clip already uses: Latin tokens of four letters or more that
    are not function words, or Chinese 2-4 character runs. Nothing is invented, so a
    generated caption can never claim something the speaker did not say.
    """
    text = str(text or "")
    picked: list[str] = []
    seen: set[str] = set()
    for word in re.findall(r"[A-Za-z][A-Za-z'-]{3,}", text):
        key = word.lower()
        if key in _STOPWORDS_EN or key in seen:
            continue
        seen.add(key)
        picked.append(word)
    for run in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        for size in (4, 3, 2):
            for start in range(0, len(run) - size + 1):
                gram = run[start:start + size]
                if len(picked) >= count:
                    return picked[:count]
                if gram not in seen and not any(gram in kept for kept in picked):
                    seen.add(gram)
                    picked.append(gram)
            if len(picked) >= count:
                return picked[:count]
    return picked[:count]


def describe_clip(text: str, audience: str = "en", limit: int = 60) -> list[str]:
    """One honest line describing a clip, in the audience's script.

    This is what a key-free deployment shows instead of a translation: it says what
    the clip is about using only its own words. Zeros on the screen of an English
    platform are worse than a real Chinese title, and an invented translation is
    worse than both.
    """
    words = keywords(text, 5)
    if not words:
        return []
    if audience == "en":
        head = " · ".join(words[:3])
        line = _pack_line(f"On {head}" if len(words) > 3 else head, limit)
        return [line] if line else []
    head = "、".join(words[:3])
    line = _pack_line(head, 20)
    return [line] if line else []


def build_packaging(payload: dict[str, Any]) -> dict[str, Any]:
    """Packaging metadata derived from the transcript.

    The model is asked for titles, sentence segments, speakers, emphasis marks
    and tags. Everything here is extractive: titles come from the clip's own
    opening sentences, segments rejoin ASR fragments into complete sentences,
    emphasis is a substring of the line it points at, and speakers are left
    empty unless the transcript actually names someone.
    """
    template = str(payload.get("template") or "interview_zh")
    audience = str(payload.get("audience_language") or "zh")
    limit = int(payload.get("title_limit") or (20 if audience == "zh" else 60))
    raw_lines = payload.get("lines") if isinstance(payload.get("lines"), list) else []
    lines = [str((item or {}).get("text") or "") for item in raw_lines if isinstance(item, dict)]
    text = " ".join(line for line in lines if line)

    if payload.get("translate") is True:
        # No second language exists here. Keeping the source text with captions is the
        # documented fallback: a readable clip beats an empty one.
        audience = "zh" if script_of(text) == "zh" else "en"
        limit = int(payload.get("title_limit") or (20 if audience == "zh" else 60))
        title_lines = _pack_title_lines(text, limit, audience) or describe_clip(text, audience)
    else:
        title_lines = _pack_title_lines(text, limit, audience)

    # Rejoin ASR fragments into sentences, then map each sentence back to the
    # line ids it covers: segments must be contiguous, ordered, non-overlapping
    # and cover every line. The validator requires them whenever it expects a
    # translation, and the text is the source row (nothing is invented here).
    segments: list[dict[str, Any]] = []
    if lines:
        start, buffer = 0, ""
        for index, line in enumerate(lines):
            buffer = f"{buffer} {line}".strip()
            sentence_end = bool(buffer) and buffer[-1] in "。！？!?.;；"
            long_enough = len(buffer) >= 40
            if (sentence_end or long_enough) and index - start < 4:
                segments.append({"from": start, "to": index})
                start, buffer = index + 1, ""
        if start < len(lines):
            segments.append({"from": start, "to": len(lines) - 1})
        for segment in segments:
            segment["text"] = _clean(" ".join(lines[segment["from"]:segment["to"] + 1]))[:600]

    # A highlight must be a literal substring of the line it points at.
    highlights: list[dict[str, Any]] = []
    if template == "podcast_en":
        for index, line in enumerate(lines):
            if index % 3:
                continue
            words = [w for w in re.findall(r"[A-Za-z][A-Za-z'-]{4,}", line) if w.lower() not in _EN_STOP]
            if words:
                highlights.append({"line": index, "word": words[0]})

    # Editorial notes only exist for the interview template, and only when the
    # transcript supplies a concrete noun phrase to point at.
    tags: list[dict[str, Any]] = []
    if template == "interview_zh" and audience == "zh":
        for index, line in enumerate(lines):
            phrase = _post_tags(line, 1, "zh")
            if phrase:
                tags.append({"line": index, "text": phrase[0][:10]})
            if len(tags) >= 3:
                break

    marks: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        phrase = _post_tags(line, 1, audience if audience == "en" else "zh")
        if not phrase:
            continue
        word = phrase[0]
        if word not in line:
            continue
        marks.append({"line": index, "text": word})

    mood = "calm"
    lowered = text.casefold()
    if any(word in lowered for word in ("错", "失败", "危机", "风险", "问题", "fail", "risk", "crisis", "wrong")):
        mood = "serious"
    elif any(word in lowered for word in ("必须", "一定", "关键", "核心", "根本", "must", "never", "critical")):
        mood = "bold"

    return {
        "title_lines": title_lines,
        "accent_line": 0 if len(title_lines) > 1 else None,
        "segments": segments,
        "speakers": [],
        "tags": tags,
        "highlights": highlights,
        "marks": marks,
        "mood": mood,
    }


# ----------------------------------------------------------------- post copy ---
# Publishing metadata is derived from the transcript, not invented. Everything
# here is extractive: a sentence the speaker actually said becomes the title and
# description; tags are words that literally appear in the lines.
_POST_SENTENCE_END = "。！？!?.;；\n"
# Words that carry no topic. Tags built from these would be noise on a post.
_EN_STOP = frozenset("""about after again against also because been before being between
both could does doing down during each from further have having here into itself
just like more most much must only other over same should some such than that the
their them then there these they this those through under until very were what when
where which while with would your you're it's don't didn't doesn't isn't aren't
really going gonna want know think right okay yeah well thing things people""".split())
_ZH_BAD_LEAD = frozenset("一二三四五六七八九十百千万亿几个每这那哪些个的了着过和与及或就都也还很更最把被从对向为以及是不没要能会可该得地之其")
_ZH_STOP = frozenset("""这个 那个 我们 你们 他们 就是 然后 所以 但是 因为 如果 这样 那样 什么 怎么
可以 一个 没有 知道 觉得 其实 现在 时候 东西 事情 真的 非常 应该 需要 可能 不是 这么 那么
自己 大家 很多 一直 已经 还是 或者 而且 比如 这里 那里 的话 这种 那种""".split())
_CJK = re.compile(r"[\u3400-\u9fff]")


def _post_sentences(text: str) -> list[str]:
    plain = re.sub(r"\s+", " ", re.sub(r">>+\s*", " ", str(text or ""))).strip()
    parts, buffer = [], ""
    for char in plain:
        buffer += char
        if char in _POST_SENTENCE_END:
            if buffer.strip(_POST_SENTENCE_END + " "):
                parts.append(buffer.strip())
            buffer = ""
    if buffer.strip():
        parts.append(buffer.strip())
    return [part for part in parts if len(part) >= 8]


def _post_fit(text: str, limit: int) -> str:
    """Cut to `limit` chars at a sentence or clause end, mirroring post_copy._fit."""
    if limit <= 0 or len(text) <= limit:
        return text
    cut = text[:limit]
    best = max((cut.rfind(mark) for mark in "。！？!?.；;，, "), default=-1)
    return (cut[:best + 1] if best >= limit * 0.6 else cut).rstrip("，,;； ").strip()


def _post_tags(text: str, count: int, language: str) -> list[str]:
    """Pick topic words that literally occur in the transcript.

    Only repeated words are kept. A word the speaker returns to is the subject of
    the clip; a word that appears once is usually a fragment, and a hashtag built
    from a fragment is worse than one fewer hashtag.
    """
    if count <= 0 or not text.strip():
        return []
    picked: list[str] = []
    if language == "en":
        seen: set[str] = set()
        counts: dict[str, int] = {}
        for match in re.finditer(r"[A-Za-z][A-Za-z'-]{3,}", text):
            word = match.group(0)
            if word.lower() in _EN_STOP:
                continue
            counts[word] = counts.get(word, 0) + 1
        for word, hits in sorted(counts.items(), key=lambda item: (-item[1], text.index(item[0]))):
            if hits < 2 or len(picked) >= count:
                continue
            if word.lower() in seen or any(word.lower() in kept.lower() for kept in picked):
                continue
            seen.add(word.lower())
            picked.append(word)
        return picked

    runs = re.findall(r"[\u3400-\u9fff]{2,}", text)
    chosen: list[tuple[int, str]] = []
    # Longest first, so "组织变革" wins over the "组织" fragment inside it.
    for size in (5, 4, 3, 2):
        for run in runs:
            for start_at in range(0, len(run) - size + 1):
                gram = run[start_at:start_at + size]
                if gram in _ZH_STOP or len(chosen) >= count * 3:
                    continue
                # "一个业务"/"三十家" start mid-phrase: a classifier or numeral
                # cannot begin a topic word, so drop the fragment.
                if gram[0] in _ZH_BAD_LEAD:
                    continue
                if text.count(gram) < 2:
                    continue
                if any(gram in kept or kept in gram for _, kept in chosen):
                    continue
                chosen.append((start_at, gram))
        if len(chosen) >= count:
            break
    ranked = sorted(chosen, key=lambda item: (-text.count(item[1]), -len(item[1]), item[0]))
    for _, gram in ranked:
        if len(picked) >= count:
            break
        picked.append(gram)
    return picked


def build_posts(payload: dict[str, Any]) -> dict[str, Any]:
    """Per-platform title/description/tags for one clip, from the transcript."""
    lines = str(payload.get("lines") or "")
    title_hint = _salient(_clean(str(payload.get("title_hint") or "")), 60)
    source = str(payload.get("source") or "").strip()
    platforms = [str(p) for p in (payload.get("platforms") or []) if isinstance(p, str)]
    rules = payload.get("rules") if isinstance(payload.get("rules"), dict) else {}

    sentences = _post_sentences(lines)
    # The opening sentence of a clip is the hook the renderer selected, so it is
    # the honest first choice for a title; fall back to the model's own hint.
    hook = _tidy(_clean(sentences[0])) if sentences else ""
    body = " ".join(sentences[1:4]) if len(sentences) > 1 else ""
    posts: dict[str, Any] = {}
    for platform in platforms:
        rule = rules.get(platform) if isinstance(rules.get(platform), dict) else {}
        language = str(rule.get("language") or "zh")
        title_max = int(rule.get("title_max") or 60)
        description_max = int(rule.get("description_max") or 300)
        tag_span = str(rule.get("tags") or "3-5")
        low, _, high = tag_span.partition("-")
        tag_max = int(high or low or 5)

        chinese_platform = language != "en"
        candidate = title_hint or hook
        if chinese_platform and not _CJK.search(candidate):
            candidate = hook or title_hint
        if not chinese_platform:
            english = hook or title_hint
            # English platforms never accept a Chinese title (post_copy enforces
            # this too); if the transcript has no English the field stays empty
            # so the user fills it rather than shipping a wrong-language post.
            candidate = english if not _CJK.search(english or "") else ""
        title = _post_fit(candidate, title_max)

        description = ""
        if sentences:
            description = " ".join(sentences[:3])
            if source:
                description = f"{description}（来源：{source}）"
            description = _post_fit(description, description_max)

        tags = _post_tags(lines, tag_max, language)
        if platform == "xiaohongshu":
            tags = [_post_fit(tag, 20) for tag in tags]
        posts[platform] = {"title": title, "description": description, "tags": tags}
    return {"posts": posts}


# ------------------------------------------------------------------- server ---
def _completion(content: str) -> dict[str, Any]:
    return {
        "id": f"chatcmpl-offline-{int(time.time() * 1000)}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": MODEL_ID,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
    }


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003 - stdlib hook
        logger.debug("offline engine: " + fmt, *args)

    def _send(self, status: int, body: dict[str, Any]) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802 - stdlib hook
        if self.path.rstrip("/").endswith("/models"):
            self._send(200, {"object": "list", "data": [{
                "id": MODEL_ID, "object": "model", "created": 0, "owned_by": "autoclip",
            }]})
            return
        if self.path.rstrip("/").endswith("/health"):
            self._send(200, {"status": "ok", "model": MODEL_ID})
            return
        self._send(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:  # noqa: N802 - stdlib hook
        if not self.path.rstrip("/").endswith("/chat/completions"):
            self._send(404, {"error": {"message": "not found"}})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            request = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, OSError):
            self._send(400, {"error": {"message": "invalid JSON body"}})
            return

        messages = request.get("messages") or []
        prompt = "\n".join(
            str(message.get("content") or "")
            for message in messages
            if isinstance(message, dict) and message.get("role") == "user"
        )
        try:
            content = answer(prompt, None)
        except Exception as error:  # noqa: BLE001 - a bad chunk must not kill the server
            logger.exception("offline engine failed: %s", type(error).__name__)
            content = "[]"
        self._send(200, _completion(content))


class _Server:
    def __init__(self) -> None:
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        # Port may be assigned by the kernel, so the bound URL is remembered.
        self.url: str = ""

    def start(self, host: str = LOCAL_HOST, port: int = DEFAULT_PORT) -> str:
        if self._httpd is not None:
            return self.url
        # A fixed port is a deployment hazard: two worker processes, a restarted
        # instance that still holds the old socket, or a serverless sandbox
        # shared between invocations all collide on 8100. Bind port 0 and read
        # back what the kernel assigned; the caller uses the returned URL.
        httpd = None
        if port:
            try:
                httpd = ThreadingHTTPServer((host, port), _Handler)
            except OSError:
                logger.info("offline engine port %s busy, falling back to a free port", port)
        if httpd is None:
            httpd = ThreadingHTTPServer((host, 0), _Handler)
        httpd.daemon_threads = True
        self._httpd = httpd
        bound = httpd.server_address[1]
        self.url = f"http://{host}:{bound}/v1"
        self._thread = threading.Thread(target=httpd.serve_forever, name="offline-engine", daemon=True)
        self._thread.start()
        logger.info("offline analysis engine listening on %s", self.url)
        return self.url

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None


_SERVER = _Server()


def start_if_enabled() -> str | None:
    """Start the in-process endpoint when the deployment asked for it."""
    if not enabled():
        return None
    host = os.getenv(ENV_HOST, LOCAL_HOST)
    try:
        port = int(os.getenv(ENV_PORT, str(DEFAULT_PORT)))
    except ValueError:
        port = DEFAULT_PORT
    return _SERVER.start(host, port)


def stop() -> None:
    _SERVER.stop()
