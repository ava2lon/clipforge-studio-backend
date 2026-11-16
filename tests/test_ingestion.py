import pytest

from renderer import ingestion
from renderer.ingestion import (
    SourceAccessRequired,
    _validated_https_target,
    classify_source_url,
    ensure_supported_ingestion_source,
)


@pytest.mark.parametrize(("url", "expected"), [
    ("https://www.youtube.com/watch?v=abc123", "youtube"),
    ("https://youtu.be/abc123", "youtube"),
    ("https://www.tiktok.com/@creator/video/123", "tiktok"),
    ("https://www.facebook.com/reel/123", "facebook"),
    ("https://fb.watch/abc123", "facebook"),
    ("https://www.instagram.com/reel/abc123/", "instagram"),
    ("https://cdn.example.net/video.mp4", "direct_http_video"),
    ("https://project.supabase.co/storage/v1/object/sign/bucket/a.mp4?token=x", "supabase_signed_url"),
])
def test_classifies_known_source_types(url, expected):
    assert classify_source_url(url) == expected


@pytest.mark.parametrize("url", [
    "http://cdn.example.net/video.mp4",
    "https://user:pass@cdn.example.net/video.mp4",
    "https://cdn.example.net:8443/video.mp4",
    "https://cdn.example.net/video.mp4#fragment",
])
def test_rejects_invalid_direct_urls(url):
    with pytest.raises(Exception):
        _validated_https_target(url)


def test_rejects_private_dns_resolution(monkeypatch):
    monkeypatch.setattr(
        ingestion.socket, "getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("127.0.0.1", 443))],
    )
    with pytest.raises(Exception, match="non-public"):
        _validated_https_target("https://attacker.example/video.mp4")


def test_accepts_public_dns_resolution(monkeypatch):
    monkeypatch.setattr(
        ingestion.socket, "getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("8.8.8.8", 443))],
    )
    host, port, path, addresses = _validated_https_target("https://cdn.example/video.mp4?token=x")
    assert (host, port, path, addresses) == ("cdn.example", 443, "/video.mp4?token=x", ["8.8.8.8"])


@pytest.mark.parametrize("url", [
    "https://www.youtube.com/watch?v=abc",
    "https://www.tiktok.com/@creator/video/123",
    "https://www.facebook.com/reel/123",
    "https://www.instagram.com/reel/123",
])
def test_social_links_require_authorized_provider_access(url):
    with pytest.raises(SourceAccessRequired):
        ensure_supported_ingestion_source(url)


def test_direct_video_is_supported():
    assert ensure_supported_ingestion_source("https://cdn.example/video.mp4") == "direct_http_video"
