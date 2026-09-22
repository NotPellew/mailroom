"""Local-request safety helpers for the review server.

The review server is bound to loopback, but a browser can still be tricked
into sending requests from another page (CSRF) or the app can be reached via a
rebinding hostname. These helpers keep state-changing requests same-origin and
loopback-only. They are deliberately dependency-free.
"""

import hmac
import secrets
from typing import Optional
from urllib.parse import urlparse

from Mailroom.config import is_loopback_host


def host_from_header(host_header: Optional[str]) -> str:
    """Extract a bare host from a Host header value (port/brackets removed)."""
    if not host_header:
        return ""
    value = host_header.strip()
    if value.startswith("["):
        # IPv6 literal, e.g. [::1]:5000
        end = value.find("]")
        return value[1:end].lower() if end != -1 else value.lower()
    if ":" in value:
        value = value.rsplit(":", 1)[0]
    return value.lower()


def is_allowed_host_header(host_header: Optional[str]) -> bool:
    """True when the request Host header points at a loopback host."""
    return is_loopback_host(host_from_header(host_header))


def is_allowed_origin_or_referer(value: Optional[str]) -> bool:
    """True when an Origin/Referer URL is an http(s) loopback URL."""
    if not value:
        return False
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    return is_loopback_host(parsed.hostname)


def generate_csrf_token() -> str:
    """Generate a random CSRF token for the running app instance."""
    return secrets.token_urlsafe(32)


def csrf_token_matches(expected: Optional[str], provided: Optional[str]) -> bool:
    """Constant-time comparison of the expected and provided CSRF tokens."""
    if not expected or not provided:
        return False
    return hmac.compare_digest(str(expected), str(provided))
