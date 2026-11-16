"""Source classification and bounded public-video ingestion."""
from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import subprocess
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from renderer.pipeline import PipelineError
from renderer.security import is_allowed_supabase_storage_url

MAX_REDIRECTS = 3
CONNECT_TIMEOUT_SECONDS = 12
READ_TIMEOUT_SECONDS = 30
MAX_DOWNLOAD_SECONDS = 15 * 60
_CHUNK_SIZE = 1024 * 1024


class SourceAccessRequired(PipelineError):
    """A platform URL needs an authorized provider integration."""


def classify_source_url(value: str) -> str:
    if is_allowed_supabase_storage_url(value):
        return "supabase_signed_url"
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower().rstrip(".")
    except ValueError:
        return "invalid"
    if parsed.scheme.lower() != "https" or not host:
        return "invalid"
    try:
        if parsed.port not in (None, 443) or parsed.username is not None or parsed.password is not None or parsed.fragment:
            return "invalid"
    except ValueError:
        return "invalid"
    if host == "youtube.com" or host.endswith(".youtube.com") or host in {"youtu.be", "www.youtu.be"}:
        return "youtube"
    if host == "tiktok.com" or host.endswith(".tiktok.com"):
        return "tiktok"
    if host == "facebook.com" or host.endswith(".facebook.com") or host == "fb.watch":
        return "facebook"
    if host == "instagram.com" or host.endswith(".instagram.com"):
        return "instagram"
    if host.endswith(".supabase.co"):
        return "invalid"
    try:
        _validate_direct_url_shape(value)
    except PipelineError:
        return "invalid"
    return "direct_http_video"


def _validate_direct_url_shape(url: str) -> tuple[object, str]:
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        port = parsed.port
    except ValueError as exc:
        raise PipelineError("Invalid source URL") from exc
    if parsed.scheme.lower() != "https" or not host:
        raise PipelineError("Direct video URLs must use HTTPS")
    if port not in (None, 443):
        raise PipelineError("Direct video URLs must use the default HTTPS port")
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise PipelineError("Credentials and URL fragments are not allowed")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise PipelineError("IP-literal source URLs are not allowed")
    return parsed, host


def _validated_https_target(url: str) -> tuple[str, int, str, list[str]]:
    parsed, host = _validate_direct_url_shape(url)
    try:
        host = host.encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError as exc:
        raise PipelineError("Invalid source hostname") from exc
    try:
        records = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise PipelineError("Source hostname could not be resolved") from exc
    addresses = list(dict.fromkeys(record[4][0] for record in records))
    if not addresses:
        raise PipelineError("Source hostname has no usable address")
    for address in addresses:
        try:
            parsed_ip = ipaddress.ip_address(address.split("%", 1)[0])
        except ValueError as exc:
            raise PipelineError("Source hostname resolved to an invalid address") from exc
        if not parsed_ip.is_global:
            raise PipelineError("Source hostname resolves to a non-public network address")
    request_target = parsed.path or "/"
    if parsed.query:
        request_target += "?" + parsed.query
    return host, 443, request_target, addresses


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection pinned to an already-vetted DNS answer."""

    def __init__(self, host: str, addresses: list[str]):
        super().__init__(host, 443, timeout=CONNECT_TIMEOUT_SECONDS, context=ssl.create_default_context())
        self._addresses = addresses

    def connect(self) -> None:
        last_error: OSError | None = None
        for address in self._addresses:
            raw = None
            try:
                raw = socket.create_connection((address, 443), timeout=CONNECT_TIMEOUT_SECONDS)
                peer = raw.getpeername()[0]
                if ipaddress.ip_address(peer.split("%", 1)[0]) != ipaddress.ip_address(address.split("%", 1)[0]):
                    raise PipelineError("Connected peer did not match the validated source address")
                self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
                self.sock.settimeout(READ_TIMEOUT_SECONDS)
                return
            except (OSError, ssl.SSLError, PipelineError) as exc:
                last_error = exc
                if raw is not None:
                    raw.close()
        raise PipelineError("Could not connect to the public video host") from last_error


def download_direct_mp4(url: str, target: Path, max_bytes: int) -> None:
    """Download an HTTPS MP4 without following unvalidated redirects or DNS."""
    current = url
    total = 0
    started = time.monotonic()
    try:
        for redirect_count in range(MAX_REDIRECTS + 1):
            host, _port, request_target, addresses = _validated_https_target(current)
            connection = _PinnedHTTPSConnection(host, addresses)
            try:
                connection.request("GET", request_target, headers={
                    "Host": host,
                    "User-Agent": "ClipForge-Studio/0.3",
                    "Accept": "video/mp4,application/octet-stream",
                    "Accept-Encoding": "identity",
                    "Connection": "close",
                })
                response = connection.getresponse()
                if response.status in (301, 302, 303, 307, 308):
                    location = response.getheader("Location")
                    response.read(64 * 1024)
                    if not location or redirect_count >= MAX_REDIRECTS:
                        raise PipelineError("Source exceeded the redirect limit")
                    current = urljoin(current, location)
                    continue
                if response.status != 200:
                    raise PipelineError(f"Video host returned HTTP {response.status}")
                content_type = (response.getheader("Content-Type") or "").split(";", 1)[0].strip().lower()
                if content_type not in {"video/mp4", "application/mp4", "application/octet-stream"}:
                    raise PipelineError("Direct source must serve an MP4 video")
                encoding = (response.getheader("Content-Encoding") or "identity").lower()
                if encoding not in {"", "identity"}:
                    raise PipelineError("Compressed HTTP responses are not supported for video ingestion")
                length = response.getheader("Content-Length")
                if length:
                    try:
                        if int(length) <= 0 or int(length) > max_bytes:
                            raise PipelineError("Source exceeds configured maximum source size")
                    except ValueError as exc:
                        raise PipelineError("Invalid source Content-Length") from exc
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("wb") as output:
                    first = b""
                    while True:
                        chunk = response.read(_CHUNK_SIZE)
                        if not chunk:
                            break
                        if time.monotonic() - started > MAX_DOWNLOAD_SECONDS:
                            raise PipelineError("Source download exceeded the configured time limit")
                        if len(first) < 12:
                            first += chunk[:12 - len(first)]
                            if len(first) >= 12 and first[4:8] != b"ftyp":
                                raise PipelineError("Downloaded file is not an MP4 stream")
                        total += len(chunk)
                        if total > max_bytes:
                            raise PipelineError("Source exceeds configured maximum source size")
                        output.write(chunk)
                if total < 12 or first[4:8] != b"ftyp":
                    raise PipelineError("Downloaded file is not a valid ISO Base Media/MP4 stream")
                return
            finally:
                connection.close()
        raise PipelineError("Source exceeded the redirect limit")
    except Exception:
        target.unlink(missing_ok=True)
        raise


def _social_extraction_error(source_type: str, diagnostic: str) -> PipelineError:
    """Translate extractor diagnostics into safe, actionable user-facing errors."""
    message = diagnostic.lower()
    access_markers = (
        "sign in to confirm", "not a bot", "login required", "authentication required",
        "private video", "members-only", "join this channel",
    )
    unavailable_markers = (
        "video unavailable", "video has been removed", "this video is private",
        "does not exist", "account has been terminated", "post not found",
    )
    transient_markers = (
        "http error 429", "http error 500", "http error 502", "http error 503",
        "http error 504", "timed out", "timeout", "temporary failure",
        "connection reset", "connection refused", "network is unreachable",
    )
    if any(marker in message for marker in access_markers):
        return SourceAccessRequired(
            f"This {source_type.title()} link was accepted, but the platform requires "
            "sign-in or additional verification before it will provide the video. "
            "ClipForge does not bypass that restriction."
        )
    if any(marker in message for marker in unavailable_markers):
        return PipelineError(
            f"The {source_type.title()} platform reports that this video is private, "
            "removed, or unavailable."
        )
    if any(marker in message for marker in transient_markers):
        return PipelineError(
            f"The {source_type.title()} platform or network returned a temporary "
            "retrieval error. Retry the job later."
        )
    return PipelineError(
        f"Could not retrieve this public {source_type.title()} video. "
        "It may be unavailable or restricted by the platform."
    )


def download_social_video(url: str, target: Path, max_bytes: int) -> None:
    """Best-effort unauthenticated public-video extraction using yt-dlp.

    No browser cookies, account credentials, or user-supplied headers are read.
    Platform availability remains dependent on each site's public access rules.
    """
    source_type = classify_source_url(url)
    if source_type not in {"youtube", "tiktok", "facebook", "instagram"}:
        raise PipelineError("Unsupported social video URL")
    output_base = target.with_suffix("")
    output_pattern = str(output_base) + ".%(ext)s"
    command = [
        "yt-dlp",
        "--ignore-config",
        "--no-playlist",
        "--no-warnings",
        "--no-progress",
        "--quiet",
        "--no-cache-dir",
        "--socket-timeout", "30",
        "--retries", "2",
        "--fragment-retries", "2",
        "--max-filesize", str(max_bytes),
        "--format", "best[ext=mp4]/best",
        "--remux-video", "mp4",
        "--output", output_pattern,
        url,
    ]
    try:
        result = subprocess.run(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, timeout=MAX_DOWNLOAD_SECONDS,
            check=False,
        )
    except FileNotFoundError as exc:
        raise PipelineError("Social video extraction is unavailable: yt-dlp is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        for path in target.parent.glob(output_base.name + ".*"):
            path.unlink(missing_ok=True)
        raise PipelineError("Social video extraction exceeded the 15-minute time limit") from exc

    candidates = list(target.parent.glob(output_base.name + ".*"))
    mp4_files = [path for path in candidates if path.suffix.lower() == ".mp4" and path.is_file()]
    if result.returncode != 0 or not mp4_files:
        diagnostic = "\n".join(
            part for part in (getattr(result, "stderr", ""), getattr(result, "stdout", ""))
            if isinstance(part, str)
        )[-12000:]
        for path in candidates:
            path.unlink(missing_ok=True)
        raise _social_extraction_error(source_type, diagnostic)
    downloaded = mp4_files[0]
    try:
        size = downloaded.stat().st_size
        if size <= 0 or size > max_bytes:
            raise PipelineError("Downloaded social video exceeds the configured source size limit")
        with downloaded.open("rb") as stream:
            header = stream.read(12)
        if len(header) < 12 or header[4:8] != b"ftyp":
            raise PipelineError("Social video extractor did not produce a valid MP4 file")
        downloaded.replace(target)
    finally:
        for path in candidates:
            if path != target:
                path.unlink(missing_ok=True)


def ensure_supported_ingestion_source(url: str) -> str:
    source_type = classify_source_url(url)
    if source_type == "invalid":
        raise PipelineError("Source must be a valid HTTPS video URL")
    return source_type
