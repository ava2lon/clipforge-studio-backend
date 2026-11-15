"""Strict URL validation for signed Supabase Storage source URLs."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def is_allowed_supabase_storage_url(value: str) -> bool:
    """Accept HTTPS Supabase project storage signed URLs on the default TLS port."""
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname or ""
        port = parsed.port
    except ValueError:
        return False

    if parsed.scheme.lower() != "https":
        return False
    if not hostname.endswith(".supabase.co"):
        return False
    project = hostname[: -len(".supabase.co")]
    if not _HOST_LABEL.fullmatch(project):
        return False
    if port not in (None, 443):
        return False
    if parsed.username is not None or parsed.password is not None:
        return False
    if parsed.fragment:
        return False
    if not parsed.path.startswith("/storage/v1/object/sign/"):
        return False
    if len(parsed.path) <= len("/storage/v1/object/sign/"):
        return False
    return True
