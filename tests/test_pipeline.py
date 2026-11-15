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
