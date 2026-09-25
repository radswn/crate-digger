"""Preview and guarded publication of reviewed DJ values to native audio tags."""

import hashlib
import json
import os
import re
import secrets
import shutil
import tempfile
from pathlib import Path
from typing import Any

from mutagen import File, MutagenError
from mutagen.id3 import COMM, TCON

from crate_digger.collection.dj_curation import get_curation
from crate_digger.collection.index import refresh_track_metadata

_BLOCK = re.compile(r"\[\[CRATE_DIGGER_DJ:(\{[^\n]*?\})\]\]")
_MARKER = "[[CRATE_DIGGER_DJ:"
_ID3_FORMATS = {".mp3", ".wav", ".aif", ".aiff"}
_MP4_FORMATS = {".m4a", ".mp4", ".alac"}
_MAPPING_FORMATS = {".flac", ".ogg", ".opus"}


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _audio(path: Path) -> Any:
    if path.suffix.lower() not in _ID3_FORMATS | _MP4_FORMATS | _MAPPING_FORMATS:
        raise ValueError("Unsupported audio format for tag publishing")
    try:
        audio = File(path, easy=False)
    except (MutagenError, OSError) as error:
        raise ValueError(f"Could not read audio tags: {error}") from error
    if audio is None:
        raise ValueError("Could not read audio tags")
    return audio


def _comment_slots(audio: Any, suffix: str) -> list[tuple[Any, str, int, str]]:
    tags = audio.tags
    if tags is None:
        return []
    slots: list[tuple[Any, str, int, str]] = []
    if suffix in _ID3_FORMATS:
        for key, frame in tags.items():
            if isinstance(frame, COMM):
                slots.extend(
                    (frame, key, i, str(value)) for i, value in enumerate(frame.text)
                )
    else:
        keys = ("\xa9cmt",) if suffix in _MP4_FORMATS else ("comment",)
        for key in keys:
            values = tags.get(key, [])
            slots.extend((tags, key, i, str(value)) for i, value in enumerate(values))
    return slots


def _state(audio: Any, suffix: str) -> dict[str, Any]:
    tags = audio.tags
    if tags is None:
        genre = None
    elif suffix in _ID3_FORMATS:
        frame = tags.get("TCON")
        genre = str(frame.text[0]) if frame and frame.text else None
    else:
        key = "\xa9gen" if suffix in _MP4_FORMATS else "genre"
        values = tags.get(key, [])
        genre = str(values[0]) if values else None
    return {
        "genre": genre,
        "comments": [slot[3] for slot in _comment_slots(audio, suffix)],
    }


def _managed_text(
    comments: list[str], values: dict[str, Any]
) -> tuple[str | None, str]:
    occurrences = [
        (comment, match) for comment in comments for match in _BLOCK.finditer(comment)
    ]
    if sum(comment.count(_MARKER) for comment in comments) != len(occurrences):
        raise ValueError("Malformed managed DJ comment block")
    if len(occurrences) > 1:
        raise ValueError("Multiple managed DJ comment blocks")
    if occurrences:
        try:
            existing = json.loads(occurrences[0][1].group(1))
        except json.JSONDecodeError as error:
            raise ValueError("Malformed managed DJ comment block") from error
        if not isinstance(existing, dict):
            raise ValueError("Malformed managed DJ comment block")
    block = f"{_MARKER}{json.dumps(values, sort_keys=True, separators=(',', ':'))}]]"
    if occurrences:
        original, match = occurrences[0]
        return original, original[: match.start()] + block + original[match.end() :]
    original = comments[0] if comments else None
    return original, (original + "\n" if original else "") + block


def _values(curation: dict[str, Any]) -> dict[str, Any]:
    values = {
        "energy": curation["energy"],
        "tone": curation["tone"],
        "character": curation["character"],
    }
    if not curation["approved_genre"] or not isinstance(values["energy"], int):
        raise ValueError("Review Genre and Energy before publishing")
    if values["tone"] is None or not values["character"]:
        raise ValueError("Review Tone and Character before publishing")
    return values


def preview_tags(db_path: Path, track_path: str) -> dict[str, Any]:
    curation = get_curation(db_path, track_path)
    values = _values(curation)
    path = Path(track_path)
    audio = _audio(path)
    before = _state(audio, path.suffix.lower())
    _, comment = _managed_text(before["comments"], values)
    after = {"genre": curation["approved_genre"], "comment": comment}
    file_sha256 = _digest(path)
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "path": track_path,
                "file_sha256": file_sha256,
                "values": values,
                "after": after,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    return {
        "path": track_path,
        "before": before,
        "after": after,
        "file_sha256": file_sha256,
        "fingerprint": fingerprint,
        "change_needed": before["genre"] != after["genre"]
        or comment not in before["comments"],
    }


def _write_tags(path: Path, *, genre: str, comment: str) -> None:
    audio = _audio(path)
    suffix = path.suffix.lower()
    slots = _comment_slots(audio, suffix)
    if audio.tags is None:
        audio.add_tags()
    if audio.tags is None:
        raise ValueError("Audio format did not accept tags")
    if suffix in _ID3_FORMATS:
        audio.tags.setall("TCON", [TCON(encoding=3, text=[genre])])
        selected = next((slot for slot in slots if _MARKER in slot[3]), None)
        if selected is None and slots:
            selected = slots[0]
        if selected:
            frame, _, index, _ = selected
            frame.text[index] = comment
        else:
            audio.tags.add(COMM(encoding=3, lang="eng", desc="", text=[comment]))
    else:
        genre_key = "\xa9gen" if suffix in _MP4_FORMATS else "genre"
        audio.tags[genre_key] = [genre]
        selected = next((slot for slot in slots if _MARKER in slot[3]), None)
        if selected is None and slots:
            selected = slots[0]
        if selected:
            _, key, index, _ = selected
            values = list(audio.tags[key])
            values[index] = comment
            audio.tags[key] = values
        else:
            key = "\xa9cmt" if suffix in _MP4_FORMATS else "comment"
            audio.tags[key] = [comment]
    audio.save()


def apply_tags(db_path: Path, track_path: str, fingerprint: str) -> dict[str, Any]:
    preview = preview_tags(db_path, track_path)
    if preview["fingerprint"] != fingerprint:
        raise ValueError("File or reviewed metadata changed; preview tags again")
    if not preview["change_needed"]:
        return {**preview, "changed": False, "backup": None}
    path = Path(track_path)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.stem}.crate-digger-", suffix=path.suffix, dir=path.parent
    )
    os.close(descriptor)
    temporary = Path(temp_name)
    backup: Path | None = None
    try:
        shutil.copy2(path, temporary)
        _write_tags(temporary, **preview["after"])
        written = _state(_audio(temporary), path.suffix.lower())
        if (
            written["genre"] != preview["after"]["genre"]
            or preview["after"]["comment"] not in written["comments"]
        ):
            raise ValueError("Edited copy did not retain the expected tags")
        if _digest(path) != preview["file_sha256"]:
            raise ValueError(
                "Audio file changed during publication; preview tags again"
            )
        backup = path.with_name(
            f"{path.name}.crate-digger-backup-{preview['file_sha256'][:12]}-{secrets.token_hex(4)}"
        )
        with backup.open("xb") as target, path.open("rb") as source:
            shutil.copyfileobj(source, target)
        shutil.copystat(path, backup)
        if _digest(backup) != preview["file_sha256"]:
            raise ValueError("Could not verify audio backup")
        os.replace(temporary, path)
        refreshed = refresh_track_metadata(db_path, path=track_path)
        if not refreshed:
            raise ValueError(
                "Tags were written but the collection index did not refresh"
            )
        return {**preview, "changed": True, "backup": str(backup)}
    finally:
        temporary.unlink(missing_ok=True)
