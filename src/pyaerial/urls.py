"""URL helpers shared by config validation, alerters and stores.

Dependency-free (stdlib only) so any module can import it without pulling in
the config loader: ``alerters.webhook`` importing ``config.loader`` used to
form a cycle with the loader's lazy ``alerters`` import.
"""

from __future__ import annotations

from urllib.parse import urlparse, urlsplit, urlunsplit

_LOCAL_HTTP_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def redact_url(url: str) -> str:
    """Return ``scheme://host`` for logs; webhook paths and queries are secrets."""
    try:
        parsed = urlparse(str(url).strip())
        host = parsed.hostname or ""
    except ValueError:
        return "<invalid url>"
    scheme = parsed.scheme or "?"
    if not host:
        return f"{scheme}://<no host>"
    if ":" in host:
        host = f"[{host}]"
    return f"{scheme}://{host}"


def redact_uri(uri: str) -> str:
    """Return ``uri`` with any password replaced by ``***`` (safe to log)."""
    try:
        parts = urlsplit(uri)
    except ValueError:
        return "<unparseable redis uri>"
    if parts.password is None:
        return uri
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    user = parts.username or ""
    return urlunsplit(parts._replace(netloc=f"{user}:***@{host}"))


def webhook_url_allowed(url: str) -> bool:
    """HTTPS anywhere, HTTP only for localhost / 127.0.0.1 / ::1."""
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    if parsed.scheme == "https":
        return True
    if parsed.scheme == "http":
        return host in _LOCAL_HTTP_HOSTS
    return False
