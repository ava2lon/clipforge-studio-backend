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
