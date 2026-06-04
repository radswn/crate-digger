import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mutagen import File, MutagenError
from mutagen.aiff import AIFF
from mutagen.flac import FLAC
from mutagen.id3 import ID3NoHeaderError, TIT2
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4
from mutagen.wave import WAVE

from crate_digger.collection.scanner import discover_audio_files


INSTRUMENTAL_TAIL_RE = re.compile(
    r"[\s_\-–—]*\(?instrumental\)?[\s_\-–—]*$", re.IGNORECASE
)
ACAPELLA_TAIL_RE = re.compile(
    r"[\s_\-–—]*\(?(?:a\s*capp?ella|acapella|vocals?)\)?[\s_\-–—]*$",
    re.IGNORECASE,
)
TRAILING_SEPARATOR_RE = re.compile(r"[\s_\-–—\.,;:]+$")


@dataclass(frozen=True)
class TitleSuffixRule:
    path: Path
    suffix: str
    tail_pattern: re.Pattern[str]


@dataclass(frozen=True)
class TitleSuffixResult:
    path: Path
    before_title: str | None
    after_title: str | None
    changed: bool
    error: str | None = None


def ensure_title_suffix(
    title: str, *, suffix: str, tail_pattern: re.Pattern[str]
) -> str:
    """Return title with exactly one canonical suffix at the end."""

    normalized = unicodedata.normalize("NFKC", title).strip()
    base = tail_pattern.sub("", normalized)
    base = TRAILING_SEPARATOR_RE.sub("", base).strip()
    if not base:
        base = normalized
    return f"{base} ({suffix})"


def ensure_title_suffixes(
    rules: Iterable[TitleSuffixRule],
    *,
    apply: bool,
    filters: Iterable[str] = (),
    limit: int | None = None,
) -> list[TitleSuffixResult]:
    normalized_filters = [value.casefold() for value in filters]
    results: list[TitleSuffixResult] = []
    processed = 0

    for rule in rules:
        for path in discover_audio_files([rule.path]):
            if normalized_filters and not any(
                value in path.name.casefold() for value in normalized_filters
            ):
                continue

            results.append(
                ensure_path_title_suffix(
                    path,
                    suffix=rule.suffix,
                    tail_pattern=rule.tail_pattern,
                    apply=apply,
                )
            )
            processed += 1
            if limit is not None and processed >= limit:
                return results

    return results


def ensure_path_title_suffix(
    path: Path,
    *,
    suffix: str,
    tail_pattern: re.Pattern[str],
    apply: bool,
) -> TitleSuffixResult:
    try:
        before_title = read_title(path)
        title_base = before_title or path.stem
        after_title = ensure_title_suffix(
            title_base,
            suffix=suffix,
            tail_pattern=tail_pattern,
        )
        changed = before_title != after_title
        if changed and apply:
            write_title(path, after_title)
        return TitleSuffixResult(
            path=path,
            before_title=before_title,
            after_title=after_title,
            changed=changed,
        )
    except (MutagenError, OSError, ValueError) as exc:
        return TitleSuffixResult(
            path=path,
            before_title=None,
            after_title=None,
            changed=False,
            error=str(exc),
        )


def read_title(path: Path) -> str | None:
    audio = File(path, easy=True)
    title = _first_text(_tag_get(getattr(audio, "tags", None), "title"))
    if title:
        return title

    audio = File(path, easy=False)
    if audio is None:
        raise ValueError("unsupported audio file")

    tags = getattr(audio, "tags", None)
    title = _first_text(_tag_get(tags, "TIT2"))
    if title:
        return title

    return _first_text(_tag_get(tags, "\xa9nam"))


def write_title(path: Path, title: str) -> None:
    suffix = path.suffix.casefold()
    if suffix == ".flac":
        audio = FLAC(path)
        audio["title"] = [title]
        audio.save()
        return

    if suffix == ".mp3":
        audio = MP3(path)
        _write_id3_title(audio, title)
        audio.save()
        return

    if suffix == ".wav":
        audio = WAVE(path)
        _write_id3_title(audio, title)
        audio.save()
        return

    if suffix in {".aiff", ".aif"}:
        audio = AIFF(path)
        _write_id3_title(audio, title)
        audio.save()
        return

    if suffix in {".m4a", ".mp4", ".alac"}:
        audio = MP4(path)
        audio["\xa9nam"] = [title]
        audio.save()
        return

    audio = File(path, easy=True)
    if audio is None:
        raise ValueError("unsupported audio file")
    audio["title"] = [title]
    audio.save()


def _write_id3_title(audio: Any, title: str) -> None:
    if audio.tags is None:
        try:
            audio.add_tags()
        except ID3NoHeaderError:
            pass
    if audio.tags is None:
        raise ValueError("could not create ID3 tags")
    audio.tags.setall("TIT2", [TIT2(encoding=3, text=title)])


def _tag_get(tags: Any, key: str) -> Any:
    if tags is None:
        return None
    try:
        return tags.get(key)
    except AttributeError:
        return None


def _first_text(value: Any) -> str | None:
    if value is None:
        return None
    text = getattr(value, "text", value)
    if isinstance(text, list):
        return str(text[0]) if text else None
    if isinstance(text, tuple):
        return str(text[0]) if text else None
    if isinstance(text, str):
        return text or None
    return str(text)
