import json
import os
import shutil
import sqlite3
import tempfile
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

from crate_digger.collection.exporters import (
    decode_profile_marker,
    render_rekordbox,
    render_traktor,
    strip_profile_marker,
)
from crate_digger.collection.importers.rekordbox import parse_rekordbox_document
from crate_digger.collection.importers.traktor import parse_traktor_document
from crate_digger.collection.index import _ensure_schema
from crate_digger.collection.matching import (
    PathMap,
    load_indexed_tracks,
    match_tracks,
    normalize_path,
)
from crate_digger.collection.models import (
    BeatGridMarker,
    CanonicalTrackMetadata,
    ConflictPolicy,
    CuePoint,
    ImportMatch,
    ImportedPlaylist,
    ImportedTrack,
    LibraryDocument,
    SyncConflict,
    SyncReport,
    TrackProfile,
    TrackTag,
)
from crate_digger.collection.profiles import PROFILE_ROLES, TAG_CATEGORIES


TRACK_FIELDS = (
    "title",
    "artist",
    "album",
    "genre",
    "label",
    "comment",
    "rating",
    "bpm",
    "musical_key",
    "color",
    "play_count",
    "date_added",
    "cues",
    "beatgrids",
)
POLICIES = {"manual", "latest", "prefer-rekordbox", "prefer-traktor"}


def sync_libraries(
    *,
    rekordbox_path: Path,
    traktor_path: Path,
    db_path: Path,
    dry_run: bool = True,
    conflict_policy: ConflictPolicy = "manual",
    path_maps: tuple[PathMap, ...] = (),
    backup_dir: Path | None = None,
    source_of_truth: str | None = None,
    copy_audio_to: Path | None = None,
) -> SyncReport:
    """Reconcile two collection files through canonical SQLite metadata.

    A missing destination is allowed when ``source_of_truth`` is supplied, which
    is how the migration commands create a new target collection.
    """

    if conflict_policy not in POLICIES:
        raise ValueError(f"Invalid conflict policy: {conflict_policy}")
    if source_of_truth not in {None, "rekordbox", "traktor"}:
        raise ValueError(f"Invalid source of truth: {source_of_truth}")
    documents = {
        "rekordbox": _load_document(
            rekordbox_path, "rekordbox", allow_missing=source_of_truth == "traktor"
        ),
        "traktor": _load_document(
            traktor_path, "traktor", allow_missing=source_of_truth == "rekordbox"
        ),
    }
    mtimes = {
        "rekordbox": _mtime_ns(rekordbox_path),
        "traktor": _mtime_ns(traktor_path),
    }
    matches = {
        source: _match_document(
            document,
            db_path=db_path,
            path_maps=path_maps,
            read_only=dry_run,
        )
        for source, document in documents.items()
    }
    matched_counts = {
        source: sum(item.status == "matched" for item in source_matches)
        for source, source_matches in matches.items()
    }
    payloads = {
        source: _track_payloads(source_matches)
        for source, source_matches in matches.items()
    }
    previous = _load_snapshots(db_path)
    canonical = _load_canonical_tracks(db_path)
    profiles, tags = _load_profiles_and_tags(db_path)
    open_conflicts = _load_open_conflict_keys(db_path)
    conflicts: list[_PendingConflict] = []
    origins: dict[str, dict[str, str]] = {}
    current_tracks: dict[str, CanonicalTrackMetadata] = {}
    resolved_profiles: dict[str, dict[str, object] | None] = {}
    all_track_paths = (
        set(canonical) | set(payloads["rekordbox"]) | set(payloads["traktor"])
    )
    for track_path in sorted(all_track_paths, key=str.casefold):
        existing = canonical.get(track_path)
        resolved: dict[str, object] = {}
        track_origins: dict[str, str] = {}
        for field in TRACK_FIELDS:
            value, origin, conflict = _reconcile_value(
                field_name=field,
                rekordbox=_field(payloads["rekordbox"].get(track_path), field),
                traktor=_field(payloads["traktor"].get(track_path), field),
                previous_rekordbox=_snapshot_field(
                    previous, "rekordbox", "track", track_path, field
                ),
                previous_traktor=_snapshot_field(
                    previous, "traktor", "track", track_path, field
                ),
                canonical=_canonical_field(existing, field),
                policy=(
                    cast(ConflictPolicy, f"prefer-{source_of_truth}")
                    if source_of_truth
                    else conflict_policy
                ),
                mtimes=mtimes,
            )
            resolved[field] = value
            track_origins[field] = origin
            if (
                source_of_truth is None
                and conflict_policy == "manual"
                and (track_path, None, field) in open_conflicts
                and not _same(
                    _field(payloads["rekordbox"].get(track_path), field),
                    _field(payloads["traktor"].get(track_path), field),
                )
            ):
                conflict = True
            if conflict:
                conflicts.append(
                    _PendingConflict(
                        track_path=track_path,
                        playlist_path=None,
                        field_name=field,
                        rekordbox_value=_field(
                            payloads["rekordbox"].get(track_path), field
                        ),
                        traktor_value=_field(
                            payloads["traktor"].get(track_path), field
                        ),
                    )
                )
        origins[track_path] = track_origins
        current_tracks[track_path] = _metadata_from_values(
            track_path, resolved, existing
        )
        profile_value, _profile_origin, profile_conflict = _reconcile_value(
            field_name="profile",
            rekordbox=_field(payloads["rekordbox"].get(track_path), "profile"),
            traktor=_field(payloads["traktor"].get(track_path), "profile"),
            previous_rekordbox=_snapshot_field(
                previous, "rekordbox", "track", track_path, "profile"
            ),
            previous_traktor=_snapshot_field(
                previous, "traktor", "track", track_path, "profile"
            ),
            canonical=_profile_payload(track_path, profiles, tags),
            policy=(
                cast(ConflictPolicy, f"prefer-{source_of_truth}")
                if source_of_truth
                else conflict_policy
            ),
            mtimes=mtimes,
        )
        if profile_value is not _MISSING:
            resolved_profiles[track_path] = cast(
                dict[str, object] | None, profile_value
            )
        if (
            source_of_truth is None
            and conflict_policy == "manual"
            and (track_path, None, "profile") in open_conflicts
            and not _same(
                _field(payloads["rekordbox"].get(track_path), "profile"),
                _field(payloads["traktor"].get(track_path), "profile"),
            )
        ):
            profile_conflict = True
        if profile_conflict:
            conflicts.append(
                _PendingConflict(
                    track_path=track_path,
                    playlist_path=None,
                    field_name="profile",
                    rekordbox_value=_field(
                        payloads["rekordbox"].get(track_path), "profile"
                    ),
                    traktor_value=_field(
                        payloads["traktor"].get(track_path), "profile"
                    ),
                )
            )

    playlist_payloads = {
        source: _playlist_payloads(documents[source], matches[source])
        for source in ("rekordbox", "traktor")
    }
    canonical_playlists = _load_canonical_playlists(db_path)
    current_playlists: dict[str, ImportedPlaylist] = {}
    playlist_origins: dict[str, str] = {}
    playlist_keys = (
        set(canonical_playlists)
        | set(playlist_payloads["rekordbox"])
        | set(playlist_payloads["traktor"])
    )
    for playlist_path in sorted(playlist_keys, key=str.casefold):
        rb = playlist_payloads["rekordbox"].get(playlist_path)
        traktor = playlist_payloads["traktor"].get(playlist_path)
        existing = canonical_playlists.get(playlist_path)
        value, origin, conflict = _reconcile_value(
            field_name="track_paths",
            rekordbox=list(rb.track_paths) if rb else _MISSING,
            traktor=list(traktor.track_paths) if traktor else _MISSING,
            previous_rekordbox=_snapshot_field(
                previous, "rekordbox", "playlist", playlist_path, "track_paths"
            ),
            previous_traktor=_snapshot_field(
                previous, "traktor", "playlist", playlist_path, "track_paths"
            ),
            canonical=list(existing.track_paths) if existing else _MISSING,
            policy=(
                cast(ConflictPolicy, f"prefer-{source_of_truth}")
                if source_of_truth
                else conflict_policy
            ),
            mtimes=mtimes,
        )
        template = rb or traktor or existing
        if template is None:
            continue
        current_playlists[playlist_path] = ImportedPlaylist(
            name=template.name,
            folder_path=template.folder_path,
            track_paths=tuple(cast(list[str], value if value is not _MISSING else [])),
        )
        playlist_origins[playlist_path] = origin
        if (
            source_of_truth is None
            and conflict_policy == "manual"
            and (None, playlist_path, "track_paths") in open_conflicts
            and not _same(
                list(rb.track_paths) if rb else _MISSING,
                list(traktor.track_paths) if traktor else _MISSING,
            )
        ):
            conflict = True
        if conflict:
            conflicts.append(
                _PendingConflict(
                    track_path=None,
                    playlist_path=playlist_path,
                    field_name="track_paths",
                    rekordbox_value=list(rb.track_paths) if rb else None,
                    traktor_value=list(traktor.track_paths) if traktor else None,
                )
            )

    unresolved = (
        len(conflicts) if conflict_policy == "manual" and not source_of_truth else 0
    )
    _apply_profile_payloads(profiles, tags, resolved_profiles)
    source_paths, source_ids = _source_identity_maps(matches, db_path)
    audio_plan = _plan_audio_copies(current_tracks, copy_audio_to)
    if audio_plan:
        relocated = {source: str(target) for source, target in audio_plan.items()}
        if source_of_truth == "rekordbox":
            source_paths["traktor"].update(relocated)
        elif source_of_truth == "traktor":
            source_paths["rekordbox"].update(relocated)
        else:
            source_paths["rekordbox"].update(relocated)
            source_paths["traktor"].update(relocated)
    canonical_list = list(current_tracks.values())
    playlist_list = list(current_playlists.values())
    rb_bytes, rb_changes = render_rekordbox(
        rekordbox_path,
        canonical_list,
        playlist_list,
        source_paths=source_paths["rekordbox"],
        source_ids=source_ids["rekordbox"],
        profiles=profiles,
        tags=tags,
    )
    traktor_bytes, traktor_changes = render_traktor(
        traktor_path,
        canonical_list,
        playlist_list,
        source_paths=source_paths["traktor"],
        source_ids=source_ids["traktor"],
        profiles=profiles,
        tags=tags,
    )
    if rekordbox_path.is_file() and rekordbox_path.read_bytes() == rb_bytes:
        rb_changes = 0
    if traktor_path.is_file() and traktor_path.read_bytes() == traktor_bytes:
        traktor_changes = 0
    if source_of_truth == "rekordbox":
        rb_changes = 0
    elif source_of_truth == "traktor":
        traktor_changes = 0
    if unresolved:
        rb_changes = 0
        traktor_changes = 0

    backups: list[str] = []
    copied_audio: list[Path] = []
    if not dry_run:
        try:
            copied_audio = _copy_audio_files(audio_plan)
            _persist_sync_state(
                db_path=db_path,
                current_tracks=current_tracks,
                origins=origins,
                playlists=current_playlists,
                playlist_origins=playlist_origins,
                resolved_profiles=resolved_profiles,
                matches=matches,
                source_paths=source_paths,
                source_ids=source_ids,
                payloads=payloads,
                playlist_payloads=playlist_payloads,
                mtimes=mtimes,
                conflicts=conflicts,
                policy=(
                    cast(ConflictPolicy, f"prefer-{source_of_truth}")
                    if source_of_truth
                    else conflict_policy
                ),
                rekordbox_path=rekordbox_path,
                traktor_path=traktor_path,
            )
            if not unresolved:
                outputs: list[tuple[Path, bytes]] = []
                if source_of_truth != "rekordbox":
                    outputs.append((rekordbox_path, rb_bytes))
                if source_of_truth != "traktor":
                    outputs.append((traktor_path, traktor_bytes))
                backups.extend(_write_outputs(outputs, backup_dir=backup_dir))
        except (OSError, sqlite3.Error, ValueError):
            for copied in reversed(copied_audio):
                copied.unlink(missing_ok=True)
            raise

    return SyncReport(
        dry_run=dry_run,
        matched_rekordbox=matched_counts["rekordbox"],
        matched_traktor=matched_counts["traktor"],
        canonical_tracks=len(current_tracks),
        playlists=len(current_playlists),
        conflicts=len(conflicts),
        unresolved_conflicts=unresolved,
        rekordbox_changes=rb_changes,
        traktor_changes=traktor_changes,
        audio_files_copied=(
            len(copied_audio)
            if not dry_run
            else sum(not target.exists() for target in audio_plan.values())
        ),
        backups=tuple(backups),
    )


def watch_libraries(
    *,
    rekordbox_path: Path,
    traktor_path: Path,
    db_path: Path,
    interval: float = 2.0,
    conflict_policy: ConflictPolicy = "manual",
    path_maps: tuple[PathMap, ...] = (),
    backup_dir: Path | None = None,
    copy_audio_to: Path | None = None,
    on_sync: Callable[[SyncReport], None] | None = None,
    max_cycles: int | None = None,
) -> Iterator[SyncReport]:
    if interval < 0.25:
        raise ValueError("Watch interval must be at least 0.25 seconds")
    last_state: tuple[int, int, int] | None = None
    cycles = 0
    while max_cycles is None or cycles < max_cycles:
        state = (
            _mtime_ns(rekordbox_path),
            _mtime_ns(traktor_path),
            _mtime_ns(db_path),
        )
        if state != last_state:
            report = sync_libraries(
                rekordbox_path=rekordbox_path,
                traktor_path=traktor_path,
                db_path=db_path,
                dry_run=False,
                conflict_policy=conflict_policy,
                path_maps=path_maps,
                backup_dir=backup_dir,
                copy_audio_to=copy_audio_to,
            )
            last_state = (
                _mtime_ns(rekordbox_path),
                _mtime_ns(traktor_path),
                _mtime_ns(db_path),
            )
            if on_sync:
                on_sync(report)
            yield report
        cycles += 1
        if max_cycles is None or cycles < max_cycles:
            time.sleep(interval)


def list_sync_conflicts(
    db_path: Path, *, include_resolved: bool = False
) -> list[SyncConflict]:
    if not db_path.is_file() or not _table_exists(db_path, "library_sync_conflicts"):
        return []
    with sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        where = "" if include_resolved else "where status = 'open'"
        rows = conn.execute(
            f"select * from library_sync_conflicts {where} order by id"
        ).fetchall()
    return [
        SyncConflict(
            conflict_id=int(row["id"]),
            track_path=row["track_path"],
            playlist_path=row["playlist_path"],
            field_name=str(row["field_name"]),
            rekordbox_value=_json_value(row["rekordbox_value"]),
            traktor_value=_json_value(row["traktor_value"]),
            status=str(row["status"]),
            resolution=row["resolution"],
            created_at=str(row["created_at"]),
            resolved_at=row["resolved_at"],
        )
        for row in rows
    ]


def resolve_sync_conflict(
    db_path: Path, *, conflict_id: int, resolution: str
) -> SyncConflict:
    if resolution not in {"rekordbox", "traktor"}:
        raise ValueError("Conflict resolution must be rekordbox or traktor")
    now = _now()
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        _ensure_schema(conn)
        row = conn.execute(
            "select * from library_sync_conflicts where id = ?", (conflict_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"Sync conflict not found: {conflict_id}")
        if row["status"] == "resolved":
            raise ValueError(f"Sync conflict is already resolved: {conflict_id}")
        value = _json_value(row[f"{resolution}_value"])
        if row["track_path"]:
            _write_resolved_track_field(
                conn, str(row["track_path"]), str(row["field_name"]), value, now
            )
        elif row["playlist_path"]:
            _write_resolved_playlist(
                conn, str(row["playlist_path"]), cast(list[str], value), resolution, now
            )
        conn.execute(
            """
            update library_sync_conflicts
            set status = 'resolved', resolution = ?, resolved_at = ? where id = ?
            """,
            (resolution, now, conflict_id),
        )
    return next(
        conflict
        for conflict in list_sync_conflicts(db_path, include_resolved=True)
        if conflict.conflict_id == conflict_id
    )


class _Missing:
    pass


_MISSING = _Missing()


class _PendingConflict:
    def __init__(
        self,
        *,
        track_path: str | None,
        playlist_path: str | None,
        field_name: str,
        rekordbox_value: object,
        traktor_value: object,
    ) -> None:
        self.track_path = track_path
        self.playlist_path = playlist_path
        self.field_name = field_name
        self.rekordbox_value = rekordbox_value
        self.traktor_value = traktor_value


def _load_document(path: Path, source: str, *, allow_missing: bool) -> LibraryDocument:
    if not path.is_file():
        if allow_missing:
            return LibraryDocument(source=cast(Any, source), tracks=(), playlists=())
        raise OSError(f"{source.title()} collection not found: {path}")
    return (
        parse_rekordbox_document(path)
        if source == "rekordbox"
        else parse_traktor_document(path)
    )


def _match_document(
    document: LibraryDocument,
    *,
    db_path: Path,
    path_maps: tuple[PathMap, ...],
    read_only: bool,
) -> list[ImportMatch]:
    indexed = load_indexed_tracks(db_path, ensure_schema=not read_only)
    known = _load_identities(db_path, document.source)
    indexed_paths = {item.path for item in indexed}
    pending: list[ImportedTrack] = []
    positions: list[tuple[str, ImportMatch | int]] = []
    for track in document.tracks:
        known_path = known.get(track.source_track_id or "")
        if known_path in indexed_paths:
            positions.append(
                (
                    "match",
                    ImportMatch(
                        track=track,
                        status="matched",
                        track_path=known_path,
                        candidate_paths=(known_path,),
                        reason="Stable source identity",
                    ),
                )
            )
        else:
            positions.append(("pending", len(pending)))
            pending.append(track)
    matched_pending = match_tracks(pending, indexed, path_maps)
    return [
        cast(ImportMatch, value)
        if kind == "match"
        else matched_pending[cast(int, value)]
        for kind, value in positions
    ]


def _track_payloads(matches: list[ImportMatch]) -> dict[str, dict[str, object]]:
    values: dict[str, dict[str, object]] = {}
    for match in matches:
        if match.status != "matched" or match.track_path is None:
            continue
        track = match.track
        raw_profile = decode_profile_marker(track.comment)
        values[match.track_path] = {
            "title": track.title,
            "artist": track.artist,
            "album": track.album,
            "genre": track.genre,
            "label": track.label,
            "comment": strip_profile_marker(track.comment),
            "rating": track.legacy_rating,
            "bpm": track.bpm,
            "musical_key": track.musical_key,
            "color": track.color,
            "play_count": track.play_count,
            "date_added": track.date_added,
            "cues": [asdict(item) for item in track.cues],
            "beatgrids": [asdict(item) for item in track.beatgrids],
            "profile": (
                _normalize_profile_payload(raw_profile)
                if raw_profile is not None
                else None
            ),
        }
    return values


def _playlist_payloads(
    document: LibraryDocument, matches: list[ImportMatch]
) -> dict[str, ImportedPlaylist]:
    path_lookup = {
        normalize_path(match.track.source_path): match.track_path
        for match in matches
        if match.status == "matched"
        and match.track.source_path
        and match.track_path is not None
    }
    values: dict[str, ImportedPlaylist] = {}
    for playlist in document.playlists:
        canonical_paths = tuple(
            path_lookup[normalize_path(path)]
            for path in playlist.track_paths
            if normalize_path(path) in path_lookup
        )
        key = "/".join((*playlist.folder_path, playlist.name))
        values[key] = ImportedPlaylist(
            name=playlist.name,
            folder_path=playlist.folder_path,
            track_paths=canonical_paths,
        )
    return values


def _reconcile_value(
    *,
    field_name: str,
    rekordbox: object,
    traktor: object,
    previous_rekordbox: object,
    previous_traktor: object,
    canonical: object,
    policy: ConflictPolicy,
    mtimes: dict[str, int],
) -> tuple[object, str, bool]:
    if rekordbox is None and previous_rekordbox is _MISSING:
        rekordbox = _MISSING
    if traktor is None and previous_traktor is _MISSING:
        traktor = _MISSING
    if rekordbox is _MISSING and traktor is _MISSING:
        return canonical, "canonical", False
    if rekordbox is _MISSING:
        return traktor, "traktor", False
    if traktor is _MISSING:
        return rekordbox, "rekordbox", False
    if _same(rekordbox, traktor):
        return rekordbox, "both", False
    rb_had_snapshot = previous_rekordbox is not _MISSING
    tr_had_snapshot = previous_traktor is not _MISSING
    rb_changed = rb_had_snapshot and not _same(rekordbox, previous_rekordbox)
    tr_changed = tr_had_snapshot and not _same(traktor, previous_traktor)
    if rb_had_snapshot and tr_had_snapshot:
        if rb_changed and not tr_changed:
            return rekordbox, "rekordbox", False
        if tr_changed and not rb_changed:
            return traktor, "traktor", False
        if not rb_changed and not tr_changed and canonical is not _MISSING:
            return canonical, "canonical", False
    if policy == "prefer-rekordbox":
        return rekordbox, "rekordbox", False
    if policy == "prefer-traktor":
        return traktor, "traktor", False
    if policy == "latest":
        source = "rekordbox" if mtimes["rekordbox"] >= mtimes["traktor"] else "traktor"
        return (rekordbox if source == "rekordbox" else traktor), source, False
    fallback = canonical if canonical is not _MISSING else rekordbox
    return fallback, "conflict", True


def _metadata_from_values(
    track_path: str,
    values: dict[str, object],
    existing: CanonicalTrackMetadata | None,
) -> CanonicalTrackMetadata:
    def optional(name: str) -> Any:
        value = values.get(name, _MISSING)
        if value is _MISSING:
            return getattr(existing, name) if existing else None
        return value

    return CanonicalTrackMetadata(
        track_path=track_path,
        title=optional("title"),
        artist=optional("artist"),
        album=optional("album"),
        genre=optional("genre"),
        label=optional("label"),
        comment=optional("comment"),
        rating=optional("rating"),
        bpm=optional("bpm"),
        musical_key=optional("musical_key"),
        color=optional("color"),
        play_count=optional("play_count"),
        date_added=optional("date_added"),
        cues=tuple(
            CuePoint(**item)
            for item in cast(list[dict[str, Any]], optional("cues") or [])
        ),
        beatgrids=tuple(
            BeatGridMarker(**item)
            for item in cast(list[dict[str, Any]], optional("beatgrids") or [])
        ),
        updated_at=_now(),
    )


def _persist_sync_state(
    *,
    db_path: Path,
    current_tracks: dict[str, CanonicalTrackMetadata],
    origins: dict[str, dict[str, str]],
    playlists: dict[str, ImportedPlaylist],
    playlist_origins: dict[str, str],
    resolved_profiles: dict[str, dict[str, object] | None],
    matches: dict[str, list[ImportMatch]],
    source_paths: dict[str, dict[str, str]],
    source_ids: dict[str, dict[str, str]],
    payloads: dict[str, dict[str, dict[str, object]]],
    playlist_payloads: dict[str, dict[str, ImportedPlaylist]],
    mtimes: dict[str, int],
    conflicts: list[_PendingConflict],
    policy: ConflictPolicy,
    rekordbox_path: Path,
    traktor_path: Path,
) -> None:
    now = _now()
    with sqlite3.connect(db_path) as conn:
        _ensure_schema(conn)
        for track in current_tracks.values():
            conn.execute(
                """
                insert into canonical_track_metadata (
                    track_path, title, artist, album, genre, label, comment,
                    rating, bpm, musical_key, color, play_count, date_added,
                    field_origins, updated_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(track_path) do update set
                    title=excluded.title, artist=excluded.artist, album=excluded.album,
                    genre=excluded.genre, label=excluded.label, comment=excluded.comment,
                    rating=excluded.rating, bpm=excluded.bpm,
                    musical_key=excluded.musical_key, color=excluded.color,
                    play_count=excluded.play_count, date_added=excluded.date_added,
                    field_origins=excluded.field_origins, updated_at=excluded.updated_at
                """,
                (
                    track.track_path,
                    track.title,
                    track.artist,
                    track.album,
                    track.genre,
                    track.label,
                    track.comment,
                    track.rating,
                    track.bpm,
                    track.musical_key,
                    track.color,
                    track.play_count,
                    track.date_added,
                    json.dumps(origins[track.track_path], sort_keys=True),
                    now,
                ),
            )
            conn.execute(
                "delete from canonical_track_cues where track_path = ?",
                (track.track_path,),
            )
            conn.executemany(
                """
                insert into canonical_track_cues (
                    track_path, position, name, kind, start_ms, length_ms, hotcue
                ) values (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        track.track_path,
                        position,
                        cue.name,
                        cue.kind,
                        cue.start_ms,
                        cue.length_ms,
                        cue.hotcue,
                    )
                    for position, cue in enumerate(track.cues)
                ],
            )
            conn.execute(
                "delete from canonical_track_beatgrids where track_path = ?",
                (track.track_path,),
            )
            conn.executemany(
                """
                insert into canonical_track_beatgrids (
                    track_path, position, start_ms, bpm, meter, beat
                ) values (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        track.track_path,
                        position,
                        grid.start_ms,
                        grid.bpm,
                        grid.meter,
                        grid.beat,
                    )
                    for position, grid in enumerate(track.beatgrids)
                ],
            )
        for playlist_path, playlist in playlists.items():
            conn.execute(
                """
                insert into canonical_playlists (
                    playlist_path, name, folder_path, track_paths, source, updated_at
                ) values (?, ?, ?, ?, ?, ?)
                on conflict(playlist_path) do update set
                    name=excluded.name, folder_path=excluded.folder_path,
                    track_paths=excluded.track_paths, source=excluded.source,
                    updated_at=excluded.updated_at
                """,
                (
                    playlist_path,
                    playlist.name,
                    json.dumps(playlist.folder_path, ensure_ascii=False),
                    json.dumps(playlist.track_paths, ensure_ascii=False),
                    playlist_origins[playlist_path],
                    now,
                ),
            )
        for track_path, profile in resolved_profiles.items():
            _persist_profile_payload(conn, track_path, profile, now)
        for source in ("rekordbox", "traktor"):
            for match in matches[source]:
                if match.status != "matched" or match.track_path is None:
                    continue
                imported = match.track
                conn.execute(
                    """
                    insert into track_source_metadata (
                        track_path, source, source_track_id, legacy_rating,
                        genre, comment, comment2, imported_at
                    ) values (?, ?, ?, ?, ?, ?, ?, ?)
                    on conflict(track_path, source) do update set
                        source_track_id=excluded.source_track_id,
                        legacy_rating=excluded.legacy_rating,
                        genre=excluded.genre, comment=excluded.comment,
                        comment2=excluded.comment2, imported_at=excluded.imported_at
                    """,
                    (
                        match.track_path,
                        source,
                        imported.source_track_id,
                        imported.legacy_rating,
                        imported.genre,
                        strip_profile_marker(imported.comment),
                        strip_profile_marker(imported.comment2),
                        now,
                    ),
                )
                conn.execute(
                    "delete from track_tags where track_path = ? and source = ?",
                    (match.track_path, source),
                )
                conn.executemany(
                    """
                    insert into track_tags (
                        track_path, category, value, source, approved,
                        confidence, updated_at
                    ) values (?, ?, ?, ?, 1, null, ?)
                    """,
                    [
                        (match.track_path, category, value, source, now)
                        for category, value in imported.tags
                    ],
                )
            for track_path in current_tracks:
                source_id = source_ids[source].get(track_path)
                source_path = source_paths[source].get(track_path)
                if source_id is None or source_path is None:
                    continue
                conn.execute(
                    """
                    delete from library_track_identities
                    where source = ? and source_track_id = ? and track_path != ?
                    """,
                    (source, source_id, track_path),
                )
                conn.execute(
                    """
                    insert into library_track_identities (
                        track_path, source, source_track_id, source_path, updated_at
                    ) values (?, ?, ?, ?, ?)
                    on conflict(track_path, source) do update set
                        source_track_id=excluded.source_track_id,
                        source_path=excluded.source_path,
                        updated_at=excluded.updated_at
                    """,
                    (track_path, source, source_id, source_path, now),
                )
            for track_path, payload in payloads[source].items():
                _upsert_snapshot(
                    conn, source, "track", track_path, payload, mtimes[source], now
                )
            for playlist_path, playlist in playlist_payloads[source].items():
                _upsert_snapshot(
                    conn,
                    source,
                    "playlist",
                    playlist_path,
                    {"track_paths": list(playlist.track_paths)},
                    mtimes[source],
                    now,
                )
        for conflict in conflicts:
            existing_conflict = conn.execute(
                """
                select id from library_sync_conflicts
                where status = 'open' and field_name = ?
                  and coalesce(track_path, '') = coalesce(?, '')
                  and coalesce(playlist_path, '') = coalesce(?, '')
                """,
                (
                    conflict.field_name,
                    conflict.track_path,
                    conflict.playlist_path,
                ),
            ).fetchone()
            if existing_conflict is not None:
                conn.execute(
                    """
                    update library_sync_conflicts
                    set rekordbox_value = ?, traktor_value = ? where id = ?
                    """,
                    (
                        _json_dump(conflict.rekordbox_value),
                        _json_dump(conflict.traktor_value),
                        int(existing_conflict[0]),
                    ),
                )
                continue
            conn.execute(
                """
                insert into library_sync_conflicts (
                    track_path, playlist_path, field_name, rekordbox_value,
                    traktor_value, status, created_at
                ) values (?, ?, ?, ?, ?, 'open', ?)
                """,
                (
                    conflict.track_path,
                    conflict.playlist_path,
                    conflict.field_name,
                    _json_dump(conflict.rekordbox_value),
                    _json_dump(conflict.traktor_value),
                    now,
                ),
            )
        if policy != "manual":
            conn.execute(
                """
                update library_sync_conflicts
                set status = 'resolved', resolution = ?, resolved_at = ?
                where status = 'open'
                """,
                (policy, now),
            )
        conn.execute(
            """
            insert into library_sync_runs (
                started_at, rekordbox_file, traktor_file, policy, dry_run, summary
            ) values (?, ?, ?, ?, 0, ?)
            """,
            (
                now,
                str(rekordbox_path.resolve()),
                str(traktor_path.resolve()),
                policy,
                json.dumps(
                    {
                        "tracks": len(current_tracks),
                        "playlists": len(playlists),
                        "conflicts": len(conflicts),
                    },
                    sort_keys=True,
                ),
            ),
        )


def _load_canonical_tracks(db_path: Path) -> dict[str, CanonicalTrackMetadata]:
    if not _table_exists(db_path, "canonical_track_metadata"):
        return {}
    with sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("select * from canonical_track_metadata").fetchall()
        cues = _group_rows(conn, "canonical_track_cues")
        grids = _group_rows(conn, "canonical_track_beatgrids")
    return {
        str(row["track_path"]): CanonicalTrackMetadata(
            track_path=str(row["track_path"]),
            title=row["title"],
            artist=row["artist"],
            album=row["album"],
            genre=row["genre"],
            label=row["label"],
            comment=row["comment"],
            rating=row["rating"],
            bpm=row["bpm"],
            musical_key=row["musical_key"],
            color=row["color"],
            play_count=row["play_count"],
            date_added=row["date_added"],
            cues=tuple(
                CuePoint(
                    name=item["name"],
                    kind=str(item["kind"]),
                    start_ms=float(item["start_ms"]),
                    length_ms=item["length_ms"],
                    hotcue=item["hotcue"],
                )
                for item in cues.get(str(row["track_path"]), [])
            ),
            beatgrids=tuple(
                BeatGridMarker(
                    start_ms=float(item["start_ms"]),
                    bpm=float(item["bpm"]),
                    meter=item["meter"],
                    beat=item["beat"] if "beat" in item.keys() else 1,
                )
                for item in grids.get(str(row["track_path"]), [])
            ),
            updated_at=str(row["updated_at"]),
        )
        for row in rows
    }


def _load_canonical_playlists(db_path: Path) -> dict[str, ImportedPlaylist]:
    if not _table_exists(db_path, "canonical_playlists"):
        return {}
    with sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("select * from canonical_playlists").fetchall()
    return {
        str(row["playlist_path"]): ImportedPlaylist(
            name=str(row["name"]),
            folder_path=tuple(json.loads(row["folder_path"])),
            track_paths=tuple(json.loads(row["track_paths"])),
        )
        for row in rows
    }


def _load_snapshots(
    db_path: Path,
) -> dict[tuple[str, str, str], dict[str, object]]:
    if not _table_exists(db_path, "library_sync_snapshots"):
        return {}
    with sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True) as conn:
        rows = conn.execute(
            "select source, entity_type, entity_key, payload from library_sync_snapshots"
        ).fetchall()
    return {
        (str(source), str(entity_type), str(entity_key)): json.loads(payload)
        for source, entity_type, entity_key, payload in rows
    }


def _load_open_conflict_keys(
    db_path: Path,
) -> set[tuple[str | None, str | None, str]]:
    if not _table_exists(db_path, "library_sync_conflicts"):
        return set()
    with sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True) as conn:
        return {
            (
                str(track_path) if track_path is not None else None,
                str(playlist_path) if playlist_path is not None else None,
                str(field_name),
            )
            for track_path, playlist_path, field_name in conn.execute(
                """
                select track_path, playlist_path, field_name
                from library_sync_conflicts where status = 'open'
                """
            )
        }


def _load_identities(db_path: Path, source: str) -> dict[str, str]:
    if not _table_exists(db_path, "library_track_identities"):
        return {}
    with sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True) as conn:
        return {
            str(source_id): str(track_path)
            for source_id, track_path in conn.execute(
                """
                select source_track_id, track_path from library_track_identities
                where source = ? and source_track_id is not null
                """,
                (source,),
            )
        }


def _load_profiles_and_tags(
    db_path: Path,
) -> tuple[dict[str, TrackProfile], dict[str, list[TrackTag]]]:
    if not _table_exists(db_path, "track_profiles"):
        return {}, {}
    profiles: dict[str, TrackProfile] = {}
    tags: dict[str, list[TrackTag]] = {}
    with sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        for row in conn.execute("select * from track_profiles"):
            profiles[str(row["track_path"])] = TrackProfile(
                track_path=str(row["track_path"]),
                energy=row["energy"],
                personal_rating=row["personal_rating"],
                set_role=row["set_role"],
                notes=row["notes"],
                updated_at=str(row["updated_at"]),
            )
        for row in conn.execute("select * from track_tags"):
            tags.setdefault(str(row["track_path"]), []).append(
                TrackTag(
                    track_path=str(row["track_path"]),
                    category=row["category"],
                    value=str(row["value"]),
                    source=row["source"],
                    approved=bool(row["approved"]),
                    confidence=row["confidence"],
                    updated_at=str(row["updated_at"]),
                )
            )
    return profiles, tags


def _profile_payload(
    track_path: str,
    profiles: dict[str, TrackProfile],
    tags: dict[str, list[TrackTag]],
) -> object:
    profile = profiles.get(track_path)
    manual_tags = sorted(
        f"{tag.category}:{tag.value}"
        for tag in tags.get(track_path, [])
        if tag.source == "manual"
    )
    if profile is None and not manual_tags:
        return _MISSING
    return {
        "energy": profile.energy if profile else None,
        "personal_rating": profile.personal_rating if profile else None,
        "set_role": profile.set_role if profile else None,
        "notes": profile.notes if profile else None,
        "tags": manual_tags,
    }


def _apply_profile_payloads(
    profiles: dict[str, TrackProfile],
    tags: dict[str, list[TrackTag]],
    payloads: dict[str, dict[str, object] | None],
) -> None:
    now = _now()
    for track_path, raw in payloads.items():
        if raw is None:
            profiles.pop(track_path, None)
            tags[track_path] = [
                tag for tag in tags.get(track_path, []) if tag.source != "manual"
            ]
            continue
        payload = _normalize_profile_payload(raw)
        profiles[track_path] = TrackProfile(
            track_path=track_path,
            energy=cast(int | None, payload["energy"]),
            personal_rating=cast(int | None, payload["personal_rating"]),
            set_role=cast(Any, payload["set_role"]),
            notes=cast(str | None, payload["notes"]),
            updated_at=now,
        )
        retained = [tag for tag in tags.get(track_path, []) if tag.source != "manual"]
        for encoded in cast(list[str], payload["tags"]):
            category, value = encoded.split(":", maxsplit=1)
            retained.append(
                TrackTag(
                    track_path=track_path,
                    category=cast(Any, category),
                    value=value,
                    source="manual",
                    approved=True,
                    confidence=None,
                    updated_at=now,
                )
            )
        tags[track_path] = retained


def _normalize_profile_payload(
    raw: dict[str, object] | None,
) -> dict[str, object]:
    if raw is None:
        raise ValueError("Track Profile marker payload must be an object")
    energy = raw.get("energy")
    rating = raw.get("personal_rating")
    role = raw.get("set_role")
    notes = raw.get("notes")
    raw_tags = raw.get("tags", [])
    for name, value in (("energy", energy), ("personal_rating", rating)):
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 5
        ):
            raise ValueError(f"Invalid {name} in Track Profile marker")
    if role is not None and role not in PROFILE_ROLES:
        raise ValueError("Invalid set_role in Track Profile marker")
    if notes is not None and not isinstance(notes, str):
        raise ValueError("Invalid notes in Track Profile marker")
    if not isinstance(raw_tags, list) or not all(
        isinstance(item, str) and ":" in item for item in raw_tags
    ):
        raise ValueError("Invalid tags in Track Profile marker")
    normalized_tags: list[str] = []
    for encoded in cast(list[str], raw_tags):
        category, value = encoded.split(":", maxsplit=1)
        category = category.strip().casefold()
        value = " ".join(value.split()).casefold()
        if category not in TAG_CATEGORIES or not value:
            raise ValueError(f"Invalid tag in Track Profile marker: {encoded}")
        normalized_tags.append(f"{category}:{value}")
    return {
        "energy": energy,
        "personal_rating": rating,
        "set_role": role,
        "notes": notes,
        "tags": sorted(set(normalized_tags)),
    }


def _source_identity_maps(
    matches: dict[str, list[ImportMatch]], db_path: Path
) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, str]]]:
    paths = {"rekordbox": {}, "traktor": {}}
    ids = {"rekordbox": {}, "traktor": {}}
    if _table_exists(db_path, "library_track_identities"):
        with sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True) as conn:
            for track_path, source, source_id, source_path in conn.execute(
                "select track_path, source, source_track_id, source_path from library_track_identities"
            ):
                paths[str(source)][str(track_path)] = str(source_path)
                if source_id:
                    ids[str(source)][str(track_path)] = str(source_id)
    for source, source_matches in matches.items():
        for match in source_matches:
            if match.status != "matched" or match.track_path is None:
                continue
            paths[source][match.track_path] = (
                match.track.source_path or match.track_path
            )
            if match.track.source_track_id:
                ids[source][match.track_path] = match.track.source_track_id
    for source in ("rekordbox", "traktor"):
        all_paths = set(paths["rekordbox"]) | set(paths["traktor"])
        for track_path in all_paths:
            paths[source].setdefault(track_path, track_path)
            ids[source].setdefault(track_path, _generated_source_id(source, track_path))
    return paths, ids


def _plan_audio_copies(
    tracks: dict[str, CanonicalTrackMetadata], destination: Path | None
) -> dict[str, Path]:
    if destination is None:
        return {}
    sources = [Path(path) for path in tracks if Path(path).is_file()]
    if not sources:
        return {}
    try:
        common_root = Path(os.path.commonpath([str(path.parent) for path in sources]))
    except ValueError:
        common_root = Path("/")
    plan: dict[str, Path] = {}
    used_targets: dict[Path, str] = {}
    for source in sources:
        try:
            relative = source.relative_to(common_root)
        except ValueError:
            relative = Path(*source.parts[1:]) if source.is_absolute() else source
        target = destination / relative
        previous = used_targets.get(target)
        if previous is not None and previous != str(source):
            raise ValueError(f"Audio copy destination collision: {target}")
        if target.is_file() and not _same_file_content(source, target):
            raise ValueError(
                f"Audio destination exists with different content: {target}"
            )
        used_targets[target] = str(source)
        plan[str(source)] = target
    return plan


def _copy_audio_files(plan: dict[str, Path]) -> list[Path]:
    copied: list[Path] = []
    try:
        for raw_source, target in plan.items():
            source = Path(raw_source)
            if target.is_file():
                if not _same_file_content(source, target):
                    raise ValueError(
                        f"Audio destination exists with different content: {target}"
                    )
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as handle:
                temporary = Path(handle.name)
            try:
                shutil.copy2(source, temporary)
                os.replace(temporary, target)
                copied.append(target)
            finally:
                if temporary.exists():
                    temporary.unlink()
    except (OSError, ValueError):
        for target in reversed(copied):
            target.unlink(missing_ok=True)
        raise
    return copied


def _same_file_content(left: Path, right: Path) -> bool:
    if left.stat().st_size != right.stat().st_size:
        return False
    left_hash = sha256()
    right_hash = sha256()
    with left.open("rb") as left_handle, right.open("rb") as right_handle:
        while True:
            left_chunk = left_handle.read(1024 * 1024)
            right_chunk = right_handle.read(1024 * 1024)
            if not left_chunk and not right_chunk:
                break
            left_hash.update(left_chunk)
            right_hash.update(right_chunk)
    return left_hash.digest() == right_hash.digest()


def _write_outputs(
    outputs: list[tuple[Path, bytes]], *, backup_dir: Path | None
) -> list[str]:
    changed = [
        (path, content)
        for path, content in outputs
        if not path.is_file() or path.read_bytes() != content
    ]
    originals = {
        path: path.read_bytes() if path.is_file() else None
        for path, _content in changed
    }
    backups: list[str] = []
    for path, _content in changed:
        if path.is_file():
            destination_dir = backup_dir or path.parent / ".crate-digger-backups"
            destination_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            backup = destination_dir / f"{path.name}.{timestamp}.bak"
            shutil.copy2(path, backup)
            backups.append(str(backup))
    written: list[Path] = []
    try:
        for path, content in changed:
            _atomic_write(path, content)
            written.append(path)
    except OSError:
        for path in reversed(written):
            original = originals[path]
            if original is None:
                path.unlink(missing_ok=True)
            else:
                _atomic_write(path, original)
        raise
    return backups


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        handle.write(content)
        temporary = Path(handle.name)
    try:
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _write_resolved_track_field(
    conn: sqlite3.Connection,
    track_path: str,
    field: str,
    value: object,
    now: str,
) -> None:
    if field == "profile":
        _persist_profile_payload(
            conn, track_path, cast(dict[str, object] | None, value), now
        )
        return
    if field not in TRACK_FIELDS:
        raise ValueError(f"Unsupported conflict field: {field}")
    if field == "cues":
        conn.execute(
            "delete from canonical_track_cues where track_path = ?", (track_path,)
        )
        conn.executemany(
            """
            insert into canonical_track_cues (
                track_path, position, name, kind, start_ms, length_ms, hotcue
            ) values (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    track_path,
                    index,
                    item.get("name"),
                    item["kind"],
                    item["start_ms"],
                    item.get("length_ms"),
                    item.get("hotcue"),
                )
                for index, item in enumerate(cast(list[dict[str, Any]], value or []))
            ],
        )
    elif field == "beatgrids":
        conn.execute(
            "delete from canonical_track_beatgrids where track_path = ?", (track_path,)
        )
        conn.executemany(
            """
            insert into canonical_track_beatgrids (
                track_path, position, start_ms, bpm, meter, beat
            ) values (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    track_path,
                    index,
                    item["start_ms"],
                    item["bpm"],
                    item.get("meter"),
                    item.get("beat", 1),
                )
                for index, item in enumerate(cast(list[dict[str, Any]], value or []))
            ],
        )
    else:
        conn.execute(
            f"update canonical_track_metadata set {field} = ?, updated_at = ? where track_path = ?",
            (value, now, track_path),
        )


def _persist_profile_payload(
    conn: sqlite3.Connection,
    track_path: str,
    raw: dict[str, object] | None,
    now: str,
) -> None:
    if raw is None:
        conn.execute("delete from track_profiles where track_path = ?", (track_path,))
        conn.execute(
            "delete from track_tags where track_path = ? and source = 'manual'",
            (track_path,),
        )
        return
    payload = _normalize_profile_payload(raw)
    conn.execute(
        """
        insert into track_profiles (
            track_path, energy, personal_rating, set_role, notes, updated_at
        ) values (?, ?, ?, ?, ?, ?)
        on conflict(track_path) do update set
            energy=excluded.energy, personal_rating=excluded.personal_rating,
            set_role=excluded.set_role, notes=excluded.notes,
            updated_at=excluded.updated_at
        """,
        (
            track_path,
            payload["energy"],
            payload["personal_rating"],
            payload["set_role"],
            payload["notes"],
            now,
        ),
    )
    conn.execute(
        "delete from track_tags where track_path = ? and source = 'manual'",
        (track_path,),
    )
    conn.executemany(
        """
        insert into track_tags (
            track_path, category, value, source, approved, confidence, updated_at
        ) values (?, ?, ?, 'manual', 1, null, ?)
        """,
        [
            (track_path, *encoded.split(":", maxsplit=1), now)
            for encoded in cast(list[str], payload["tags"])
        ],
    )


def _write_resolved_playlist(
    conn: sqlite3.Connection,
    playlist_path: str,
    value: list[str],
    resolution: str,
    now: str,
) -> None:
    conn.execute(
        """
        update canonical_playlists
        set track_paths = ?, source = ?, updated_at = ? where playlist_path = ?
        """,
        (json.dumps(value, ensure_ascii=False), resolution, now, playlist_path),
    )


def _upsert_snapshot(
    conn: sqlite3.Connection,
    source: str,
    entity_type: str,
    entity_key: str,
    payload: dict[str, object],
    mtime: int,
    now: str,
) -> None:
    conn.execute(
        """
        insert into library_sync_snapshots (
            source, entity_type, entity_key, payload, source_mtime_ns, imported_at
        ) values (?, ?, ?, ?, ?, ?)
        on conflict(source, entity_type, entity_key) do update set
            payload=excluded.payload, source_mtime_ns=excluded.source_mtime_ns,
            imported_at=excluded.imported_at
        """,
        (source, entity_type, entity_key, _json_dump(payload), mtime, now),
    )


def _group_rows(conn: sqlite3.Connection, table: str) -> dict[str, list[sqlite3.Row]]:
    tables = {
        "canonical_track_cues",
        "canonical_track_beatgrids",
    }
    if table not in tables:
        raise ValueError(f"Unsupported table: {table}")
    rows = conn.execute(
        f"select * from {table} order by track_path, position"
    ).fetchall()
    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(str(row["track_path"]), []).append(row)
    return grouped


def _field(payload: dict[str, object] | None, field: str) -> object:
    return payload[field] if payload is not None and field in payload else _MISSING


def _snapshot_field(
    snapshots: dict[tuple[str, str, str], dict[str, object]],
    source: str,
    entity_type: str,
    key: str,
    field: str,
) -> object:
    payload = snapshots.get((source, entity_type, key))
    return _field(payload, field)


def _canonical_field(metadata: CanonicalTrackMetadata | None, field: str) -> object:
    if metadata is None:
        return _MISSING
    value = getattr(metadata, field)
    if field in {"cues", "beatgrids"}:
        return [asdict(item) for item in value]
    return value


def _same(left: object, right: object) -> bool:
    return _json_dump(left) == _json_dump(right)


def _json_dump(value: object) -> str:
    if value is _MISSING:
        return "null"
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_value(value: str | None) -> object:
    return json.loads(value) if value is not None else None


def _table_exists(db_path: Path, name: str) -> bool:
    if not db_path.is_file():
        return False
    with sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True) as conn:
        return (
            conn.execute(
                "select 1 from sqlite_master where type = 'table' and name = ?",
                (name,),
            ).fetchone()
            is not None
        )


def _generated_source_id(source: str, track_path: str) -> str:
    digest = sha256(track_path.encode("utf-8")).hexdigest()
    return str(int(digest[:14], 16)) if source == "rekordbox" else digest[:32]


def _mtime_ns(path: Path) -> int:
    return path.stat().st_mtime_ns if path.exists() else 0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
