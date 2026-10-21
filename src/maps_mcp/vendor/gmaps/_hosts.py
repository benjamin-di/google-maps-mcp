"""Keep every request, redirects included, on Google's hosts.

The clients follow redirects. Without a check, a redirect (or a crafted base URL) could send
the request and its cookies anywhere; with it, a hop to any other host is refused before it
is sent.
"""

from __future__ import annotations

import httpx

from .exceptions import HostNotAllowedError

ALLOWED_HOSTS = frozenset({"www.google.com", "google.com", "consent.google.com", "maps.google.com"})


async def refuse_other_hosts(request: httpx.Request) -> None:
    """httpx request hook: raise HostNotAllowedError for a host outside ALLOWED_HOSTS."""
    host = request.url.host
    if host not in ALLOWED_HOSTS:
        raise HostNotAllowedError(f"refused a request to {host!r}: not a Google Maps host")
