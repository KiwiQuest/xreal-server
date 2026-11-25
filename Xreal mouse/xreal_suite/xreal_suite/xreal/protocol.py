"""Protokol bryli XREAL One / One Pro (TCP na 169.254.2.1).

Porty (podle komunitnich projektu a vlastniho mereni):
  52996 metadata | 52997 video (SLAM kamery Eye) | 52998 IMU | 52999 ridici kanal

Ramce:
  IMU:     0x28 0x36 + u32 big-endian delka tela + telo     (celkem 134 B)
  ridici:  0x27, id zpravy, u32 big-endian delka + telo
"""
import math
import socket
import struct
from dataclasses import dataclass
from typing import Iterator, List, Optional, Tuple

HOST = "169.254.2.1"
PORT_META = 52996
PORT_VIDEO = 52997
PORT_IMU = 52998
PORT_CONTROL = 52999

IMU_MAGICS = (b"\x28\x36", b"\x27\x36")
CTRL_MAGIC = 0x27

TYPE_IMU = 11          # gyro + akcelerometr (~1000 Hz)
TYPE_MAG = 4           # magnetometr (~400 Hz)
MAG_INVALID = -3200.0

MSG_KEY_RAW = 0x2E     # u32 id tlacitka, u32 stav (1 = dolu, 2 = nahoru)
MSG_KEY_GESTURE = 0x12  # u32 gesto (1 klik, 2 dvojklik, 3 dlouhy stisk), u32 id
MSG_TEMPERATURE = 0x8A  # protobuf s teplotami

G = 9.80665


@dataclass
class ImuSample:
    ts_ns: int
    gyro: Tuple[float, float, float]      # rad/s
    acc: Tuple[float, float, float]       # m/s^2
    temp: float


@dataclass
class MagSample:
    ts_ns: int
    mag: Tuple[float, float, float]       # uT


def split_imu_frames(buf: bytearray) -> List[bytes]:
    """Vyjme z bufferu kompletni IMU ramce (magic + delka big-endian + telo)."""
    out: List[bytes] = []
    while True:
        pos = -1
        for m in IMU_MAGICS:
            p = buf.find(m)
            if p != -1 and (pos == -1 or p < pos):
                pos = p
        if pos == -1:
            del buf[:-1]
            return out
        if pos > 0:
            del buf[:pos]
        if len(buf) < 6:
            return out
        length = int.from_bytes(buf[2:6], "big")
        if length < 0x44 or length > 4096:       # falesna hlavicka uvnitr dat
            del buf[:1]
            continue
        total = 6 + length
        if len(buf) < total:
            return out
        out.append(bytes(buf[:total]))
        del buf[:total]


def parse_imu_frame(fr: bytes) -> Optional[object]:
    """ImuSample / MagSample / None (neplatny nebo neznamy ramec)."""
    if len(fr) < 0x4A:
        return None
    ptype, = struct.unpack_from("<I", fr, 0x1E)
    ts, = struct.unpack_from("<Q", fr, 0x0E)
    if ptype == TYPE_IMU:
        gyro = struct.unpack_from("<fff", fr, 0x22)
        acc = struct.unpack_from("<fff", fr, 0x2E)
        if any(math.isnan(v) or math.isinf(v) for v in gyro + acc):
            return None
        temp, = struct.unpack_from("<f", fr, 0x46)
        return ImuSample(ts, gyro, acc, temp)
    if ptype == TYPE_MAG:
        mag = struct.unpack_from("<fff", fr, 0x3A)
        if any(math.isnan(v) for v in mag) or mag[0] == MAG_INVALID:
            return None
        return MagSample(ts, mag)
    return None


def control_frames(buf: bytearray) -> List[Tuple[int, bytes]]:
    """Ridici kanal: (id zpravy, telo)."""
    out: List[Tuple[int, bytes]] = []
    while True:
        pos = buf.find(bytes([CTRL_MAGIC]))
        if pos == -1:
            buf.clear()
            return out
        if pos > 0:
            del buf[:pos]
        if len(buf) < 6:
            return out
        length = int.from_bytes(buf[2:6], "big")
        if buf[2:4] != b"\x00\x00" or length > 4096:
            del buf[:1]
            continue
        if len(buf) < 6 + length:
            return out
        out.append((buf[1], bytes(buf[6:6 + length])))
        del buf[:6 + length]


def parse_key_event(body: bytes) -> Optional[Tuple[int, int]]:
    """Telo zpravy 0x2E -> (id tlacitka, stav 1=dolu / 2=nahoru)."""
    if len(body) < 8:
        return None
    return struct.unpack_from("<II", body, 0)


# ------------------------------ zdroje dat ---------------------------------
def socket_chunks(port: int, host: str = HOST, timeout: float = 5.0) -> Iterator[bytes]:
    sock = socket.create_connection((host, port), timeout=timeout)
    try:
        while True:
            data = sock.recv(65536)
            if not data:
                return
            yield data
    finally:
        sock.close()


def file_chunks(path: str, size: int = 4096) -> Iterator[bytes]:
    with open(path, "rb") as f:
        while True:
            data = f.read(size)
            if not data:
                return
            yield data


def imu_samples(chunks: Iterator[bytes]) -> Iterator[object]:
    buf = bytearray()
    for chunk in chunks:
        buf.extend(chunk)
        for fr in split_imu_frames(buf):
            s = parse_imu_frame(fr)
            if s is not None:
                yield s


# ------------------------------ pomocne pro testy --------------------------
def build_imu_frame(ts_ns: int, gyro, acc, temp: float = 40.0, ptype: int = TYPE_IMU,
                    mag=(0.0, 0.0, 0.0)) -> bytes:
    body = bytearray(134 - 6)
    fr = bytearray(b"\x28\x36" + (134 - 6).to_bytes(4, "big") + bytes(body))
    struct.pack_into("<Q", fr, 0x0E, ts_ns)
    struct.pack_into("<I", fr, 0x1E, ptype)
    struct.pack_into("<fff", fr, 0x22, *gyro)
    struct.pack_into("<fff", fr, 0x2E, *acc)
    struct.pack_into("<fff", fr, 0x3A, *mag)
    struct.pack_into("<f", fr, 0x46, temp)
    return bytes(fr)
