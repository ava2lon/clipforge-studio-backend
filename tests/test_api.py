from fastapi.testclient import TestClient

from api import app

client = TestClient(app)


def test_root():
    response = client.get("/")
    assert response.status_code == 200
    assert response.json()["name"] == "ClipForge Studio Backend"


def test_health_aliases():
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/healthz").json()["status"] == "ok"


def test_capabilities_shape():
    response = client.get("/capabilities")
    assert response.status_code == 200
    body = response.json()
    assert "ffmpeg" in body
    assert "python_modules" in body


def test_clip_submission_requires_api_key(monkeypatch):
    monkeypatch.setenv("AVA2LON_STUDIO_API_KEY", "test-secret")
    response = client.post(
        "/clips",
        json={"media": "https://project.supabase.co/storage/v1/object/sign/videos/source.mp4?token=test"},
    )
    assert response.status_code == 401


def test_clip_submission_accepts_public_social_source_without_oauth(monkeypatch):
    monkeypatch.setenv("AVA2LON_STUDIO_API_KEY", "test-secret")
    monkeypatch.setattr("api.create_job", lambda url, workdir, *args: "b" * 32)
    response = client.post(
        "/clips",
        headers={"X-API-Key": "test-secret"},
        json={"media": "https://youtube.com/shorts/_az0sT5IYaE?si=RGARmBuPzvmIVrz3"},
    )
    assert response.status_code == 202
    assert response.json()["source_type"] == "youtube"
    assert response.json()["job_id"] == "b" * 32



def test_clip_submission_rejects_unsafe_supabase_urls(monkeypatch):
    monkeypatch.setenv("AVA2LON_STUDIO_API_KEY", "test-secret")
    headers = {"X-API-Key": "test-secret"}
    unsafe_urls = [
        "http://project.supabase.co/storage/v1/object/sign/videos/source.mp4?token=test",
        "https://user:pass@project.supabase.co/storage/v1/object/sign/videos/source.mp4?token=test",
        "https://project.supabase.co:8443/storage/v1/object/sign/videos/source.mp4?token=test",
        "https://project.supabase.co/storage/v1/object/sign/videos/source.mp4#fragment",
        "https://project.supabase.co/rest/v1/users?select=*",
    ]
    for media in unsafe_urls:
        response = client.post("/clips", headers=headers, json={"media": media})
        assert response.status_code in (400, 422), (media, response.text)



def test_status_rejects_malformed_job_id(monkeypatch):
    monkeypatch.setenv("AVA2LON_STUDIO_API_KEY", "test-secret")
    response = client.get("/status/../../etc/passwd", headers={"X-API-Key": "test-secret"})
    assert response.status_code in (400, 404)


def test_status_rejects_non_hex_job_id(monkeypatch):
    monkeypatch.setenv("AVA2LON_STUDIO_API_KEY", "test-secret")
    response = client.get("/status/" + "g" * 32, headers={"X-API-Key": "test-secret"})
    assert response.status_code == 400


def test_download_rejects_non_hex_job_id(monkeypatch):
    monkeypatch.setenv("AVA2LON_STUDIO_API_KEY", "test-secret")
    response = client.get("/download/" + "g" * 32, headers={"X-API-Key": "test-secret"})
    assert response.status_code == 400


def test_clip_submission_accepts_direct_https_mp4_url(monkeypatch):
    monkeypatch.setenv("AVA2LON_STUDIO_API_KEY", "test-secret")
    monkeypatch.setattr("api.create_job", lambda url, workdir, *args: "a" * 32)
    response = client.post(
        "/clips",
        headers={"X-API-Key": "test-secret"},
        json={"media": "https://cdn.example.net/video.mp4"},
    )
    assert response.status_code == 202
    assert response.json()["job_id"] == "a" * 32
    assert response.json()["source_type"] == "direct_http_video"


def test_clip_submission_rejects_direct_url_with_credentials(monkeypatch):
    monkeypatch.setenv("AVA2LON_STUDIO_API_KEY", "test-secret")
    response = client.post(
        "/clips",
        headers={"X-API-Key": "test-secret"},
        json={"media": "https://user:pass@cdn.example.net/video.mp4"},
    )
    assert response.status_code == 400



def test_clip_submission_accepts_caption_presets(monkeypatch):
    monkeypatch.setenv("AVA2LON_STUDIO_API_KEY", "test-secret")
    captured = {}
    def fake_create_job(url, workdir, *args):
        captured["args"] = args
        return "c" * 32
    monkeypatch.setattr("api.create_job", fake_create_job)
    response = client.post(
        "/clips",
        headers={"X-API-Key": "test-secret"},
        json={
            "media": "https://cdn.example.net/video.mp4",
            "caption_style": "minimal",
            "caption_position": "top",
            "quota_managed": True,
        },
    )
    assert response.status_code == 202
    assert captured["args"][-8:] == ("minimal", "top", True, "none", 0.5, None, True, [])


def test_clip_submission_rejects_unknown_caption_preset(monkeypatch):
    monkeypatch.setenv("AVA2LON_STUDIO_API_KEY", "test-secret")
    response = client.post(
        "/clips",
        headers={"X-API-Key": "test-secret"},
        json={
            "media": "https://cdn.example.net/video.mp4",
            "caption_style": "neon-glitch",
        },
    )
    assert response.status_code == 422



def test_clip_submission_can_disable_audio_cleanup(monkeypatch):
    monkeypatch.setenv("AVA2LON_STUDIO_API_KEY", "test-secret")
    captured = {}
    def fake_create_job(url, workdir, *args):
        captured["args"] = args
        return "d" * 32
    monkeypatch.setattr("api.create_job", fake_create_job)
    response = client.post(
        "/clips",
        headers={"X-API-Key": "test-secret"},
        json={"media": "https://cdn.example.net/video.mp4", "audio_cleanup": False, "quota_managed": True},
    )
    assert response.status_code == 202
    assert captured["args"][4] is False
    assert captured["args"][-5:] == ("none", 0.5, None, True, [])


def test_clip_submission_accepts_signed_project_broll_assets(monkeypatch):
    monkeypatch.setenv("AVA2LON_STUDIO_API_KEY", "test-secret")
    captured = {}

    def fake_create_job(url, workdir, *args):
        captured["args"] = args
        return "e" * 32

    monkeypatch.setattr("api.create_job", fake_create_job)
    response = client.post(
        "/clips",
        headers={"X-API-Key": "test-secret"},
        json={
            "media": "https://cdn.example.net/video.mp4",
            "broll_assets": [{
                "url": "https://project.supabase.co/storage/v1/object/sign/source-ingestion-buffers/user/project/broll/a.mp4?token=test",
                "keywords": ["office", "team"],
            }],
        },
    )
    assert response.status_code == 202, response.text
    assert captured["args"][-1] == [{
        "url": "https://project.supabase.co/storage/v1/object/sign/source-ingestion-buffers/user/project/broll/a.mp4?token=test",
        "keywords": ["office", "team"],
    }]


def test_clip_submission_rejects_non_supabase_broll_url(monkeypatch):
    monkeypatch.setenv("AVA2LON_STUDIO_API_KEY", "test-secret")
    response = client.post(
        "/clips",
        headers={"X-API-Key": "test-secret"},
        json={
            "media": "https://cdn.example.net/video.mp4",
            "broll_assets": [{
                "url": "https://attacker.example/broll.mp4",
                "keywords": ["office"],
            }],
        },
    )
    assert response.status_code == 400


def test_manifest_endpoint_requires_completed_job_and_returns_manifest(monkeypatch, tmp_path):
    import json
    import api

    monkeypatch.setenv("AVA2LON_STUDIO_API_KEY", "test-secret")
    monkeypatch.setattr(api, "WORKDIR", tmp_path)
    job_id = "f" * 32
    job_dir = tmp_path / job_id
    job_dir.mkdir()
    expected = {"job_id": job_id, "clips": [{"name": "clip-01.mp4"}]}
    (job_dir / "clipforge_manifest.json").write_text(json.dumps(expected), encoding="utf-8")
    monkeypatch.setattr(api, "get_job", lambda value: {"state": "COMPLETED"} if value == job_id else None)

    response = client.get(f"/manifest/{job_id}", headers={"X-API-Key": "test-secret"})
    assert response.status_code == 200
    assert response.json() == expected


def test_manifest_endpoint_rejects_incomplete_job(monkeypatch):
    import api

    monkeypatch.setenv("AVA2LON_STUDIO_API_KEY", "test-secret")
    monkeypatch.setattr(api, "get_job", lambda value: {"state": "RUNNING"})
    response = client.get(f"/manifest/{'a' * 32}", headers={"X-API-Key": "test-secret"})
    assert response.status_code == 409


def test_clip_asset_endpoint_serves_only_valid_completed_clip(monkeypatch, tmp_path):
    import api

    monkeypatch.setenv("AVA2LON_STUDIO_API_KEY", "test-secret")
    monkeypatch.setattr(api, "WORKDIR", tmp_path)
    job_id = "c" * 32
    clip_dir = tmp_path / job_id / "clips"
    clip_dir.mkdir(parents=True)
    clip_bytes = b"clip-test-bytes"
    (clip_dir / "clip-01.mp4").write_bytes(clip_bytes)
    monkeypatch.setattr(api, "get_job", lambda value: {"state": "COMPLETED"})

    response = client.get(f"/clip/{job_id}/clip-01.mp4", headers={"X-API-Key": "test-secret"})
    assert response.status_code == 200
    assert response.content == clip_bytes
    assert response.headers["content-type"].startswith("video/mp4")


def test_clip_asset_endpoint_rejects_invalid_filename_and_incomplete_jobs(monkeypatch):
    import api

    monkeypatch.setenv("AVA2LON_STUDIO_API_KEY", "test-secret")
    monkeypatch.setattr(api, "get_job", lambda value: {"state": "RUNNING"})
    response = client.get(f"/clip/{'b' * 32}/manifest.json", headers={"X-API-Key": "test-secret"})
    assert response.status_code == 400
    response = client.get(f"/clip/{'b' * 32}/clip-01.mp4", headers={"X-API-Key": "test-secret"})
    assert response.status_code == 409


def test_clip_edit_endpoint_saves_validated_settings(monkeypatch, tmp_path):
    import api

    monkeypatch.setenv("AVA2LON_STUDIO_API_KEY", "test-secret")
    monkeypatch.setattr(api, "WORKDIR", tmp_path)
    monkeypatch.setattr(api, "create_clip_edit", lambda job_id, filename, workdir, settings: {
        "edit_id": "d" * 32, "job_id": job_id, "clip_name": filename,
        "state": "SAVED", "settings": settings,
    })
    response = client.post(
        f"/edit/{'a' * 32}/clip-01.mp4",
        headers={"X-API-Key": "test-secret"},
        json={
            "trim_start": 1,
            "trim_end": 8,
            "captions": [{"start": 1, "end": 1.5, "text": "Edited"}],
            "aspect_ratio": "9:16",
            "caption_style": "karaoke",
            "caption_position": "bottom",
            "transition": "fade",
            "transition_duration": 0.5,
            "brand_template": "creator",
        },
    )
    assert response.status_code == 201
    assert response.json()["state"] == "SAVED"
    assert response.json()["settings"]["captions"][0]["text"] == "Edited"


def test_clip_edit_endpoint_rejects_invalid_caption_template(monkeypatch):
    monkeypatch.setenv("AVA2LON_STUDIO_API_KEY", "test-secret")
    response = client.post(
        f"/edit/{'a' * 32}/clip-01.mp4",
        headers={"X-API-Key": "test-secret"},
        json={
            "trim_start": 0, "trim_end": 5,
            "captions": [], "brand_template": "unknown",
        },
    )
    assert response.status_code == 422


def test_clip_edit_endpoint_requires_api_key(monkeypatch):
    monkeypatch.setenv("AVA2LON_STUDIO_API_KEY", "test-secret")
    response = client.post(
        f"/edit/{'a' * 32}/clip-01.mp4",
        json={"trim_start": 0, "trim_end": 5, "captions": []},
    )
    assert response.status_code == 401


def test_metrics_endpoint_requires_api_key(monkeypatch):
    monkeypatch.setenv("AVA2LON_STUDIO_API_KEY", "test-secret")
    response = client.get("/metrics")
    assert response.status_code == 401


def test_metrics_endpoint_returns_aggregate_runtime_data(monkeypatch):
    monkeypatch.setenv("AVA2LON_STUDIO_API_KEY", "test-secret")
    monkeypatch.setattr("api.runtime_metrics", lambda: {
        "jobs_by_state": {"COMPLETED": 4, "FAILED": 1},
        "queue": {
            "active_jobs": 1,
            "active_edit_renders": 2,
            "max_active_jobs": 3,
            "worker_count": 1,
        },
        "storage": {
            "total_bytes": 1000,
            "used_bytes": 400,
            "free_bytes": 600,
            "retention_seconds": 604800,
            "cleanup_interval_seconds": 3600,
        },
        "timestamp": 1.0,
    })
    response = client.get("/metrics", headers={"X-API-Key": "test-secret"})
    assert response.status_code == 200
    body = response.json()
    assert body["jobs_by_state"]["COMPLETED"] == 4
    assert body["queue"]["active_edit_renders"] == 2
    assert body["storage"]["free_bytes"] == 600


def test_retry_endpoint_requires_api_key(monkeypatch):
    monkeypatch.setenv("AVA2LON_STUDIO_API_KEY", "test-secret")
    response = client.post(f"/retry/{'a' * 32}")
    assert response.status_code == 401


def test_retry_endpoint_returns_accepted_attempt(monkeypatch):
    monkeypatch.setenv("AVA2LON_STUDIO_API_KEY", "test-secret")
    monkeypatch.setattr("api.retry_job", lambda job_id: {
        "job_id": job_id, "state": "PENDING", "attempt_count": 2,
    })
    response = client.post(
        f"/retry/{'a' * 32}",
        headers={"X-API-Key": "test-secret"},
    )
    assert response.status_code == 202
    assert response.json()["attempt_count"] == 2


def test_retry_endpoint_rejects_invalid_job_id(monkeypatch):
    monkeypatch.setenv("AVA2LON_STUDIO_API_KEY", "test-secret")
    response = client.post(
        "/retry/not-a-job",
        headers={"X-API-Key": "test-secret"},
    )
    assert response.status_code == 400
