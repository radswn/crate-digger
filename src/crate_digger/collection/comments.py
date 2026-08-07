import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mutagen import File, MutagenError
from mutagen.id3 import COMM, ID3NoHeaderError


REKORDBOX_COMMENT_BLOCK_RE = re.compile(r"/\*(.*?)\*/", re.DOTALL)
COMMENT_KEYS = ("comment", "comments", "description")
PROFILE_TAGS = {
    "tech": ("palette", "tech"),
    "house": ("palette", "house"),
    "groovy": ("groove", "groovy"),
    "rolling": ("groove", "rolling"),
    "deep": ("palette", "deep"),
    "minimal": ("palette", "minimal"),
}


@dataclass(frozen=True)
class CommentCleanupWriteResult:
    cleaned: bool
    before_comment: str | None
    after_comment: str | None


def clean_rekordbox_comment_tags(comment: str | None) -> str | None:
    if not comment:
        return None

    blocks: list[str] = []
    for match in REKORDBOX_COMMENT_BLOCK_RE.finditer(comment):
        block = _normalize_rekordbox_block(match.group(1))
        if block is not None:
            blocks.append(block)

    return " ".join(blocks) if blocks else None


def extract_profile_tags(comment: str | None) -> tuple[tuple[str, str], ...]:
    """Extract and canonicalise Rekordbox-style ``/* A / B */`` comment tags."""

    if not comment:
        return ()
    tags: set[tuple[str, str]] = set()
    for match in REKORDBOX_COMMENT_BLOCK_RE.finditer(comment):
        for raw_tag in match.group(1).split("/"):
            value = " ".join(raw_tag.split()).casefold()
            if not value:
                continue
            category, canonical = PROFILE_TAGS.get(value, ("legacy", value))
            tags.add((category, canonical))
    return tuple(sorted(tags))


def write_rekordbox_comment_tags_only(path: Path) -> CommentCleanupWriteResult:
    before_comment = read_comment(path)
    after_comment = clean_rekordbox_comment_tags(before_comment)
    if _normalized_empty(before_comment) == _normalized_empty(after_comment):
        return CommentCleanupWriteResult(
            cleaned=False,
            before_comment=before_comment,
            after_comment=after_comment,
        )

    cleaned = write_comment(path, after_comment)
    return CommentCleanupWriteResult(
        cleaned=cleaned,
        before_comment=before_comment,
        after_comment=after_comment if cleaned else before_comment,
    )


def clear_comment(path: Path) -> CommentCleanupWriteResult:
    before_comment = read_comment(path)
    if not before_comment:
        return CommentCleanupWriteResult(
            cleaned=False,
            before_comment=before_comment,
            after_comment=None,
        )

    cleaned = write_comment(path, None)
    return CommentCleanupWriteResult(
        cleaned=cleaned,
        before_comment=before_comment,
        after_comment=None if cleaned else before_comment,
    )


def read_comment(path: Path) -> str | None:
    try:
        audio = File(path, easy=True)
    except MutagenError:
        audio = None
    if audio is not None and audio.tags is not None:
        for key in COMMENT_KEYS:
            value = audio.tags.get(key)
            text = _first_text(value)
            if text:
                return text

    try:
        audio = File(path, easy=False)
    except MutagenError:
        return None

    if audio is None or audio.tags is None:
        return None
    return _read_raw_comment(audio.tags)


def _read_raw_comment(tags: Any) -> str | None:
    for key in COMMENT_KEYS:
        value = _tag_get(tags, key)
        text = _first_text(value)
        if text:
            return text

    mp4_comment = _first_text(_tag_get(tags, "\xa9cmt"))
    if mp4_comment:
        return mp4_comment

    for key, value in tags.items():
        if str(key).casefold() in COMMENT_KEYS:
            text = _first_text(value)
            if text:
                return text
        if isinstance(value, COMM):
            text = _first_text(value.text)
            if text:
                return text

    return None


def _tag_get(tags: Any, key: str) -> Any:
    try:
        return tags.get(key)
    except (KeyError, ValueError):
        return None


def write_comment(path: Path, comment: str | None) -> bool:
    try:
        audio = File(path, easy=False)
    except MutagenError:
        return False
    if audio is None:
        return False

    try:
        if path.suffix.lower() in {".mp3", ".wav", ".aiff", ".aif"}:
            _write_id3_comment(audio, comment)
        elif path.suffix.lower() in {".m4a", ".mp4", ".alac"}:
            _write_mp4_comment(audio, comment)
        else:
            _write_mapping_comment(audio, comment)
        audio.save()
    except (MutagenError, OSError):
        return False

    return True


def _normalize_rekordbox_block(raw_block: str) -> str | None:
    tags = [
        re.sub(r"\s+", " ", tag).strip()
        for tag in raw_block.split("/")
        if re.sub(r"\s+", " ", tag).strip()
    ]
    if not tags:
        return None
    return f"/* {' / '.join(tags)} */"


def _write_id3_comment(audio: Any, comment: str | None) -> None:
    if audio.tags is None:
        try:
            audio.add_tags()
        except ID3NoHeaderError:
            pass
    if audio.tags is None:
        return

    audio.tags.delall("COMM")
    if comment:
        audio.tags.add(COMM(encoding=3, lang="eng", desc="", text=[comment]))


def _write_mp4_comment(audio: Any, comment: str | None) -> None:
    if comment:
        audio["\xa9cmt"] = [comment]
    elif "\xa9cmt" in audio:
        del audio["\xa9cmt"]


def _write_mapping_comment(audio: Any, comment: str | None) -> None:
    if audio.tags is None:
        audio.add_tags()
    if audio.tags is None:
        return

    for key in COMMENT_KEYS + tuple(key.upper() for key in COMMENT_KEYS):
        if key in audio:
            del audio[key]
    if comment:
        audio["comment"] = [comment]


def _first_text(value: Any) -> str | None:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="ignore")
    if isinstance(value, str):
        return value
    if isinstance(value, list | tuple) and value:
        return _first_text(value[0])
    return None


def _normalized_empty(value: str | None) -> str | None:
    return value if value not in ("", None) else None
