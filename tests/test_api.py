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
        },
    )
    assert response.status_code == 202
    assert captured["args"][-6:] == ("minimal", "top", True, "none", 0.5, [])


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
        json={"media": "https://cdn.example.net/video.mp4", "audio_cleanup": False},
    )
    assert response.status_code == 202
    assert captured["args"][4] is False
    assert captured["args"][-3:] == ("none", 0.5, [])


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
