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



def test_aspect_dimensions_supports_social_formats():
    from renderer.pipeline import _aspect_dimensions
    assert _aspect_dimensions("9:16") == (1080, 1920)
    assert _aspect_dimensions("1:1") == (1080, 1080)
    assert _aspect_dimensions("16:9") == (1920, 1080)


def test_aspect_dimensions_rejects_unsupported_format():
    import pytest
    from renderer.pipeline import PipelineError, _aspect_dimensions
    with pytest.raises(PipelineError):
        _aspect_dimensions("4:5")



def test_focus_keyframes_are_smoothed_and_keep_timestamps():
    from renderer.pipeline import _smooth_focus_keyframes

    points = [
        {"time": 0.0, "x": 0.2, "y": 0.4},
        {"time": 1.0, "x": 0.8, "y": 0.6},
        {"time": 2.0, "x": 0.4, "y": 0.8},
    ]
    result = _smooth_focus_keyframes(points, radius=1)
    assert [item["time"] for item in result] == [0.0, 1.0, 2.0]
    assert result[0]["x"] == 0.5
    assert result[1]["x"] == 0.4667
    assert result[2]["y"] == 0.7


def test_focus_keyframes_empty_input_uses_center_fallback():
    from renderer.pipeline import _smooth_focus_keyframes

    assert _smooth_focus_keyframes([]) == []



def test_clean_audio_builds_safe_ffmpeg_filter_chain(monkeypatch, tmp_path):
    import subprocess
    from renderer.pipeline import _clean_audio

    source = tmp_path / "raw.mp4"
    output = tmp_path / "clean.mp4"
    source.write_bytes(b"source")
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        Path(command[-1]).write_bytes(b"cleaned")
        return subprocess.CompletedProcess(command, 0, "", "")

    from pathlib import Path
    monkeypatch.setattr("renderer.pipeline.shutil.which", lambda _: "/usr/bin/ffmpeg")
    monkeypatch.setattr("renderer.pipeline.subprocess.run", fake_run)
    assert _clean_audio(source, output) == output
    assert output.read_bytes() == b"cleaned"
    command = captured["command"]
    filters = command[command.index("-af") + 1]
    assert "aresample=48000" in filters
    assert "highpass=f=80" in filters
    assert "afftdn=" in filters
    assert "loudnorm=I=-16" in filters
    assert "-c:v" in command and "copy" in command
    assert captured["kwargs"]["timeout"] == 900


def test_clean_audio_removes_partial_output_on_ffmpeg_failure(monkeypatch, tmp_path):
    import subprocess
    import pytest
    from renderer.pipeline import PipelineError, _clean_audio

    source = tmp_path / "raw.mp4"
    output = tmp_path / "clean.mp4"
    source.write_bytes(b"source")
    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 1, "", "filter error")
    monkeypatch.setattr("renderer.pipeline.shutil.which", lambda _: "/usr/bin/ffmpeg")
    monkeypatch.setattr("renderer.pipeline.subprocess.run", fake_run)
    with pytest.raises(PipelineError, match="Audio cleanup failed"):
        _clean_audio(source, output)
    assert not output.exists()
    assert not (tmp_path / "clean.part.mp4").exists()
\n\ndef test_transition_options_validate_and_default_to_none():\n    import pytest\n    from renderer.pipeline import PipelineError\n\n    # The transition contract is deliberately bounded to avoid excessive render cost.\n    with pytest.raises(PipelineError, match="Transition must be"):\n        from renderer.pipeline import _validate_transition_options\n        _validate_transition_options("slide", 0.5)\n