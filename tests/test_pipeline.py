from renderer.pipeline import _rank_candidates


def test_rank_candidates_respects_duration_and_deduplicates():
    segments = [
        {"start": i * 3.0, "end": i * 3.0 + 2.5, "text": "Here is the truth about this important topic?"}
        for i in range(30)
    ]
    clips = _rank_candidates(segments, [30.0], 120.0)
    assert clips
    assert all(20 <= clip["duration"] <= 60 for clip in clips)
    assert len(clips) <= 8
    for left, right in zip(clips, clips[1:]):
        assert left["end"] <= right["start"]


def test_rank_candidates_empty_transcript():
    assert _rank_candidates([], [], 90.0) == []


def test_parse_auto_editor_v1_kept_ranges():
    from renderer.pipeline import _parse_auto_editor_timeline

    timeline = {
        "version": "1",
        "timebase": "30/1",
        "chunks": [[0, 30, 1], [30, 60, 99999], [60, 120, 1]],
    }
    assert _parse_auto_editor_timeline(timeline) == [(0.0, 1.0), (2.0, 4.0)]


def test_parse_auto_editor_v1_fractional_timebase():
    from renderer.pipeline import _parse_auto_editor_timeline

    timeline = {"version": "1", "timebase": "30000/1001", "chunks": [[0, 30000, 1]]}
    assert _parse_auto_editor_timeline(timeline) == [(0.0, 1001.0)]



def test_rank_candidates_exposes_explainable_score_factors():
    segments = [
        {"start": i * 3.0, "end": i * 3.0 + 2.5,
         "text": "Here is the surprising truth about dogs and why this matters?"}
        for i in range(30)
    ]
    clips = _rank_candidates(segments, [30.0], 120.0)
    assert clips
    clip = clips[0]
    assert 0 <= clip["virality_score"] <= 100
    assert clip["score"] == clip["virality_score"]
    assert set(clip["score_factors"]) == {
        "speech_density", "hook_strength", "context_completeness",
        "ending_strength", "scene_alignment", "audio_activity", "prompt_relevance"
    }
    assert clip["score"] == round(sum(clip["score_factors"].values()), 1)


def test_rank_candidates_prompt_discovery_filters_unmatched_topics():
    segments = [
        {"start": i * 3.0, "end": i * 3.0 + 2.5,
         "text": ("The dog learned an important lesson about trust and friendship."
                  if i < 15 else
                  "Here is the truth about financial markets and investing?")}
        for i in range(30)
    ]
    clips = _rank_candidates(segments, [], 120.0, prompt="dog friendship")
    assert clips
    assert all("dog" in clip["transcript"].lower() for clip in clips)
    assert all(clip["prompt_match"] > 0 for clip in clips)


def test_rank_candidates_prompt_with_no_matches_returns_empty():
    segments = [
        {"start": i * 3.0, "end": i * 3.0 + 2.5,
         "text": "A story about financial markets and investing."}
        for i in range(30)
    ]
    assert _rank_candidates(segments, [], 120.0, prompt="astronaut cooking") == []
