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
