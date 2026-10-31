"""Pomocnicze budowanie zdarzeń w testach."""

import json

from fastapi.testclient import TestClient

from host.core.protocol import DecodedEvent, decode_frame

_counter = 0


def make_event(type_: str, body: dict, ts: int = 0) -> DecodedEvent:
    global _counter
    _counter += 1
    ev = decode_frame(
        json.dumps({"v": 1, "t": "event", "id": str(_counter), "ts": ts, "type": type_, "body": body})
    )
    assert isinstance(ev, DecodedEvent)
    return ev


def heartbeat(batt_pct: float = 71, rssi: int = -58, **extra) -> DecodedEvent:
    return make_event("body.heartbeat", {"batt_pct": batt_pct, "link_rssi": rssi, **extra})


class _LocalClient(TestClient):
    # TestClient łączy /ws zawsze z hostem "testserver"; tu adres WebSocket wynika z base_url.
    def websocket_connect(self, url, subprotocols=None, **kwargs):
        if url.startswith("/"):
            url = str(self.base_url).replace("http", "ws", 1).rstrip("/") + url
        return super().websocket_connect(url, subprotocols, **kwargs)


def local_client(app, **kwargs) -> TestClient:
    """Klient testowy jak przeglądarka lub urządzenie na tym samym komputerze: adres 127.0.0.1 i Host
    wskazujący pętlę zwrotną. Bez tokenu host odrzuca inne połączenia (spec, sekcje 1 i 10)."""
    kwargs.setdefault("base_url", "http://127.0.0.1:8000")
    kwargs.setdefault("client", ("127.0.0.1", 50000))
    return _LocalClient(app, **kwargs)
