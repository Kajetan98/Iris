"""CORS dla klienta z innego adresu (docs/spec.md, sekcja 10)."""

import pytest
from pydantic import ValidationError

from host.api.main import create_app
from host.core.clock import ManualClock
from host.core.config import Config
from host.store import Store
from host.tests.helpers import local_client

SITE = "https://iris-asystent.vercel.app"
TOKEN = "cors_Token-1234567890abcd"


def client(origins, token=TOKEN):
    clock = ManualClock()
    app = create_app(Config.model_validate({"api": {"cors_origins": origins}}), clock=clock,
                     store=Store(clock=clock), run_tick_loop=False, token=token)
    return local_client(app)


def test_preflight_from_a_listed_origin_is_allowed():
    with client([SITE]) as c:
        r = c.options("/facts", headers={"Origin": SITE, "Access-Control-Request-Method": "DELETE",
                                         "Access-Control-Request-Headers": "authorization"})
        assert r.status_code == 200
        assert r.headers["access-control-allow-origin"] == SITE
        assert "DELETE" in r.headers["access-control-allow-methods"]
        assert "authorization" in r.headers["access-control-allow-headers"].lower()
        assert "access-control-allow-credentials" not in r.headers


def test_other_origins_get_no_cors_answer():
    with client([SITE]) as c:
        r = c.options("/facts", headers={"Origin": "https://zly.example", "Access-Control-Request-Method": "GET"})
        assert r.status_code == 400
        r = c.get("/health", headers={"Origin": "https://zly.example", "Authorization": f"Bearer {TOKEN}"})
        assert "access-control-allow-origin" not in r.headers


def test_without_the_setting_nothing_is_added():
    with client([]) as c:
        r = c.get("/health", headers={"Origin": SITE, "Authorization": f"Bearer {TOKEN}"})
        assert r.status_code == 200 and "access-control-allow-origin" not in r.headers
        assert c.options("/facts", headers={"Origin": SITE, "Access-Control-Request-Method": "GET"}).status_code == 405


def test_cors_does_not_replace_the_token():
    with client([SITE]) as c:
        r = c.get("/facts", headers={"Origin": SITE})
        assert r.status_code == 401 and r.headers["access-control-allow-origin"] == SITE  # przeglądarka zobaczy 401
        r = c.get("/facts", headers={"Origin": SITE, "Authorization": f"Bearer {TOKEN}"})
        assert r.status_code == 200 and r.headers["access-control-allow-origin"] == SITE
        assert c.get("/facts", headers={"Origin": SITE, "Authorization": "Bearer zly-token-1234567890"}).status_code == 401


def test_websocket_still_needs_the_token_from_a_listed_origin():
    with client([SITE]) as c:
        with c.websocket_connect("/ws?device=telefon", subprotocols=["iris"], headers={"Origin": SITE}) as ws:
            with pytest.raises(Exception) as e:
                ws.receive_text()
            assert getattr(e.value, "code", None) == 4003


@pytest.mark.parametrize("bad", ["*", "https://x.app/", "http://example.com", "https://X.app", "iris.app", "https://",
                                 "https://x.app/app"])
def test_bad_origins_stop_the_config(bad):
    with pytest.raises(ValidationError, match="cors_origins"):
        Config.model_validate({"api": {"cors_origins": [bad]}})


@pytest.mark.parametrize("ok", [SITE, "https://iris.example.ts.net:8443", "http://localhost:5173", "http://127.0.0.1:8000"])
def test_good_origins(ok):
    assert Config.model_validate({"api": {"cors_origins": [ok]}}).api.cors_origins == [ok]
