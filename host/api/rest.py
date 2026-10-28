"""Endpointy REST (docs/spec.md, sekcja 10) i WebSocket /ws."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import Response

from host.api.auth import UNAUTHORIZED_CLOSE_CODE, WS_PROTOCOL, bearer_ok, via_proxy, ws_token_ok
from host.api.service import (
    AckTimeout,
    ChatDisabled,
    ChatError,
    HostService,
    NotConnected,
    UnknownDevice,
)
from host.core.protocol import valid_device_id
from host.store.models import Fact, Snapshot, StoredEvent

log = logging.getLogger(__name__)


def require_token(request: Request) -> None:
    """401 bez poprawnego `Authorization: Bearer`, gdy host ma token, i dla żądań przez proxy,
    gdy go nie ma (spec, sekcja 10)."""
    token = getattr(request.app.state, "token", None)
    if token is None:
        if via_proxy(request.headers):
            raise HTTPException(401, "przez proxy lub tunel host wymaga IRIS_TOKEN")
        return
    if not bearer_ok(request.headers.get("authorization"), token):
        raise HTTPException(401, "wymagany token", headers={"WWW-Authenticate": "Bearer"})


router = APIRouter(dependencies=[Depends(require_token)])
ws_router = APIRouter()  # /ws sprawdza token sam: zamyka połączenie kodem 4003

STATE_EVENT_TYPES = [
    "host.start",
    "host.stop",
    "link.lost",
    "link.restored",
    "drive.enter",
    "drive.exit",
    "system.boot",
    "body.fault",
    "body.fault.clear",
    "device.switch",
]


def get_service(request: Request) -> HostService:
    return request.app.state.service


def parse_since(since: datetime | None = Query(None)) -> datetime | None:
    if since is not None and since.tzinfo is None:
        raise HTTPException(422, "since wymaga strefy czasowej (Z lub przesunięcia)")
    return since


Limit = Query(1000, ge=1, le=1000)


def _event(e: StoredEvent) -> dict[str, Any]:
    return {
        "id": e.id,
        "session_id": e.session_id,
        "at": e.at,
        "source": e.source,
        "type": e.type,
        "ts_ms": e.ts_ms,
        "boot_id": e.boot_id,
        "device": e.device,
        "data": e.data,
    }


def _snapshot(s: Snapshot) -> dict[str, Any]:
    return {"id": s.id, "session_id": s.session_id, "at": s.at, "self": s.self_state, "body": s.body}


def _fact(f: Fact) -> dict[str, Any]:
    return {
        "id": f.id,
        "text": f.text,
        "source": f.source,
        "confidence": f.confidence,
        "reinforced": f.reinforced,
        "asked_at": f.asked_at,
        "created_at": f.created_at,
        "updated_at": f.updated_at,
    }


# --- stan ---


@router.get("/self")
async def get_self(service: HostService = Depends(get_service)) -> dict[str, Any]:
    return service.core.self_snapshot()


@router.get("/self/history")
async def get_self_history(
    since: datetime | None = Depends(parse_since),
    limit: int = Limit,
    service: HostService = Depends(get_service),
) -> dict[str, Any]:
    if since is None:
        since = service.core.continuity.last_restart
    return {
        "since": since,
        "snapshots": [_snapshot(s) for s in service.store.snapshots(since=since, limit=limit)],
        "events": [
            _event(e)
            for e in service.store.events(types=STATE_EVENT_TYPES, since=since, limit=limit)
        ],
    }


@router.get("/events")
async def get_events(
    type: list[str] | None = Query(None),
    since: datetime | None = Depends(parse_since),
    after_id: int = Query(0, ge=0),
    limit: int = Limit,
    device: str | None = Query(None),
    service: HostService = Depends(get_service),
) -> dict[str, Any]:
    events = service.store.events(types=type, since=since, after_id=after_id, limit=limit, device=device)
    return {"events": [_event(e) for e in events]}


@router.get("/health")
async def get_health(service: HostService = Depends(get_service)) -> dict[str, Any]:
    return service.health()


# --- akcje ---


class ActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str
    body: dict[str, Any] = Field(default_factory=dict)
    wait_ack: bool = True
    device: str | None = None  # domyślnie aktywne urządzenie


@router.post("/actions")
async def post_action(
    req: ActionRequest, service: HostService = Depends(get_service)
) -> dict[str, Any]:
    device = req.device if req.device is not None else service.core.active_device
    try:
        action_id, ack = await service.send_action(req.type, req.body, wait_ack=req.wait_ack, device=device)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    except NotConnected:
        raise HTTPException(409, "urządzenie nie jest połączone") from None
    except AckTimeout as e:
        raise HTTPException(504, f"akcja {e.action_id} wysłana, brak ack w czasie") from None
    return {"id": action_id, "device": device, "ack": ack.model_dump() if ack else None}


# --- urządzenia ---


@router.get("/devices")
async def get_devices(service: HostService = Depends(get_service)) -> dict[str, Any]:
    return service.devices()


@router.post("/devices/{device_id}/activate")
async def activate_device(device_id: str, service: HostService = Depends(get_service)) -> dict[str, Any]:
    if not valid_device_id(device_id):
        raise HTTPException(422, "niepoprawny identyfikator urządzenia")
    try:
        await service.activate_device(device_id)
    except UnknownDevice:
        raise HTTPException(404, "urządzenie nigdy się nie połączyło") from None
    return {"active": device_id}


# --- rozmowa ---


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=4000)


CHAT_STATUS = {"unavailable": 503, "failed": 502, "refused": 422}


@router.post("/chat")
async def post_chat(req: ChatRequest, service: HostService = Depends(get_service)) -> dict[str, Any]:
    try:
        return await service.chat(req.text)
    except ChatDisabled:
        raise HTTPException(501, "LLM wyłączony w konfiguracji (brain.llm.engine)") from None
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    except ChatError as e:
        raise HTTPException(CHAT_STATUS[e.kind], f"silnik LLM: {e}") from None


# --- fakty ---


class FactPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str | None = None
    source: Literal["stated", "inferred"] | None = None
    confidence: float | None = Field(None, ge=0, le=1)


@router.get("/facts")
async def list_facts(
    source: Literal["stated", "inferred"] | None = None,
    service: HostService = Depends(get_service),
) -> dict[str, Any]:
    return {"facts": [_fact(f) for f in service.store.facts(source=source)]}


@router.patch("/facts/{fact_id}")
async def patch_fact(
    fact_id: int, patch: FactPatch, service: HostService = Depends(get_service)
) -> dict[str, Any]:
    changes = patch.model_dump(exclude_unset=True)
    if not changes or any(v is None for v in changes.values()):
        raise HTTPException(422, "podaj co najmniej jedno pole: text, source, confidence")
    try:
        fact = service.store.update_fact(fact_id, **changes)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    if fact is None:
        raise HTTPException(404, "nie ma takiego faktu")
    return _fact(fact)


@router.delete("/facts/{fact_id}", status_code=204)
async def delete_fact(fact_id: int, service: HostService = Depends(get_service)) -> Response:
    if not service.store.delete_fact(fact_id):
        raise HTTPException(404, "nie ma takiego faktu")
    service.refresh_known_facts()
    return Response(status_code=204)


# --- WebSocket ---


@ws_router.websocket("/ws")
async def body_socket(websocket: WebSocket) -> None:
    offered = websocket.scope.get("subprotocols") or []
    subprotocol = WS_PROTOCOL if WS_PROTOCOL in offered else None
    token = getattr(websocket.app.state, "token", None)
    if token is None and via_proxy(websocket.headers):
        refused = "przez proxy lub tunel, a host nie ma IRIS_TOKEN"
    elif token is not None and not ws_token_ok(offered, token):
        refused = "brak lub zły token"
    else:
        refused = None
    if refused:
        log.warning("połączenie odrzucone: %s", refused)
        await websocket.accept(subprotocol=subprotocol)
        await websocket.close(code=UNAUTHORIZED_CLOSE_CODE, reason="unauthorized")
        return
    # device czytany ręcznie: niepoprawny ma dać kod zamknięcia 4002, nie odrzucony handshake
    await websocket.app.state.service.serve_connection(
        websocket, websocket.query_params.get("device"), subprotocol=subprotocol
    )
