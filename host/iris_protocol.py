"""Protokół ramek HERMES <-> host, wersja 1 (docs/protocol.md)."""

from __future__ import annotations

import binascii
import struct
from dataclasses import dataclass
from enum import IntEnum

SOF = b"\xA5\x5A"
VERSION = 0x01
MAX_PAYLOAD = 1024

_HDR = struct.Struct("<BBHH")  # VER, TYPE, SEQ, LEN
_CRC = struct.Struct("<H")
HEADER_SIZE = len(SOF) + _HDR.size  # 8
OVERHEAD = HEADER_SIZE + _CRC.size  # 10


class FrameType(IntEnum):
    HELLO = 0x01
    HELLO_ACK = 0x02
    AUDIO_START = 0x10
    AUDIO_CHUNK = 0x11
    AUDIO_END = 0x12
    AUDIO_STOP_REQ = 0x13
    TTS_START = 0x20
    TTS_CHUNK = 0x21
    TTS_END = 0x22
    PLAYBACK_STOP = 0x23
    SENSOR_REQ = 0x30
    SENSOR_DATA = 0x31
    LOCAL_CMD_EVENT = 0x40
    DEVICE_STATUS = 0x41
    HEARTBEAT = 0x50
    HEARTBEAT_ACK = 0x51
    ERROR = 0x7F


@dataclass(frozen=True)
class Frame:
    type: int
    seq: int
    payload: bytes


def crc16_ccitt_false(data: bytes) -> int:
    """CRC-16/CCITT-FALSE: poly 0x1021, init 0xFFFF, bez odbicia, xorout 0."""
    return binascii.crc_hqx(data, 0xFFFF)


def encode(ftype: int, seq: int, payload: bytes = b"") -> bytes:
    if len(payload) > MAX_PAYLOAD:
        raise ValueError(f"ładunek {len(payload)} B > {MAX_PAYLOAD} B")
    body = _HDR.pack(VERSION, ftype, seq & 0xFFFF, len(payload)) + payload
    return SOF + body + _CRC.pack(crc16_ccitt_false(body))


class Decoder:
    """Dekoder strumieniowy z resynchronizacją po błędzie."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self.rejected = 0

    def feed(self, data: bytes) -> list[Frame]:
        self._buf += data
        frames: list[Frame] = []
        while True:
            start = self._buf.find(SOF)
            if start < 0:
                # zachowaj ostatni bajt, bo może być początkiem SOF
                del self._buf[:-1]
                return frames
            del self._buf[:start]
            if len(self._buf) < HEADER_SIZE:
                return frames
            ver, ftype, seq, length = _HDR.unpack_from(self._buf, len(SOF))
            if ver != VERSION or length > MAX_PAYLOAD:
                self._reject()
                continue
            total = HEADER_SIZE + length + _CRC.size
            if len(self._buf) < total:
                return frames
            body = bytes(self._buf[len(SOF):HEADER_SIZE + length])
            (crc,) = _CRC.unpack_from(self._buf, HEADER_SIZE + length)
            if crc != crc16_ccitt_false(body):
                self._reject()
                continue
            frames.append(Frame(ftype, seq, body[_HDR.size:]))
            del self._buf[:total]

    def _reject(self) -> None:
        self.rejected += 1
        del self._buf[:1]  # szukaj SOF od następnego bajtu


# Rekord czujnika: sensor_id u8, flags u8, value f32, age_ms u16, reserved u8
SENSOR_RECORD = struct.Struct("<BBfHB")


def parse_sensor_data(payload: bytes) -> list[tuple[int, int, float, int]]:
    count = payload[0]
    return [
        SENSOR_RECORD.unpack_from(payload, 1 + i * SENSOR_RECORD.size)[:4]
        for i in range(count)
    ]


if __name__ == "__main__":
    assert crc16_ccitt_false(b"123456789") == 0x29B1
    assert SENSOR_RECORD.size == 9

    f1 = encode(FrameType.HEARTBEAT, 1, struct.pack("<I", 1234))
    f2 = encode(FrameType.AUDIO_CHUNK, 2, b"\x00" + bytes(range(256)) * 2)
    corrupted = bytearray(f1)
    corrupted[9] ^= 0xFF

    dec = Decoder()
    out = []
    stream = b"\x13\x37" + bytes(corrupted) + f1 + f2
    for i in range(0, len(stream), 7):  # dane w małych porcjach, jak z UART
        out += dec.feed(stream[i:i + 7])

    assert [(f.type, f.seq) for f in out] == [(FrameType.HEARTBEAT, 1),
                                              (FrameType.AUDIO_CHUNK, 2)]
    assert dec.rejected == 1
    rec = struct.pack("<B", 1) + SENSOR_RECORD.pack(0x01, 0x01, 21.5, 120, 0)
    assert parse_sensor_data(rec) == [(0x01, 0x01, 21.5, 120)]
    print("OK")
