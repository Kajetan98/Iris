"""Uwierzytelnianie tokenem (docs/spec.md, sekcje 1 i 10).

Token pochodzi wyłącznie ze zmiennej środowiskowej IRIS_TOKEN i nie trafia do logów.
REST: nagłówek `Authorization: Bearer <token>`. WebSocket: podprotokoły `iris` i `token.<token>`,
bo przeglądarka nie może ustawić nagłówka, a adres z tokenem trafiłby do logów serwera.
"""

from __future__ import annotations

import hmac
import ipaddress
import os
import re
from collections.abc import Iterable, Mapping
from urllib.parse import urlsplit

TOKEN_ENV = "IRIS_TOKEN"
WS_PROTOCOL = "iris"
TOKEN_PREFIX = "token."
UNAUTHORIZED_CLOSE_CODE = 4003

_TOKEN = re.compile(r"[A-Za-z0-9_-]{16,128}")
# Nagłówki dodawane przez proxy i tunele (Cloudflare, nginx, Tailscale i inne). Tunel łączy się
# z hostem przez 127.0.0.1, więc bez tokenu tylko po nich widać, że żądanie przyszło z zewnątrz.
PROXY_HEADERS = ("forwarded", "x-forwarded-for", "x-real-ip", "cf-connecting-ip", "true-client-ip")


class AuthConfigError(ValueError):
    pass


def token_from_env() -> str | None:
    """Token hosta albo None (bez uwierzytelniania). AuthConfigError dla niepoprawnej wartości."""
    value = os.environ.get(TOKEN_ENV)
    if not value:
        return None
    if not _TOKEN.fullmatch(value):
        # Bez wartości w komunikacie: to może być prawie poprawny sekret.
        raise AuthConfigError(f"{TOKEN_ENV}: wymagane 16-128 znaków A-Z, a-z, 0-9, _ i -")
    return value


def _same(given: str, token: str) -> bool:
    return hmac.compare_digest(given.encode(), token.encode())


def bearer_ok(header: str | None, token: str) -> bool:
    if not header:
        return False
    scheme, _, value = header.partition(" ")
    return scheme.lower() == "bearer" and _same(value.strip(), token)


def ws_token_ok(subprotocols: Iterable[str], token: str) -> bool:
    return any(p.startswith(TOKEN_PREFIX) and _same(p[len(TOKEN_PREFIX):], token) for p in subprotocols)


def via_proxy(headers: Mapping[str, str]) -> bool:
    """True, gdy żądanie przeszło przez proxy lub tunel (spec, sekcja 1)."""
    return any(name in headers for name in PROXY_HEADERS)


def _loopback_ip(text: str) -> bool:
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)  # ::ffff:127.0.0.1
    return ip.is_loopback or (mapped is not None and mapped.is_loopback)


def is_loopback_host(host: str) -> bool:
    """Nazwa z nagłówka Host lub adres nasłuchu: localhost, 127.x.x.x albo ::1, z portem lub bez."""
    h = host.strip().lower().rstrip(".")
    if h.startswith("["):  # [::1]:8000
        end = h.find("]")
        if end < 0:
            return False
        h = h[1:end]
    elif h.count(":") == 1:  # localhost:8000; więcej dwukropków to IPv6 bez nawiasów
        h = h.rsplit(":", 1)[0]
    return h == "localhost" or _loopback_ip(h)


def local_refusal(
    peer: str | None, headers: Mapping[str, str], allowed_origins: Iterable[str] = ()
) -> tuple[int, str] | None:
    """Host bez tokenu (spec, sekcje 1 i 10): (kod HTTP, powód), gdy połączenie nie jest lokalne, inaczej None.

    `peer` to adres nadawcy TCP (None dla gniazda uniksowego). Powód nie zawiera wartości nagłówków.
    """
    if via_proxy(headers):
        return 401, "przez proxy lub tunel, a host nie ma IRIS_TOKEN"
    if peer is not None and not _loopback_ip(peer):
        return 403, "połączenie z innego komputera, a host nie ma IRIS_TOKEN"
    if not is_loopback_host(headers.get("host", "")):
        return 403, "nagłówek Host spoza tego komputera (możliwy DNS rebinding)"
    origin = headers.get("origin")
    if origin is not None and origin not in set(allowed_origins):
        hostname = urlsplit(origin).hostname if origin != "null" else None
        if hostname is None or not is_loopback_host(hostname):
            return 403, "żądanie ze strony z innego adresu, a host nie ma IRIS_TOKEN"
    return None
