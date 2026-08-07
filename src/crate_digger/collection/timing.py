"""Decoder timing differences when moving Rekordbox markers to Traktor.

MP3 Xing/Info without a valid LAME tag CRC needs one MPEG audio frame added.
Format research: https://github.com/digital-dj-tools/dj-data-converter/issues/3
"""

from pathlib import Path
from typing import Protocol, cast

from mutagen import MutagenError
from mutagen.mp3 import MPEGInfo


class _MPEGDetails(Protocol):
    frame_offset: int
    mode: int
    version: float
    sample_rate: int


def rekordbox_to_traktor_offset_ms(path: Path) -> float:
    if path.suffix.lower() != ".mp3":
        return 0.0
    try:
        with path.open("rb") as handle:
            # Mutagen populates these attributes dynamically from MPEGFrame.
            info = cast(_MPEGDetails, MPEGInfo(handle))
            handle.seek(info.frame_offset)
            frame = handle.read(512)
    except MutagenError as exc:
        raise ValueError(f"Invalid MP3 header in {path.name}") from exc
    xing = (
        (36 if info.mode != 3 else 21)
        if info.version == 1
        else (21 if info.mode != 3 else 13)
    )
    return _mp3_frame_offset_ms(frame, xing, info.sample_rate, info.version)


def _mp3_frame_offset_ms(
    frame: bytes, xing: int, sample_rate: int, version: float
) -> float:
    if frame[xing : xing + 4] not in (b"Xing", b"Info"):
        return 0.0
    flags = int.from_bytes(frame[xing + 4 : xing + 8], "big")
    lame = (
        xing
        + 8
        + sum(size for flag, size in ((1, 4), (2, 4), (4, 100), (8, 4)) if flags & flag)
    )
    if frame[lame : lame + 4] == b"LAME" and len(frame) >= lame + 36:
        stored = int.from_bytes(frame[lame + 34 : lame + 36], "big")
        if stored == _crc16(frame[: lame + 34]):
            return 0.0
    return (1152 if version == 1 else 576) * 1000 / sample_rate


def _crc16(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0xA001 if crc & 1 else 0)
    return crc
