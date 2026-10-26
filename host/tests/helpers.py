"""Pomocnicze budowanie zdarzeń w testach."""

import json

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
