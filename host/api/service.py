"""Logika hosta między transportem a rdzeniem: ramki -> HostCore -> Store, akcje do ciała.

Metody synchroniczne i korutyny wołane są wyłącznie z pętli zdarzeń, więc HostCore
nie wymaga blokad. Store ma własną blokadę.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from collections.abc import Callable
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
)
from host.store import Store, StoreError

if TYPE_CHECKING:  # host.brain ładuje się dopiero, gdy LLM lub głos jest włączony
    from host.brain.assistant import Assistant
    from host.brain.audio import RawUtterance
    from host.brain.speech import STTEngine, TTSEngine
    from host.brain.voice import VoicePipeline

log = logging.getLogger(__name__)

REPLACED_CLOSE_CODE = 4000


class NotConnected(Exception):
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
    async def accept(self) -> None: ...
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
        self.boot_id: str | None = None
        self.rejected_frames = 0
        self._ws: WebSocketLike | None = None
        self._pending: dict[str, asyncio.Future[AckBody]] = {}
        self._action_seq = 0
        self._started = clock.monotonic()
        self._last_touch = self._started
        self._last_snapshot = self._started

    # --- cykl życia ---

    def start(self) -> None:
        continuity = self.store.begin_session()
        self.core = HostCore(self.config, self.clock, continuity)
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
        if now - self._last_snapshot >= self.config.api.snapshot_every_s:
            self._snapshot()

    # --- połączenie z ciałem ---

    @property
    def connected(self) -> bool:
        return self._ws is not None

    async def serve_connection(self, ws: WebSocketLike) -> None:
        await ws.accept()
        previous, self._ws = self._ws, ws
        if previous is not None:
            log.info("nowe połączenie zastępuje poprzednie")
            try:
                await previous.close(code=REPLACED_CLOSE_CODE, reason="replaced")
            except Exception:
                log.debug("stare połączenie było już zamknięte", exc_info=True)
        try:
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                try:
                    if msg.get("text") is not None:
                        self.handle_text(msg["text"])
                    elif msg.get("bytes") is not None:
                        self.handle_binary(msg["bytes"])
                except Exception:
                    log.exception("błąd przy obsłudze ramki")
        finally:
            if self._ws is ws:
                self._ws = None

    def handle_text(self, raw: str) -> None:
        core = self._require_core()
        at = self.clock.wall()
        try:
            decoded = decode_frame(raw)
        except FrameRejected as e:
            self.rejected_frames += 1
            log.warning("ramka odrzucona: %s", e.reason)
            return
        if isinstance(decoded, DecodedAck):
            self._on_ack(decoded)
            return
        if isinstance(decoded.body, SystemBoot):
            self.boot_id = decoded.body.boot_id
        if DEFAULT_DEVICE not in core.known_devices():
            self._persist(core.connect(DEFAULT_DEVICE))  # device.switch przed pierwszym zdarzeniem
        host_events = core.handle_event(decoded)
        if decoded.type != "body.heartbeat" or self.config.api.store_heartbeats:
            self._store(self.store.append_body_event, decoded, at=at, boot_id=self.boot_id)
        self._persist(host_events)
        self._route_voice(decoded.body)

    def handle_binary(self, raw: bytes) -> None:
        try:
            frame = decode_audio_frame(raw)
        except FrameRejected as e:
            self.rejected_frames += 1
            log.warning("ramka audio odrzucona: %s", e.reason)
            return
        if self.voice is not None:
            self.voice.on_audio(frame)
        self.on_audio(frame)

    # --- akcje ---

    async def send_action(
        self, type_: str, body: dict[str, Any], *, wait_ack: bool = True
    ) -> tuple[str, AckBody | None]:
        """Wysyła akcję do ciała. ValueError: niepoprawna akcja, NotConnected, AckTimeout."""
        ws = self._ws
        if ws is None:
            raise NotConnected()
        action_id = f"h{self._action_seq + 1}"
        ts_ms = int((self.clock.monotonic() - self._started) * 1000)
        frame = encode_action(type_, body, id=action_id, ts_ms=ts_ms)
        self._action_seq += 1

        future: asyncio.Future[AckBody] | None = None
        if wait_ack:
            future = asyncio.get_running_loop().create_future()
            self._pending[action_id] = future
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

    def _on_ack(self, decoded: DecodedAck) -> None:
        ack = decoded.body
        self._persist(
            [
                HostEvent(
                    type="action.ack",
                    at=self.clock.wall(),
                    data={"ref": ack.ref, "ok": ack.ok, "err": ack.err},
                )
            ]
        )
        future = self._pending.get(ack.ref)
        if future is None:
            log.warning("ack dla nieznanej lub zakończonej akcji: %s", ack.ref)
        elif not future.done():
            future.set_result(ack)

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
        )
        self.voice_status = "ready"

    def _route_voice(self, body: Any) -> None:
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
            if len(self._voice_tasks) >= self.config.brain.audio.max_pending_turns:
                log.warning("zbyt wiele tur głosowych w toku, wypowiedź pominięta")
                return
            try:
                task = asyncio.get_running_loop().create_task(self._voice_turn(raw))
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
        except (ChatError, ChatDisabled, SpeechError, AudioError, NotConnected) as e:
            # Komunikaty tych błędów nie zawierają treści rozmowy.
            log.warning("tura głosowa nieudana (%s): %s", type(e).__name__, str(e)[:200])
            await self._play_error_clip()
        except Exception:
            log.exception("tura głosowa: nieoczekiwany błąd")
            await self._play_error_clip()

    async def _play_error_clip(self) -> None:
        clip = self.config.brain.audio.error_clip
        if not clip or not self.connected:
            return
        try:
            await self.send_action("speak.clip", {"clip": clip}, wait_ack=False)
        except (NotConnected, ValueError):
            log.debug("nie udało się odtworzyć klipu błędu", exc_info=True)

    async def _voice_respond(self, text: str) -> str:
        return (await self.chat(text))["text"]

    async def _send_audio(self, frame: bytes) -> None:
        ws = self._ws
        if ws is None:
            raise NotConnected()
        try:
            await ws.send_bytes(frame)
        except Exception:
            raise NotConnected() from None

    async def _announce_speech(self, audio_id: int) -> None:
        try:
            _, ack = await self.send_action("speak", {"audio_id": audio_id}, wait_ack=True)
        except (NotConnected, AckTimeout) as e:
            log.warning("speak %d: %s", audio_id, type(e).__name__)
            return
        if ack is not None and not ack.ok:
            log.warning("ciało odrzuciło speak %d: %s", audio_id, ack.err)

    async def drain_voice(self) -> None:
        """Przerywa trwające tury głosowe (przy zamykaniu hosta)."""
        tasks = list(self._voice_tasks)
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
        changed_drive = False
        for ev in events:
            self._store(self.store.append_host_event, ev)
            changed_drive |= ev.type in ("drive.enter", "drive.exit")
        if changed_drive:
            self._snapshot()

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
