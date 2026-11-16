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


class _FakeResponse:
    status = 200

    def __init__(self, chunks, headers=None):
        self._chunks = iter(chunks)
        self._headers = headers or {"Content-Type": "video/mp4"}

    def getheader(self, name):
        return self._headers.get(name)

    def read(self, _size=-1):
        return next(self._chunks, b"")


class _FakeConnection:
    def __init__(self, _host, _addresses, response):
        self.response = response

    def request(self, *_args, **_kwargs):
        pass

    def getresponse(self):
        return self.response

    def close(self):
        pass


def test_direct_mp4_download_streams_and_checks_signature(monkeypatch, tmp_path):
    payload = b"\x00\x00\x00\x18ftypmp42" + b"video-data"
    response = _FakeResponse([payload[:5], payload[5:]])
    monkeypatch.setattr(
        ingestion, "_validated_https_target",
        lambda _url: ("cdn.example", 443, "/clip.mp4", ["8.8.8.8"]),
    )
    monkeypatch.setattr(
        ingestion, "_PinnedHTTPSConnection",
        lambda host, addresses: _FakeConnection(host, addresses, response),
    )
    target = tmp_path / "source.part"
    ingestion.download_direct_mp4("https://cdn.example/clip.mp4", target, 1024)
    assert target.read_bytes() == payload


def test_direct_mp4_download_rejects_non_mp4_and_cleans_partial(monkeypatch, tmp_path):
    response = _FakeResponse([b"not-a-video-file"])
    monkeypatch.setattr(
        ingestion, "_validated_https_target",
        lambda _url: ("cdn.example", 443, "/clip.mp4", ["8.8.8.8"]),
    )
    monkeypatch.setattr(
        ingestion, "_PinnedHTTPSConnection",
        lambda host, addresses: _FakeConnection(host, addresses, response),
    )
    target = tmp_path / "source.part"
    with pytest.raises(Exception, match="not an MP4"):
        ingestion.download_direct_mp4("https://cdn.example/clip.mp4", target, 1024)
    assert not target.exists()


def test_direct_mp4_download_enforces_streaming_byte_limit(monkeypatch, tmp_path):
    payload = b"\x00\x00\x00\x18ftypmp42" + b"x" * 40
    response = _FakeResponse([payload])
    monkeypatch.setattr(
        ingestion, "_validated_https_target",
        lambda _url: ("cdn.example", 443, "/clip.mp4", ["8.8.8.8"]),
    )
    monkeypatch.setattr(
        ingestion, "_PinnedHTTPSConnection",
        lambda host, addresses: _FakeConnection(host, addresses, response),
    )
    target = tmp_path / "source.part"
    with pytest.raises(Exception, match="maximum source size"):
        ingestion.download_direct_mp4("https://cdn.example/clip.mp4", target, 20)
    assert not target.exists()
