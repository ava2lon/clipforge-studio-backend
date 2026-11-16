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
    monkeypatch.setattr("api.create_job", lambda url, workdir: "b" * 32)
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
    monkeypatch.setattr("api.create_job", lambda url, workdir: "a" * 32)
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
