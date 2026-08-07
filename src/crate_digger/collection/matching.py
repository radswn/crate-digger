import posixpath
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

from crate_digger.collection.index import _ensure_schema
from crate_digger.collection.models import ImportMatch, ImportedTrack


WINDOWS_DRIVE_RE = re.compile(r"^/[A-Za-z]:/")


@dataclass(frozen=True)
class PathMap:
    source: str
    destination: str


@dataclass(frozen=True)
class IndexedTrack:
    path: str
    title: str | None
    artist: str | None


def parse_path_map(value: str) -> PathMap:
    if "=" not in value:
        raise ValueError(f"Invalid path map {value!r}; expected SOURCE=DESTINATION")
    source, destination = value.split("=", maxsplit=1)
    source = source.strip()
    destination = destination.strip()
    if not source or not destination:
        raise ValueError(
            f"Invalid path map {value!r}; source and destination must be non-empty"
        )
    return PathMap(source=source, destination=destination)


def load_indexed_tracks(
    db_path: Path, *, ensure_schema: bool = True
) -> list[IndexedTrack]:
    if not ensure_schema and not db_path.is_file():
        return []
    if ensure_schema:
        db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        if ensure_schema:
            _ensure_schema(conn)
        elif (
            conn.execute(
                "select 1 from sqlite_master where type = 'table' and name = 'tracks'"
            ).fetchone()
            is None
        ):
            return []
        rows = conn.execute("select path, title, artist from tracks").fetchall()
    return [IndexedTrack(str(path), title, artist) for path, title, artist in rows]


def match_tracks(
    imported_tracks: list[ImportedTrack],
    indexed_tracks: list[IndexedTrack],
    path_maps: tuple[PathMap, ...] = (),
) -> list[ImportMatch]:
    exact: dict[str, list[IndexedTrack]] = {}
    insensitive: dict[str, list[IndexedTrack]] = {}
    fallback: dict[tuple[str, str, str], list[IndexedTrack]] = {}
    for track in indexed_tracks:
        normalized_path = normalize_path(track.path)
        exact.setdefault(normalized_path, []).append(track)
        insensitive.setdefault(normalized_path.casefold(), []).append(track)
        key = (
            PurePosixPath(normalized_path).name.casefold(),
            normalize_text(track.artist),
            normalize_text(track.title),
        )
        fallback.setdefault(key, []).append(track)

    results: list[ImportMatch] = []
    for track in imported_tracks:
        if track.invalid_reason or not track.source_path:
            results.append(
                ImportMatch(
                    track=track,
                    status="invalid",
                    track_path=None,
                    candidate_paths=(),
                    reason=track.invalid_reason or "Missing track location",
                )
            )
            continue

        mapped_path = apply_path_maps(track.source_path, path_maps)
        normalized_path = normalize_path(mapped_path)
        candidates = exact.get(normalized_path, [])
        if len(candidates) == 1:
            results.append(_matched(track, candidates[0], "Exact normalized path"))
            continue
        if len(candidates) > 1:
            results.append(_ambiguous(track, candidates, "Duplicate exact paths"))
            continue

        candidates = insensitive.get(normalized_path.casefold(), [])
        if len(candidates) == 1:
            results.append(_matched(track, candidates[0], "Case-insensitive path"))
            continue
        if len(candidates) > 1:
            results.append(
                _ambiguous(track, candidates, "Multiple case-insensitive paths")
            )
            continue

        artist = normalize_text(track.artist)
        title = normalize_text(track.title)
        if artist and title:
            key = (PurePosixPath(normalized_path).name.casefold(), artist, title)
            candidates = fallback.get(key, [])
            if len(candidates) == 1:
                results.append(
                    _matched(track, candidates[0], "Unique filename, artist, and title")
                )
                continue
            if len(candidates) > 1:
                results.append(
                    _ambiguous(
                        track,
                        candidates,
                        "Multiple tracks share filename, artist, and title",
                    )
                )
                continue

        results.append(
            ImportMatch(
                track=track,
                status="unmatched",
                track_path=None,
                candidate_paths=(),
                reason="No conservative path or metadata match",
            )
        )
    return results


def apply_path_maps(path: str, path_maps: tuple[PathMap, ...]) -> str:
    decoded = decode_path(path)
    candidate = decoded.replace("\\", "/")
    for path_map in path_maps:
        source = decode_path(path_map.source).replace("\\", "/").rstrip("/")
        destination = decode_path(path_map.destination).replace("\\", "/").rstrip("/")
        if candidate.casefold() == source.casefold():
            return destination
        prefix = f"{source}/"
        if candidate[: len(prefix)].casefold() == prefix.casefold():
            return f"{destination}/{candidate[len(prefix) :]}"
    return decoded


def decode_path(value: str) -> str:
    value = value.strip()
    if value.casefold().startswith("file:"):
        parsed = urlsplit(value)
        path = unquote(parsed.path)
        if parsed.netloc and parsed.netloc.casefold() != "localhost":
            if re.fullmatch(r"[A-Za-z]:", parsed.netloc):
                path = f"{parsed.netloc}{path}"
            else:
                path = f"//{parsed.netloc}{path}"
        if WINDOWS_DRIVE_RE.match(path):
            path = path[1:]
        return path
    return unquote(value)


def normalize_path(value: str) -> str:
    decoded = decode_path(value).replace("\\", "/")
    if WINDOWS_DRIVE_RE.match(decoded):
        decoded = decoded[1:]
    normalized = posixpath.normpath(decoded)
    if normalized == ".":
        return ""
    if re.match(r"^[A-Za-z]:/", normalized):
        normalized = f"{normalized[0].upper()}{normalized[1:]}"
    return normalized


def normalize_text(value: str | None) -> str:
    return " ".join((value or "").split()).casefold()


def _matched(track: ImportedTrack, candidate: IndexedTrack, reason: str) -> ImportMatch:
    return ImportMatch(
        track=track,
        status="matched",
        track_path=candidate.path,
        candidate_paths=(candidate.path,),
        reason=reason,
    )


def _ambiguous(
    track: ImportedTrack, candidates: list[IndexedTrack], reason: str
) -> ImportMatch:
    return ImportMatch(
        track=track,
        status="ambiguous",
        track_path=None,
        candidate_paths=tuple(sorted(candidate.path for candidate in candidates)),
        reason=reason,
    )
