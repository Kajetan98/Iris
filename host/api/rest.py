"""Endpointy REST (docs/spec.md, sekcja 10) i WebSocket /ws."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import Response

from host.api.auth import UNAUTHORIZED_CLOSE_CODE, WS_PROTOCOL, bearer_ok, local_refusal, ws_token_ok
from host.api.service import TOO_MANY_DEVICES_CLOSE_CODE
from host.api.service import (
    AckTimeout,
    ChatDisabled,
    ChatError,
    HostService,
    NotConnected,
    UnknownDevice,
)
from host.core import memory as mem
from host.core.protocol import valid_device_id
from host.store.models import Snapshot, StoredEvent

log = logging.getLogger(__name__)


def _peer(conn: Request | WebSocket) -> str | None:
    return conn.client.host if conn.client is not None else None


def require_token(request: Request) -> None:
    """401 bez poprawnego `Authorization: Bearer`, gdy host ma token. Bez tokenu tylko żądania
    z tego komputera: 401 przez proxy, 403 z innego adresu, obcego Host lub obcego Origin (spec, sekcja 10)."""
    token = getattr(request.app.state, "token", None)
    if token is None:
        refusal = local_refusal(_peer(request), request.headers, request.app.state.allowed_origins)
        if refusal is not None:
            log.warning("żądanie odrzucone: %s", refusal[1])
            raise HTTPException(refusal[0], refusal[1])
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


# --- pamięć (spec, sekcje 9 i 10) ---


class LearnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=500)
    domain: Literal["dom", "czas", "ty", "rozmowa"] | None = None


class RecallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=500)
    k: int = Field(8, ge=1, le=50)


@router.get("/memory")
async def get_memory(service: HostService = Depends(get_service)) -> dict[str, Any]:
    return service.memory.snapshot()


@router.post("/memory/learn")
async def post_memory_learn(req: LearnRequest, service: HostService = Depends(get_service)) -> dict[str, Any]:
    if not service.memory.enabled:
        raise HTTPException(409, "pamięć wyłączona w konfiguracji (memory.enabled)")
    memory = service.memory
    ext = mem.keep(mem.heuristic(req.text, memory.cfg), memory.cfg, allow_sensitive=True)
    plan = memory.learn(ext, how="told", domain=req.domain)
    return {"fresh": plan.fresh, "reinforced": plan.reinforced, "domain": plan.domain}


@router.post("/memory/recall")
async def post_memory_recall(req: RecallRequest, service: HostService = Depends(get_service)) -> dict[str, Any]:
    hits = service.memory.recall(req.query, by="api", k=req.k)
    return {"results": [{"key": h.key, "label": h.label, "score": h.score, "via": h.via} for h in hits]}


@router.delete("/memory/concepts/{key}", status_code=204)
async def delete_concept(key: str, service: HostService = Depends(get_service)) -> Response:
    if not service.memory.forget([key], why="user"):
        raise HTTPException(404, "nie ma takiego pojęcia")
    return Response(status_code=204)


@router.delete("/memory", status_code=204)
async def delete_memory(confirm: bool = Query(False), service: HostService = Depends(get_service)) -> Response:
    if not confirm:
        raise HTTPException(422, "usunięcie całej pamięci wymaga confirm=true")
    service.memory.clear()
    return Response(status_code=204)


# --- WebSocket ---


async def _ws_refused(websocket: WebSocket) -> tuple[str | None, bool]:
    """(wybrany podprotokół, czy odrzucono). Odmowa: przyjęcie i zamknięcie kodem 4003 (spec, sekcja 1)."""
    offered = websocket.scope.get("subprotocols") or []
    subprotocol = WS_PROTOCOL if WS_PROTOCOL in offered else None
    token = getattr(websocket.app.state, "token", None)
    if token is None:
        refusal = local_refusal(_peer(websocket), websocket.headers, websocket.app.state.allowed_origins)
        refused = refusal[1] if refusal is not None else None
    elif not ws_token_ok(offered, token):
        refused = "brak lub zły token"
    else:
        refused = None
    if refused:
        log.warning("połączenie odrzucone: %s", refused)
        await websocket.accept(subprotocol=subprotocol)
        await websocket.close(code=UNAUTHORIZED_CLOSE_CODE, reason="unauthorized")
    return subprotocol, bool(refused)


@ws_router.websocket("/memory/stream")
async def memory_stream(websocket: WebSocket) -> None:
    """Graf pamięci na żywo: stan po połączeniu, po każdej zmianie i co stream_keepalive_s (spec, sekcja 10)."""
    subprotocol, refused = await _ws_refused(websocket)
    if refused:
        return
    service: HostService = websocket.app.state.service
    cfg = service.memory.cfg
    clients: set[WebSocket] = websocket.app.state.memory_clients
    await websocket.accept(subprotocol=subprotocol)
    if len(clients) >= cfg.stream_max_clients:
        await websocket.close(code=TOO_MANY_DEVICES_CLOSE_CODE, reason="too many clients")
        return
    clients.add(websocket)
    changed = asyncio.Event()
    loop = asyncio.get_running_loop()
    unsubscribe = service.memory.subscribe(lambda: loop.call_soon_threadsafe(changed.set))
    reader = asyncio.create_task(_drain_incoming(websocket))
    try:
        while True:
            await websocket.send_json(service.memory.snapshot())
            waiter = asyncio.create_task(changed.wait())
            done, _ = await asyncio.wait({waiter, reader}, timeout=cfg.stream_keepalive_s,
                                         return_when=asyncio.FIRST_COMPLETED)
            waiter.cancel()
            if reader in done:
                break  # klient się rozłączył
            changed.clear()
            await asyncio.sleep(cfg.stream_min_interval_s)  # kilka zmian naraz daje jedną ramkę
            changed.clear()
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        unsubscribe()
        reader.cancel()
        clients.discard(websocket)


async def _drain_incoming(websocket: WebSocket) -> None:
    """Ramki od klienta strumienia są pomijane; kończy się przy rozłączeniu."""
    try:
        while True:
            await websocket.receive_text()
    except (WebSocketDisconnect, RuntimeError, KeyError):
        return


@ws_router.websocket("/ws")
async def body_socket(websocket: WebSocket) -> None:
    subprotocol, refused = await _ws_refused(websocket)
    if refused:
        return
    # device czytany ręcznie: niepoprawny ma dać kod zamknięcia 4002, nie odrzucony handshake
    await websocket.app.state.service.serve_connection(
        websocket, websocket.query_params.get("device"), subprotocol=subprotocol
    )
