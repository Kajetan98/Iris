"""Logika hosta między transportem a rdzeniem: ramki -> HostCore -> Store, akcje do ciała.

Połączonych może być kilka urządzeń, aktywne jest jedno (docs/spec.md, sekcja 1).
Metody synchroniczne i korutyny wołane są wyłącznie z pętli zdarzeń, więc HostCore
nie wymaga blokad. Store ma własną blokadę.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import sqlite3
import time
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any, Protocol

from host.core.clock import Clock
from host.core.config import Config
from host.core.core import HostCore
from host.core.events import HostEvent
from host.core.protocol import (
    DEFAULT_DEVICE,
    AckBody,
    AudioFrame,
    AudioUtteranceEnd,
    AudioUtteranceStart,
    DecodedAck,
    FrameRejected,
    SystemBoot,
    decode_audio_frame,
    decode_frame,
    encode_action,
    valid_device_id,
)
from host.store import Store, StoreError

if TYPE_CHECKING:  # host.brain ładuje się dopiero, gdy LLM lub głos jest włączony
    from host.brain.assistant import Assistant
    from host.brain.audio import RawUtterance
    from host.brain.speech import STTEngine, TTSEngine
    from host.brain.voice import VoicePipeline

log = logging.getLogger(__name__)

REPLACED_CLOSE_CODE = 4000
TOO_MANY_DEVICES_CLOSE_CODE = 4001
BAD_DEVICE_CLOSE_CODE = 4002

# Urządzenie, z którego przyszła wypowiedź bieżącej tury głosowej.
_turn_device: contextvars.ContextVar[str | None] = contextvars.ContextVar("turn_device", default=None)


class NotConnected(Exception):
    pass


class DeviceSwitched(Exception):
    """Aktywne urządzenie zmieniło się w trakcie tury głosowej; odpowiedź przepada."""


class UnknownDevice(Exception):
    pass


class AckTimeout(Exception):
    def __init__(self, action_id: str) -> None:
        super().__init__(f"brak ack dla akcji {action_id}")
        self.action_id = action_id


class ChatDisabled(Exception):
    """LLM jest wyłączony w konfiguracji."""


class ChatError(Exception):
    """Nieudana tura rozmowy. kind: unavailable | refused | failed."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


class WebSocketLike(Protocol):
    async def accept(self, subprotocol: str | None = None) -> None: ...
    async def receive(self) -> dict[str, Any]: ...
    async def send_text(self, data: str) -> None: ...
    async def send_bytes(self, data: bytes) -> None: ...
    async def close(self, code: int = 1000, reason: str | None = None) -> None: ...


class HostService:
    def __init__(
        self,
        config: Config,
        clock: Clock,
        store: Store,
        on_audio: Callable[[AudioFrame], None] | None = None,
        assistant: "Assistant | None" = None,
        stt: "STTEngine | None" = None,
        tts: "TTSEngine | None" = None,
    ) -> None:
        self.config = config
        self.clock = clock
        self.store = store
        self.on_audio = on_audio or (lambda frame: None)
        self.assistant = assistant
        self.stt, self.tts = stt, tts
        self.voice: "VoicePipeline | None" = None
        self.voice_status = "disabled"  # disabled | ready | error
        self._voice_tasks: set[asyncio.Task[None]] = set()
        self._chat_lock = asyncio.Lock()
        self.core: HostCore | None = None
        self.rejected_frames = 0
        self._conns: dict[str, WebSocketLike] = {}
        self._boot_ids: dict[str, str] = {}
        self._background: set[asyncio.Task[None]] = set()
        self._pending: dict[str, tuple[str, asyncio.Future[AckBody]]] = {}
        self._action_seq = 0
        self._started = clock.monotonic()
        self._last_touch = self._started
        self._last_snapshot = self._started

    # --- cykl życia ---

    def start(self) -> None:
        continuity = self.store.begin_session()
        active = self._store(self.store.active_device)
        self.core = HostCore(self.config, self.clock, continuity, active_device=active)
        self._started = self._last_touch = self.clock.monotonic()
        self._persist(self.core.start())
        self._snapshot()
        self._init_voice()

    def stop(self) -> None:
        core = self._require_core()
        self._persist(core.stop())
        self._snapshot()
        self._store(self.store.end_session)

    async def run_tick_loop(self) -> None:
        while True:
            await asyncio.sleep(self.config.api.tick_interval_s)
            try:
                self.tick()
            except Exception:
                log.exception("błąd w tick")

    def tick(self) -> None:
        core = self._require_core()
        self._persist(core.tick())
        now = self.clock.monotonic()
        if now - self._last_touch >= self.config.api.touch_every_s:
            self._last_touch = now
            self._store(self.store.touch)
            self._store(self.store.touch_devices, list(self._conns))
        if now - self._last_snapshot >= self.config.api.snapshot_every_s:
            self._snapshot()

    # --- połączenia z urządzeniami ---

    @property
    def connected(self) -> bool:
        """Czy aktywne urządzenie ma otwarte połączenie."""
        core = self.core
        return core is not None and core.active_device in self._conns

    @property
    def boot_id(self) -> str | None:
        core = self.core
        return None if core is None or core.active_device is None else self._boot_ids.get(core.active_device)

    async def serve_connection(
        self, ws: WebSocketLike, device: str | None = None, *, subprotocol: str | None = None
    ) -> None:
        await ws.accept(subprotocol=subprotocol)
        device = DEFAULT_DEVICE if device is None else device
        if not valid_device_id(device):
            log.warning("odrzucone połączenie: niepoprawny device")
            await ws.close(code=BAD_DEVICE_CLOSE_CODE, reason="bad device")
            return
        previous = self._conns.get(device)
        if previous is None and len(self._conns) >= self.config.devices.max_connected:
            log.warning("odrzucone połączenie %s: za dużo urządzeń", device)
            await ws.close(code=TOO_MANY_DEVICES_CLOSE_CODE, reason="too many devices")
            return
        self._conns[device] = ws
        if previous is not None:
            log.info("nowe połączenie %s zastępuje poprzednie", device)
            try:
                await previous.close(code=REPLACED_CLOSE_CODE, reason="replaced")
            except Exception:
                log.debug("stare połączenie było już zamknięte", exc_info=True)
        self._register(device)
        if device != self._require_core().active_device:
            await self._send_active(device, False)
        try:
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                try:
                    if msg.get("text") is not None:
                        self.handle_text(msg["text"], device)
                    elif msg.get("bytes") is not None:
                        self.handle_binary(msg["bytes"], device)
                except Exception:
                    log.exception("błąd przy obsłudze ramki")
        finally:
            if self._conns.get(device) is ws:
                del self._conns[device]
                self._store(self.store.see_device, device)

    def _register(self, device: str) -> None:
        """Urządzenie w bazie i w rdzeniu; pierwsze w ogóle staje się aktywne."""
        self._store(self.store.see_device, device)
        self._persist(self._require_core().connect(device))

    def handle_text(self, raw: str, device: str = DEFAULT_DEVICE) -> None:
        core = self._require_core()
        at = self.clock.wall()
        try:
            decoded = decode_frame(raw)
        except FrameRejected as e:
            self.rejected_frames += 1
            log.warning("ramka odrzucona: %s", e.reason)
            return
        if isinstance(decoded, DecodedAck):
            self._on_ack(decoded, device)
            return
        if isinstance(decoded.body, SystemBoot):
            self._boot_ids[device] = decoded.body.boot_id
        if device not in core.known_devices():
            self._register(device)  # device.switch przed pierwszym zdarzeniem
        host_events = core.handle_event(decoded, device)
        if decoded.type != "body.heartbeat" or self.config.api.store_heartbeats:
            self._store(
                self.store.append_body_event, decoded, at=at, boot_id=self._boot_ids.get(device), device=device
            )
        self._persist(host_events)
        if device == core.active_device:
            self._route_voice(decoded.body, device)

    def handle_binary(self, raw: bytes, device: str = DEFAULT_DEVICE) -> None:
        core = self._require_core()
        try:
            frame = decode_audio_frame(raw)
        except FrameRejected as e:
            self.rejected_frames += 1
            log.warning("ramka audio odrzucona: %s", e.reason)
            return
        if device not in core.known_devices():
            self._register(device)
        if device != core.active_device:
            return  # audio z urządzenia w spoczynku jest pomijane (spec, sekcja 1)
        if self.voice is not None:
            self.voice.on_audio(frame)
        self.on_audio(frame)

    # --- przełączanie urządzeń ---

    def devices(self) -> dict[str, Any]:
        """Odpowiedź dla GET /devices."""
        core = self._require_core()
        rows = {r.id: r for r in (self._store(self.store.devices) or [])}
        out = []
        for device in sorted(set(rows) | set(core.known_devices())):
            state = core.device_state(device) or {}
            row = rows.get(device)
            out.append(
                {
                    "id": device,
                    "active": device == core.active_device,
                    "connected": device in self._conns,
                    "link": state.get("link"),
                    "batt_pct": state.get("batt_pct"),
                    "rssi": state.get("rssi"),
                    "caps": None if (caps := core.device_caps(device)) is None else caps.model_dump(),
                    "first_seen_at": row.first_seen_at if row else None,
                    "last_seen_at": row.last_seen_at if row else None,
                }
            )
        return {"active": core.active_device, "devices": out}

    async def activate_device(self, device: str) -> None:
        """Ręczne przełączenie (POST /devices/{id}/activate). ValueError, UnknownDevice."""
        core = self._require_core()
        if not valid_device_id(device):
            raise ValueError("niepoprawny identyfikator urządzenia")
        if device not in core.known_devices() and not self._store(self.store.device_known, device):
            raise UnknownDevice(device)
        self._persist(core.activate(device, "rest"))

    async def _notify_switch(self, previous: str | None, device: str) -> None:
        if previous is not None and previous in self._conns:
            await self._send_active(previous, False)
        if device in self._conns:
            await self._send_active(device, True)

    async def _send_active(self, device: str, active: bool) -> None:
        try:
            await self.send_action("device.active", {"active": active}, wait_ack=False, device=device)
        except NotConnected:
            log.debug("device.active do %s: brak połączenia", device)

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        try:
            task = asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            coro.close()  # bez pętli zdarzeń (testy synchroniczne) nie ma komu wysłać
            return
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    # --- akcje ---

    async def send_action(
        self, type_: str, body: dict[str, Any], *, wait_ack: bool = True, device: str | None = None
    ) -> tuple[str, AckBody | None]:
        """Wysyła akcję do urządzenia (domyślnie aktywnego).

        ValueError: niepoprawna akcja, NotConnected, AckTimeout."""
        target = self._require_core().active_device if device is None else device
        ws = None if target is None else self._conns.get(target)
        if ws is None or target is None:
            raise NotConnected()
        action_id = f"h{self._action_seq + 1}"
        ts_ms = int((self.clock.monotonic() - self._started) * 1000)
        frame = encode_action(type_, body, id=action_id, ts_ms=ts_ms)
        self._action_seq += 1

        future: asyncio.Future[AckBody] | None = None
        if wait_ack:
            future = asyncio.get_running_loop().create_future()
            self._pending[action_id] = (target, future)
        try:
            try:
                await ws.send_text(frame)
            except Exception:
                raise NotConnected() from None
            self._persist(
                [
                    HostEvent(
                        type="action.sent",
                        at=self.clock.wall(),
                        data={"id": action_id, "type": type_, "body": json.loads(frame)["body"]},
                        device=target,
                    )
                ]
            )
            if future is None:
                return action_id, None
            try:
                ack = await asyncio.wait_for(future, self.config.api.ack_timeout_s)
            except asyncio.TimeoutError:
                raise AckTimeout(action_id) from None
            return action_id, ack
        finally:
            self._pending.pop(action_id, None)

    def _on_ack(self, decoded: DecodedAck, device: str = DEFAULT_DEVICE) -> None:
        ack = decoded.body
        self._persist(
            [
                HostEvent(
                    type="action.ack",
                    at=self.clock.wall(),
                    data={"ref": ack.ref, "ok": ack.ok, "err": ack.err},
                    device=device,
                )
            ]
        )
        pending = self._pending.get(ack.ref)
        if pending is None:
            log.warning("ack dla nieznanej lub zakończonej akcji: %s", ack.ref)
        elif pending[0] != device:
            log.warning("ack dla %s od innego urządzenia (%s)", ack.ref, device)
        elif not pending[1].done():
            pending[1].set_result(ack)

    # --- głos ---

    def _init_voice(self) -> None:
        if self.stt is None or self.tts is None:
            return
        if self.assistant is None:
            log.error("głos wymaga włączonego LLM (brain.llm), głos wyłączony")
            self.voice_status = "error"
            return
        from host.brain.voice import VoicePipeline

        self.voice = VoicePipeline(
            stt=self.stt,
            tts=self.tts,
            respond=self._voice_respond,
            send_frame=self._send_audio,
            announce=self._announce_speech,
            audio=self.config.brain.audio,
            min_text_chars=self.config.brain.stt.min_text_chars,
            limits=self._turn_limits,
        )
        self.voice_status = "ready"

    def _route_voice(self, body: Any, device: str) -> None:
        voice = self.voice
        if voice is None:
            return
        if isinstance(body, AudioUtteranceStart):
            voice.on_utterance_start(body.utt_id)
        elif isinstance(body, AudioUtteranceEnd):
            # Wypowiedź trzeba pobrać od razu: następny start nie może jej porzucić.
            raw = voice.take_utterance(body.utt_id, body.dur_ms)
            if raw is None:
                return
            caps = self._require_core().device_caps(device)
            if caps is not None and "speaker" not in caps.has:
                log.info("urządzenie %s nie ma głośnika, wypowiedź bez odpowiedzi", device)
                return
            if len(self._voice_tasks) >= self.config.brain.audio.max_pending_turns:
                log.warning("zbyt wiele tur głosowych w toku, wypowiedź pominięta")
                return
            context = contextvars.copy_context()
            context.run(_turn_device.set, device)  # odpowiedź wraca na to urządzenie
            try:
                task = asyncio.get_running_loop().create_task(self._voice_turn(raw), context=context)
            except RuntimeError:
                log.error("tura głosowa wymaga działającej pętli zdarzeń")
                return
            self._voice_tasks.add(task)
            task.add_done_callback(self._voice_tasks.discard)

    async def _voice_turn(self, raw: "RawUtterance") -> None:
        from host.brain.audio import AudioError
        from host.brain.speech import SpeechError

        assert self.voice is not None
        try:
            await self.voice.process(raw)
        except asyncio.CancelledError:
            raise
        except DeviceSwitched:
            log.info("tura głosowa: urządzenie przełączone w trakcie, odpowiedź pominięta")
        except (ChatError, ChatDisabled, SpeechError, AudioError, NotConnected) as e:
            # Komunikaty tych błędów nie zawierają treści rozmowy.
            log.warning("tura głosowa nieudana (%s): %s", type(e).__name__, str(e)[:200])
            await self._play_error_clip()
        except Exception:
            log.exception("tura głosowa: nieoczekiwany błąd")
            await self._play_error_clip()

    def _turn_limits(self) -> tuple[int, int] | None:
        """Bufor odtwarzania urządzenia tury z device.caps (None: możliwości HERMES)."""
        device = _turn_device.get() or self._require_core().active_device
        caps = self._require_core().device_caps(device)
        if caps is None:
            return None
        audio = self.config.brain.audio
        return (caps.playback_bytes or audio.body_buffer_bytes, caps.playback_packets or audio.body_max_packets)

    def _turn_target(self) -> str | None:
        """Urządzenie tury głosowej. DeviceSwitched, gdy przestało być aktywne."""
        active = self._require_core().active_device
        device = _turn_device.get()
        if device is not None and device != active:
            raise DeviceSwitched(device)
        return active

    async def _play_error_clip(self) -> None:
        clip = self.config.brain.audio.error_clip
        try:
            device = self._turn_target()
        except DeviceSwitched:
            return
        if not clip or device not in self._conns:
            return
        caps = self._require_core().device_caps(device)
        if caps is not None and clip not in caps.clips:
            return  # urządzenie nie ma tego klipu (spec, sekcja 1)
        try:
            await self.send_action("speak.clip", {"clip": clip}, wait_ack=False, device=device)
        except (NotConnected, ValueError):
            log.debug("nie udało się odtworzyć klipu błędu", exc_info=True)

    async def _voice_respond(self, text: str) -> str:
        return (await self.chat(text))["text"]

    async def _send_audio(self, frame: bytes) -> None:
        device = self._turn_target()
        ws = None if device is None else self._conns.get(device)
        if ws is None:
            raise NotConnected()
        try:
            await ws.send_bytes(frame)
        except Exception:
            raise NotConnected() from None

    async def _announce_speech(self, audio_id: int) -> None:
        device = self._turn_target()
        try:
            _, ack = await self.send_action("speak", {"audio_id": audio_id}, wait_ack=True, device=device)
        except (NotConnected, AckTimeout) as e:
            log.warning("speak %d: %s", audio_id, type(e).__name__)
            return
        if ack is not None and not ack.ok:
            log.warning("ciało odrzuciło speak %d: %s", audio_id, ack.err)

    async def drain_voice(self) -> None:
        """Przerywa trwające tury głosowe i wysyłki w tle (przy zamykaniu hosta)."""
        tasks = list(self._voice_tasks) + list(self._background)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    # --- rozmowa ---

    async def chat(self, text: str) -> dict[str, Any]:
        """Jedna tura rozmowy z LLM. Rzuca ChatDisabled, ValueError (pusta wypowiedź) lub ChatError.

        Tura obejmuje pobranie wycinka stanu, zapytanie i ewentualne cofnięcie wycinka, więc
        trwa pod blokadą. Treść rozmowy nie jest zapisywana ani logowana."""
        assistant = self.assistant
        if assistant is None:
            raise ChatDisabled()
        text = text.strip()
        if not text:
            raise ValueError("pusta wypowiedź")
        from host.brain.llm import LLMError, LLMRefusal, LLMUnavailable

        core = self._require_core()
        async with self._chat_lock:
            self._persist(core.note_interaction())
            state = core.prompt_slice()
            facts = self._store(self.store.facts) or []
            started = time.monotonic()
            try:
                answer = await assistant.ask(text, state=state, facts=facts, now=self.clock.wall())
            except BaseException as e:
                core.rollback_prompt_slice()  # także przy anulowaniu żądania
                if isinstance(e, LLMRefusal):
                    raise ChatError("refused", str(e)) from None
                if isinstance(e, LLMUnavailable):
                    raise ChatError("unavailable", str(e)) from None
                if isinstance(e, LLMError):
                    raise ChatError("failed", str(e)) from None
                raise
        log.info(
            "chat: model=%s tokens=%s/%s truncated=%s %.0f ms",
            answer.model, answer.usage.input_tokens, answer.usage.output_tokens,
            answer.truncated, (time.monotonic() - started) * 1000,
        )
        return {
            "text": answer.text,
            "truncated": answer.truncated,
            "model": answer.model,
            "usage": {
                "input_tokens": answer.usage.input_tokens,
                "output_tokens": answer.usage.output_tokens,
            },
        }

    # --- stan ---

    def refresh_known_facts(self) -> None:
        self._require_core().set_known_facts(self.store.count_facts())

    def health(self) -> dict[str, Any]:
        body = self._require_core().body.snapshot()
        try:
            self.store.ping()
            store_status = "ok"
        except (sqlite3.Error, StoreError):
            store_status = "error"
        return {
            "status": "ok" if body["link"] == "ok" else "degraded",
            "uptime_s": int(self.clock.monotonic() - self._started),
            "session_id": self.store.session_id,
            "device": self._require_core().active_device,
            "devices_connected": len(self._conns),
            "link": {
                "state": body["link"],
                "connected": self.connected,
                "rssi": body["rssi"],
                "last_seen_ms": body["last_seen_ms"],
            },
            "store": store_status,
            "voice": self.voice_status,
        }

    # --- zapis ---

    def _persist(self, events: list[HostEvent]) -> None:
        changed = False
        for ev in events:
            self._store(self.store.append_host_event, ev)
            changed |= ev.type in ("drive.enter", "drive.exit", "device.switch")
            if ev.type == "device.switch":
                self._on_switch(ev.data["from"], ev.data["to"])
        if changed:
            self._snapshot()

    def _on_switch(self, previous: str | None, device: str) -> None:
        try:
            self._store(self.store.set_active_device, device)
        except KeyError:
            log.error("aktywne urządzenie %s nie jest zapisane w bazie", device)
        if previous is not None:
            self._spawn(self._notify_switch(previous, device))  # device.active w tle (spec, sekcja 1)

    def _snapshot(self) -> None:
        snap = self._require_core().self_snapshot()
        self._last_snapshot = self.clock.monotonic()
        self._store(self.store.save_snapshot, snap["self"], snap["body"])

    def _store(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Błąd zapisu jest logowany, host pracuje dalej na stanie w pamięci."""
        try:
            return fn(*args, **kwargs)
        except (sqlite3.Error, StoreError):
            log.exception("błąd zapisu do bazy")
            return None

    def _require_core(self) -> HostCore:
        if self.core is None:
            raise RuntimeError("serwis nie został uruchomiony")
        return self.core
