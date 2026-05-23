import json
import math
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from mutagen import File, MutagenError
from mutagen.id3 import ID3NoHeaderError, TXXX


TARGET_LUFS = -14.0
LOUDNESS_ANALYSIS_TIMEOUT_SECONDS = 240


@dataclass(frozen=True)
class LoudnessMeasurement:
    gain_db: float
    peak: float | None


def write_volume_normalization_tags(
    path: Path,
    *,
    target_lufs: float = TARGET_LUFS,
) -> bool:
    measurement = measure_track_loudness(path, target_lufs=target_lufs)
    if measurement is None:
        return False
    return write_replaygain_tags(
        path,
        gain_db=measurement.gain_db,
        peak=measurement.peak,
    )


def measure_track_loudness(
    path: Path,
    *,
    target_lufs: float = TARGET_LUFS,
) -> LoudnessMeasurement | None:
    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path is None:
        return None

    try:
        result = subprocess.run(
            [
                ffmpeg_path,
                "-hide_banner",
                "-nostats",
                "-i",
                str(path),
                "-af",
                (f"loudnorm=I={target_lufs}:TP=-1.5:" "LRA=11:print_format=json"),
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            check=False,
            text=True,
            timeout=LOUDNESS_ANALYSIS_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    if result.returncode != 0:
        return None

    payload = _extract_json_object(result.stderr)
    if payload is None:
        return None

    input_lufs = _float_field(payload, "input_i")
    if input_lufs is None:
        return None

    input_peak_db = _float_field(payload, "input_tp")
    peak = None if input_peak_db is None else 10 ** (input_peak_db / 20)
    return LoudnessMeasurement(gain_db=target_lufs - input_lufs, peak=peak)


def write_replaygain_tags(
    path: Path,
    *,
    gain_db: float,
    peak: float | None,
) -> bool:
    gain_text = _format_gain(gain_db)
    peak_text = None if peak is None else _format_peak(peak)

    try:
        audio = File(path, easy=False)
    except MutagenError:
        return False
    if audio is None:
        return False

    suffix = path.suffix.lower()
    try:
        if suffix in {".mp3", ".wav"}:
            _write_id3_replaygain_tags(audio, gain_text=gain_text, peak_text=peak_text)
        elif suffix in {".m4a", ".mp4", ".alac"}:
            _write_mp4_replaygain_tags(audio, gain_text=gain_text, peak_text=peak_text)
        else:
            _write_mapping_replaygain_tags(
                audio,
                gain_text=gain_text,
                peak_text=peak_text,
            )
        audio.save()
    except (MutagenError, OSError):
        return False

    return True


def _write_id3_replaygain_tags(
    audio: Any,
    *,
    gain_text: str,
    peak_text: str | None,
) -> None:
    if audio.tags is None:
        try:
            audio.add_tags()
        except ID3NoHeaderError:
            pass
    if audio.tags is None:
        return

    _delete_txxx(audio.tags, "REPLAYGAIN_TRACK_GAIN")
    audio.tags.add(TXXX(encoding=3, desc="REPLAYGAIN_TRACK_GAIN", text=[gain_text]))
    if peak_text is not None:
        _delete_txxx(audio.tags, "REPLAYGAIN_TRACK_PEAK")
        audio.tags.add(TXXX(encoding=3, desc="REPLAYGAIN_TRACK_PEAK", text=[peak_text]))


def _write_mp4_replaygain_tags(
    audio: Any,
    *,
    gain_text: str,
    peak_text: str | None,
) -> None:
    audio["----:com.apple.iTunes:replaygain_track_gain"] = [gain_text.encode("utf-8")]
    if peak_text is not None:
        audio["----:com.apple.iTunes:replaygain_track_peak"] = [
            peak_text.encode("utf-8")
        ]


def _write_mapping_replaygain_tags(
    audio: Any,
    *,
    gain_text: str,
    peak_text: str | None,
) -> None:
    audio["REPLAYGAIN_TRACK_GAIN"] = [gain_text]
    if peak_text is not None:
        audio["REPLAYGAIN_TRACK_PEAK"] = [peak_text]


def _delete_txxx(tags: Any, desc: str) -> None:
    for key in list(tags.keys()):
        frame = tags[key]
        if isinstance(frame, TXXX) and frame.desc == desc:
            del tags[key]


def _extract_json_object(raw_output: str) -> dict[str, Any] | None:
    start = raw_output.rfind("{")
    end = raw_output.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        payload = json.loads(raw_output[start : end + 1])
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _float_field(payload: dict[str, Any], key: str) -> float | None:
    value = payload.get(key)
    try:
        parsed = float(cast(Any, value))
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _format_gain(gain_db: float) -> str:
    sign = "+" if gain_db >= 0 else ""
    return f"{sign}{gain_db:.2f} dB"


def _format_peak(peak: float) -> str:
    return f"{max(0.0, peak):.6f}"
