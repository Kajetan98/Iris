"""Uwierzytelnianie tokenem (docs/spec.md, sekcje 1 i 10).

Token pochodzi wyłącznie ze zmiennej środowiskowej IRIS_TOKEN i nie trafia do logów.
REST: nagłówek `Authorization: Bearer <token>`. WebSocket: podprotokoły `iris` i `token.<token>`,
bo przeglądarka nie może ustawić nagłówka, a adres z tokenem trafiłby do logów serwera.
"""

from __future__ import annotations

import hmac
import os
import re
from collections.abc import Iterable

TOKEN_ENV = "IRIS_TOKEN"
WS_PROTOCOL = "iris"
TOKEN_PREFIX = "token."
UNAUTHORIZED_CLOSE_CODE = 4003

_TOKEN = re.compile(r"[A-Za-z0-9_-]{16,128}")


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
