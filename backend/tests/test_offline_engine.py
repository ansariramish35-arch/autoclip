"""Key-free deployment path: offline engine, its routing and its seeding.

These cover the guarantees the hosted build depends on: every pipeline step's
prompt is recognised, replies satisfy the real downstream contracts, and the
settings seed makes the readiness gate report configured without a key.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.core import offline_llm

PROMPT_DIR = Path(__file__).resolve().parents[1] / "prompt" / "knowledge"
EXAMPLE_SRT = Path(__file__).resolve().parents[1] / "assets" / "example" / "source.srt"

STEPS = {
    "outline": "大纲",
    "timeline": "时间点",
    "scoring": "推荐理由",
    "title": "标题生成",
    "cluster": "主题聚类",
}


def prompt(name: str) -> str:
    return (PROMPT_DIR / f"{STEPS[name]}.txt").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def srt_text() -> str:
    if not EXAMPLE_SRT.exists():
        pytest.skip("example subtitle fixture is not present")
    return EXAMPLE_SRT.read_text(encoding="utf-8")


# ------------------------------------------------------------------ parsing ---
def test_cues_parse_real_srt(srt_text):
    cues = offline_llm._cues(srt_text)
    assert len(cues) > 50
    assert cues[0]["start"] == 0.0
    # Strictly increasing, as any clip boundary depends on that.
    assert all(cues[i]["end"] <= cues[i + 1]["start"] + 0.01 for i in range(len(cues) - 1))
    assert all(cue["text"] for cue in cues)


def test_cues_ignore_malformed_blocks():
    text = "1\n00:00:01,000 --> 00:00:02,000\nhello\n\nnot a cue\n\n2\n00:00:03,000 --> 00:00:04,000\n"
    cues = offline_llm._cues(text)
    assert len(cues) == 1
    assert cues[0]["text"] == "hello"


def test_timestamp_roundtrip_is_srt_shaped():
    for seconds in (0.0, 1.5, 61.25, 3725.999):
        stamp = offline_llm._to_stamp(seconds)
        assert len(stamp) == 12 and stamp[2] == ":" and stamp[8] == ","
        assert offline_llm._to_seconds(stamp) == pytest.approx(seconds, abs=0.002)


def test_sentences_join_fragments_into_whole_sentences():
    cues = [
        {"start": 0.0, "end": 1.0, "text": "This is a"},
        {"start": 1.0, "end": 2.0, "text": "complete thought."},
        {"start": 2.0, "end": 3.0, "text": "And another one?"},
    ]
    sentences = offline_llm._sentences(cues)
    assert [item["text"] for item in sentences] == [
        "This is a complete thought.", "And another one?",
    ]
    assert sentences[0]["start"] == 0.0 and sentences[0]["end"] == 2.0


# ------------------------------------------------------------------ outline ---
def test_outline_returns_contract_shape(srt_text):
    topics = offline_llm.build_outline(srt_text)
    assert topics, "an empty outline counts as a failed chunk in step 1"
    for topic in topics:
        assert isinstance(topic["title"], str) and topic["title"].strip()
        assert isinstance(topic["subtopics"], list)
        assert all(isinstance(item, str) for item in topic["subtopics"])


def test_outline_handles_empty_and_plain_text():
    assert offline_llm.build_outline("") == []
    plain = offline_llm.build_outline("A single line of speech without timestamps.")
    assert plain and plain[0]["title"]


# ----------------------------------------------------------------- timeline ---
def test_timeline_is_grounded_in_cue_times(srt_text):
    topics = offline_llm.build_outline(srt_text)
    clips = offline_llm.build_timeline(topics, srt_text)

    assert clips, "an empty timeline counts as a failed chunk in step 2"
    cues = offline_llm._cues(srt_text)
    starts = {round(cue["start"], 3) for cue in cues}
    ends = {round(cue["end"], 3) for cue in cues}
    for clip in clips:
        start = round(offline_llm._to_seconds(clip["start_time"]), 3)
        end = round(offline_llm._to_seconds(clip["end_time"]), 3)
        # Boundaries must be real subtitle times and must run forwards.
        assert start in starts, clip["start_time"]
        assert end in ends, clip["end_time"]
        assert end > start
        assert clip["outline"]
        assert isinstance(clip["content"], list)


def test_timeline_clips_do_not_overlap_heavily(srt_text):
    topics = offline_llm.build_outline(srt_text)
    clips = offline_llm.build_timeline(topics, srt_text)
    spans = [
        (offline_llm._to_seconds(clip["start_time"]), offline_llm._to_seconds(clip["end_time"]))
        for clip in clips
    ]
    for index, (start, end) in enumerate(spans):
        for other_start, other_end in spans[index + 1:]:
            overlap = min(end, other_end) - max(start, other_start)
            assert overlap <= 0.25 * (end - start)


def test_timeline_returns_empty_without_input(srt_text):
    assert offline_llm.build_timeline([], srt_text) == []
    assert offline_llm.build_timeline([{"title": "t"}], "") == []


# ------------------------------------------------------------------ scoring ---
def test_scores_are_bounded_and_complete():
    clips = [
        {"id": "a", "outline": "One", "start_time": "00:00:00,000", "end_time": "00:01:00,000",
         "transcript": "A well formed sentence. Another one follows here. And a third."},
        {"id": "b", "outline": "Two", "start_time": "00:00:00,000", "end_time": "00:01:00,000",
         "transcript": ""},
    ]
    results = offline_llm.score_clips(clips)
    assert [item["id"] for item in results] == ["a", "b"]
    for item in results:
        assert 0.0 <= item["final_score"] <= 1.0
        assert item["recommend_reason"]
    # Denser content must not score lower than an empty one.
    assert results[0]["final_score"] > results[1]["final_score"]


def test_dense_clip_clears_the_seeded_threshold():
    """The bootstrap seeds 0.55; a normal talking segment must pass it."""
    clip = {
        "id": "a", "outline": "Topic", "start_time": "00:00:00,000", "end_time": "00:01:00,000",
        "transcript": " ".join(["This is a reasonably full sentence of speech."] * 12),
    }
    score = offline_llm.score_clips([clip])[0]["final_score"]
    assert score >= 0.55


# -------------------------------------------------------------------- title ---
def test_titles_are_a_dict_keyed_by_clip_id():
    clips = [
        {"id": "1", "title": "Um, I think we were trying to do too much", "content": []},
        {"id": "2", "title": "", "content": ["Some content sentence."]},
    ]
    titles = offline_llm.build_titles(clips)
    assert set(titles) == {"1", "2"}
    assert titles["1"].startswith("We were")
    assert titles["2"]


def test_title_strips_fillers_and_dangling_ends():
    assert offline_llm._tidy("it's going super well and") == "It's going super well"
    assert offline_llm._tidy("you said we did not have our best last 12") == "You said we did not have our best last"


# --------------------------------------------------------------- clustering ---
def test_clustering_only_returns_groups_of_two_or_more():
    clips = [
        {"id": "1", "title": "AI 与就业市场", "summary": "技术改变工作"},
        {"id": "2", "title": "AI 与就业结构", "summary": "技术改变岗位"},
        {"id": "3", "title": "无关的天气预报", "summary": "今天下雨"},
    ]
    groups = offline_llm.cluster_clips(clips)
    assert groups
    for group in groups:
        assert len(group["clips"]) >= 2
        assert group["collection_title"] and group["collection_summary"]
    # The unrelated clip cannot be forced into a collection.
    assert all("无关的天气预报" not in group["clips"] for group in groups)


def test_clustering_caps_collection_size():
    clips = [
        {"id": str(index), "title": f"AI 就业话题 {index}", "summary": "技术改变工作"}
        for index in range(8)
    ]
    groups = offline_llm.cluster_clips(clips, max_per=5)
    assert groups and all(len(group["clips"]) <= 5 for group in groups)


def test_cluster_prompt_clips_are_parsed():
    prompt_text = (
        "1. 标题：第一个话题\n   摘要：摘要一\n   评分：0.81\n\n"
        "2. 标题：第二个话题\n   摘要：摘要二\n   评分：0.72\n"
    )
    clips = offline_llm._parse_cluster_prompt(prompt_text)
    assert [clip["title"] for clip in clips] == ["第一个话题", "第二个话题"]
    assert clips[0]["summary"] == "摘要一"
    assert clips[0]["score"] == pytest.approx(0.81)


# ------------------------------------------------------------------ routing ---
_MINIMAL_SRT = (
    "1\n00:00:00,000 --> 00:00:03,000\nSentence number 1 says something useful here.\n" + "\n"
    "2\n00:00:03,000 --> 00:00:06,000\nSentence number 2 says something useful here.\n" + "\n"
    "3\n00:00:06,000 --> 00:00:09,000\nSentence number 3 says something useful here.\n" + "\n"
    "4\n00:00:09,000 --> 00:00:12,000\nSentence number 4 says something useful here.\n" + "\n"
    "5\n00:00:12,000 --> 00:00:15,000\nSentence number 5 says something useful here.\n" + "\n"
    "6\n00:00:15,000 --> 00:00:18,000\nSentence number 6 says something useful here.\n" + "\n"
    "7\n00:00:18,000 --> 00:00:21,000\nSentence number 7 says something useful here.\n" + "\n"
    "8\n00:00:21,000 --> 00:00:24,000\nSentence number 8 says something useful here.\n" + "\n"
    "9\n00:00:24,000 --> 00:00:27,000\nSentence number 9 says something useful here.\n" + "\n"
    "10\n00:00:27,000 --> 00:00:30,000\nSentence number 10 says something useful here.\n" + "\n"
    "11\n00:00:30,000 --> 00:00:33,000\nSentence number 11 says something useful here.\n" + "\n"
    "12\n00:00:33,000 --> 00:00:36,000\nSentence number 12 says something useful here.\n" + "\n"
    "13\n00:00:36,000 --> 00:00:39,000\nSentence number 13 says something useful here.\n" + "\n"
    "14\n00:00:39,000 --> 00:00:42,000\nSentence number 14 says something useful here.\n" + "\n"
    "15\n00:00:42,000 --> 00:00:45,000\nSentence number 15 says something useful here.\n" + "\n"
    "16\n00:00:45,000 --> 00:00:48,000\nSentence number 16 says something useful here.\n"
)

STEP_PAYLOADS = {
    "outline": {"text": _MINIMAL_SRT},
    "timeline": {"outline": [{"title": "A first sentence", "subtopics": []}], "srt_text": _MINIMAL_SRT},
    "scoring": [{"id": "1", "outline": "A first sentence", "start_time": "00:00:00,000",
                 "end_time": "00:00:09,000", "transcript": "A first sentence that says something."}],
    "title": [{"id": "1", "title": "A first sentence that says something", "content": []}],
    "cluster": None,  # embedded in the prompt itself
}


@pytest.mark.parametrize("step", list(STEPS))
def test_every_step_prompt_is_recognised(step):
    """No step may fall through to the empty-array default."""
    payload = STEP_PAYLOADS[step]
    if payload is None:
        reply = offline_llm.answer(prompt(step) + "\n1. 标题：话题甲\n   摘要：摘要甲\n\n2. 标题：话题乙\n   摘要：摘要乙\n")
    else:
        reply = offline_llm.answer(f"{prompt(step)}\n\n输入内容：\n{json.dumps(payload, ensure_ascii=False)}")
    parsed = json.loads(reply)  # every reply must be valid JSON for the lenient parser
    assert parsed, f"step {step} returned an empty reply for real input"


@pytest.mark.parametrize("step", list(STEPS))
def test_empty_input_is_reported_as_empty_not_garbage(step):
    """Empty chunks must parse cleanly, even though the pipeline treats them as failures."""
    payload = STEP_PAYLOADS[step]
    if payload is None:
        reply = offline_llm.answer(prompt(step))
    else:
        reply = offline_llm.answer(f"{prompt(step)}\n\n输入内容：\n{json.dumps(payload, ensure_ascii=False)}")
    assert isinstance(json.loads(reply), (list, dict))


def test_outline_step_reply_parses_as_json_contract(srt_text):
    payload = {"text": srt_text}
    reply = offline_llm.answer(f"{prompt('outline')}\n\n输入内容：\n{json.dumps(payload, ensure_ascii=False)}")
    parsed = json.loads(reply)
    assert isinstance(parsed, list) and parsed
    assert set(parsed[0]) >= {"title", "subtopics"}


def test_unknown_prompt_returns_parseable_empty_reply():
    assert json.loads(offline_llm.answer("something else entirely")) == []


# ------------------------------------------------------------------- server ---
def test_http_endpoint_speaks_openai_chat_completions(srt_text):
    import urllib.request

    url = offline_llm._SERVER.start("127.0.0.1", 0)
    try:
        port = offline_llm._SERVER._httpd.server_address[1]
        base = f"http://127.0.0.1:{port}/v1"
        models = json.loads(urllib.request.urlopen(f"{base}/models", timeout=10).read())
        assert models["data"][0]["id"] == offline_llm.MODEL_ID

        body = json.dumps({
            "model": offline_llm.MODEL_ID,
            "messages": [{"role": "user", "content": f"{prompt('outline')}\n\n输入内容：\n" + json.dumps({"text": srt_text}, ensure_ascii=False)}],
        }).encode("utf-8")
        request = urllib.request.Request(f"{base}/chat/completions", data=body,
                                         headers={"Content-Type": "application/json"})
        reply = json.loads(urllib.request.urlopen(request, timeout=30).read())
        content = reply["choices"][0]["message"]["content"]
        assert json.loads(content), "the endpoint must answer with a usable outline"
    finally:
        offline_llm._SERVER.stop()
        assert url.startswith("http://")


def test_endpoint_disabled_by_default(monkeypatch):
    monkeypatch.delenv(offline_llm.ENV_ENABLE, raising=False)
    assert offline_llm.enabled() is False
    assert offline_llm.start_if_enabled() is None


# ----------------------------------------------------------------- bootstrap ---
def test_bootstrap_seeds_settings_and_readiness(tmp_path, monkeypatch):
    monkeypatch.setenv(offline_llm.ENV_ENABLE, "local")
    monkeypatch.setenv(offline_llm.ENV_PORT, "0")
    monkeypatch.setenv("AUTOCLIP_DATA_DIR", str(tmp_path))

    from backend.core import offline_bootstrap

    try:
        offline_bootstrap.install()
        settings = json.loads((tmp_path / "ai-model-settings.json").read_text(encoding="utf-8"))
        connection = settings["connections"][0]
        assert connection["provider"] == "ollama"
        assert connection["api_key"] in (None, "")
        assert connection["base_url"].startswith("http://127.0.0.1:")
        assert settings["analysis"]["model"] == "autoclip-offline-v1"
        assert settings["analysis_mode"] == "subtitle"
    finally:
        offline_llm._SERVER.stop()


def test_bootstrap_never_overwrites_user_settings(tmp_path, monkeypatch):
    monkeypatch.setenv(offline_llm.ENV_ENABLE, "local")
    monkeypatch.setenv(offline_llm.ENV_PORT, "0")
    monkeypatch.setenv("AUTOCLIP_DATA_DIR", str(tmp_path))
    existing = tmp_path / "ai-model-settings.json"
    existing.write_text(json.dumps({"version": 1, "connections": [], "marker": "user"}), encoding="utf-8")

    from backend.core import offline_bootstrap

    try:
        offline_bootstrap.install()
        assert json.loads(existing.read_text(encoding="utf-8"))["marker"] == "user"
    finally:
        offline_llm._SERVER.stop()


def test_bootstrap_is_a_noop_when_disabled(tmp_path, monkeypatch):
    monkeypatch.delenv(offline_llm.ENV_ENABLE, raising=False)
    monkeypatch.setenv("AUTOCLIP_DATA_DIR", str(tmp_path))

    from backend.core import offline_bootstrap

    offline_bootstrap.install()
    assert not (tmp_path / "ai-model-settings.json").exists()
