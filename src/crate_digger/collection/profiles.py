import sqlite3
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from crate_digger.collection.index import DEFAULT_COLLECTION_DB_PATH, _ensure_schema
from crate_digger.collection.models import (
    ImportReport,
    ImportSource,
    LibraryImport,
    LocalTrack,
    ProfileRole,
    SourceTrackMetadata,
    TagCategory,
    TagSource,
    TrackProfile,
    TrackTag,
)


PROFILE_ROLES = frozenset(
    {"warmup", "builder", "peak", "reset", "afterhours", "closer"}
)
TAG_CATEGORIES = frozenset({"groove", "palette", "mood", "structure", "legacy"})
TAG_SOURCES = frozenset({"manual", "rekordbox", "traktor", "model"})
REVIEW_MODES = frozenset({"missing-energy", "all", "imported", "conflicts"})


def get_profile(
    db_path: Path = DEFAULT_COLLECTION_DB_PATH,
    *,
    track_path: str,
) -> TrackProfile | None:
    with _connect(db_path) as conn:
        row = conn.execute(
            "select * from track_profiles where track_path = ?", (track_path,)
        ).fetchone()
    return _profile_from_row(row) if row is not None else None


def upsert_manual_profile(
    db_path: Path = DEFAULT_COLLECTION_DB_PATH,
    *,
    track_path: str,
    energy: int | None,
    personal_rating: int | None,
    set_role: str | None,
    notes: str | None,
) -> TrackProfile:
    _validate_scale("energy", energy)
    _validate_scale("personal_rating", personal_rating)
    if set_role is not None and set_role not in PROFILE_ROLES:
        raise ValueError(f"Invalid set role: {set_role}")
    updated_at = _now()
    with _connect(db_path) as conn:
        if not _track_exists(conn, track_path):
            raise KeyError(f"Indexed track not found: {track_path}")
        conn.execute(
            """
            insert into track_profiles (
                track_path, energy, personal_rating, set_role, notes, updated_at
            ) values (?, ?, ?, ?, ?, ?)
            on conflict(track_path) do update set
                energy = excluded.energy,
                personal_rating = excluded.personal_rating,
                set_role = excluded.set_role,
                notes = excluded.notes,
                updated_at = excluded.updated_at
            """,
            (track_path, energy, personal_rating, set_role, notes, updated_at),
        )
    return TrackProfile(
        track_path=track_path,
        energy=energy,
        personal_rating=personal_rating,
        set_role=cast(ProfileRole | None, set_role),
        notes=notes,
        updated_at=updated_at,
    )


def list_tags(
    db_path: Path = DEFAULT_COLLECTION_DB_PATH,
    *,
    track_path: str,
) -> list[TrackTag]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            select * from track_tags
            where track_path = ?
            order by category, value, source
            """,
            (track_path,),
        ).fetchall()
    return [_tag_from_row(row) for row in rows]


def list_all_tags(
    db_path: Path = DEFAULT_COLLECTION_DB_PATH,
) -> list[TrackTag]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            select * from track_tags
            order by lower(track_path), category, value, source
            """
        ).fetchall()
    return [_tag_from_row(row) for row in rows]


def add_tag(
    db_path: Path = DEFAULT_COLLECTION_DB_PATH,
    *,
    track_path: str,
    category: str,
    value: str,
    source: str = "manual",
    approved: bool = True,
    confidence: float | None = None,
) -> TrackTag:
    normalized_value = _validate_tag(category, value, source)
    updated_at = _now()
    with _connect(db_path) as conn:
        if not _track_exists(conn, track_path):
            raise KeyError(f"Indexed track not found: {track_path}")
        _upsert_tag(
            conn,
            track_path=track_path,
            category=category,
            value=normalized_value,
            source=source,
            approved=approved,
            confidence=confidence,
            updated_at=updated_at,
        )
    return TrackTag(
        track_path=track_path,
        category=cast(TagCategory, category),
        value=normalized_value,
        source=cast(TagSource, source),
        approved=approved,
        confidence=confidence,
        updated_at=updated_at,
    )


def remove_manual_tag(
    db_path: Path = DEFAULT_COLLECTION_DB_PATH,
    *,
    track_path: str,
    category: str,
    value: str,
) -> bool:
    with _connect(db_path) as conn:
        cursor = conn.execute(
            """
            delete from track_tags
            where track_path = ? and category = ? and value = ? and source = 'manual'
            """,
            (track_path, category, value.strip().casefold()),
        )
        return cursor.rowcount > 0


def replace_manual_tags(
    db_path: Path = DEFAULT_COLLECTION_DB_PATH,
    *,
    track_path: str,
    tags: Iterable[tuple[str, str]],
) -> list[TrackTag]:
    normalized = {
        (category, _validate_tag(category, value, "manual")) for category, value in tags
    }
    updated_at = _now()
    with _connect(db_path) as conn:
        if not _track_exists(conn, track_path):
            raise KeyError(f"Indexed track not found: {track_path}")
        conn.execute(
            "delete from track_tags where track_path = ? and source = 'manual'",
            (track_path,),
        )
        for category, value in sorted(normalized):
            _upsert_tag(
                conn,
                track_path=track_path,
                category=category,
                value=value,
                source="manual",
                approved=True,
                confidence=None,
                updated_at=updated_at,
            )
    return [
        TrackTag(
            track_path=track_path,
            category=cast(TagCategory, category),
            value=value,
            source="manual",
            approved=True,
            confidence=None,
            updated_at=updated_at,
        )
        for category, value in sorted(normalized)
    ]


def upsert_source_metadata(
    db_path: Path = DEFAULT_COLLECTION_DB_PATH,
    *,
    metadata: SourceTrackMetadata,
) -> None:
    with _connect(db_path) as conn:
        if not _track_exists(conn, metadata.track_path):
            raise KeyError(f"Indexed track not found: {metadata.track_path}")
        _upsert_source_metadata(conn, metadata)


def list_source_metadata(
    db_path: Path = DEFAULT_COLLECTION_DB_PATH,
    *,
    track_path: str,
) -> list[SourceTrackMetadata]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            select * from track_source_metadata
            where track_path = ? order by source
            """,
            (track_path,),
        ).fetchall()
    return [_source_metadata_from_row(row) for row in rows]


def get_track(
    db_path: Path = DEFAULT_COLLECTION_DB_PATH,
    *,
    track_path: str,
) -> LocalTrack | None:
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            select path, title, artist, album, comment, genre, release_date,
                   file_created_at, duration_seconds, bitrate, audio_format,
                   artwork_mime, spotify_uri, soundcloud_url,
                   spotify_link_skipped_at, indexed_at
            from tracks where path = ?
            """,
            (track_path,),
        ).fetchone()
    return _track_from_row(row) if row is not None else None


def get_profile_review_candidates(
    db_path: Path = DEFAULT_COLLECTION_DB_PATH,
    *,
    mode: str = "missing-energy",
) -> list[LocalTrack]:
    if mode not in REVIEW_MODES:
        raise ValueError(f"Invalid review mode: {mode}")
    where = {
        "missing-energy": "p.energy is null",
        "all": "1 = 1",
        "imported": "exists (select 1 from track_source_metadata s where s.track_path = t.path)",
        "conflicts": """
            exists (
                select 1 from track_source_metadata r
                join track_source_metadata k on k.track_path = r.track_path
                where r.track_path = t.path and r.source = 'rekordbox'
                  and k.source = 'traktor' and r.legacy_rating is not null
                  and k.legacy_rating is not null
                  and r.legacy_rating != k.legacy_rating
            ) or exists (
                select 1 from track_tags manual
                join track_tags imported
                  on imported.track_path = manual.track_path
                 and imported.value = manual.value
                where manual.track_path = t.path and manual.source = 'manual'
                  and imported.source in ('rekordbox', 'traktor')
                  and manual.category != imported.category
            )
        """,
    }[mode]
    with _connect(db_path) as conn:
        rows = conn.execute(
            f"""
            select t.path, t.title, t.artist, t.album, t.comment, t.genre,
                   t.release_date, t.file_created_at, t.duration_seconds,
                   t.bitrate, t.audio_format, t.artwork_mime, t.spotify_uri,
                   t.soundcloud_url, t.spotify_link_skipped_at, t.indexed_at
            from tracks t
            left join track_profiles p on p.track_path = t.path
            where {where}
            order by lower(coalesce(t.artist, '')), lower(coalesce(t.title, t.stem)),
                     lower(t.path)
            """
        ).fetchall()
    return [_track_from_row(row) for row in rows]


def calculate_status_counts(
    db_path: Path = DEFAULT_COLLECTION_DB_PATH,
) -> dict[str, Any]:
    with _connect(db_path) as conn:
        counts = conn.execute(
            """
            select
                count(*) as total_tracks,
                count(*) filter (where exists (
                    select 1 from track_source_metadata s where s.track_path = t.path
                )) as tracks_with_source_metadata,
                count(*) filter (where exists (
                    select 1 from track_source_metadata s
                    where s.track_path = t.path and s.source = 'rekordbox'
                )) as rekordbox_tracks,
                count(*) filter (where exists (
                    select 1 from track_source_metadata s
                    where s.track_path = t.path and s.source = 'traktor'
                )) as traktor_tracks,
                count(p.track_path) as tracks_with_manual_profiles,
                count(*) filter (where p.energy is null) as tracks_missing_energy,
                count(p.energy) as tracks_with_energy,
                count(p.personal_rating) as tracks_with_personal_rating,
                count(p.set_role) as tracks_with_role
            from tracks t left join track_profiles p on p.track_path = t.path
            """
        ).fetchone()
        by_category = dict(
            conn.execute(
                "select category, count(*) from track_tags group by category order by category"
            ).fetchall()
        )
        by_source = dict(
            conn.execute(
                "select source, count(*) from track_tags group by source order by source"
            ).fetchall()
        )
        latest_imports = dict(
            conn.execute(
                """
                select source_type, max(imported_at) from library_imports
                where dry_run = 0 group by source_type order by source_type
                """
            ).fetchall()
        )
    result = dict(counts) if counts is not None else {}
    result["tags_by_category"] = by_category
    result["tags_by_source"] = by_source
    result["latest_imports"] = latest_imports
    return result


def list_library_imports(
    db_path: Path = DEFAULT_COLLECTION_DB_PATH,
    *,
    limit: int = 50,
) -> list[LibraryImport]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            select * from library_imports
            order by imported_at desc, id desc limit ?
            """,
            (max(0, limit),),
        ).fetchall()
    return [
        LibraryImport(
            import_id=int(row["id"]),
            source_type=cast(ImportSource, row["source_type"]),
            source_file=str(row["source_file"]),
            imported_at=str(row["imported_at"]),
            dry_run=bool(row["dry_run"]),
            parsed_count=int(row["parsed_count"]),
            matched_count=int(row["matched_count"]),
            unmatched_count=int(row["unmatched_count"]),
            ambiguous_count=int(row["ambiguous_count"]),
            invalid_count=int(row["invalid_count"]),
        )
        for row in rows
    ]


def persist_import(
    db_path: Path,
    report: ImportReport,
) -> ImportReport:
    if report.dry_run:
        return report
    imported_at = _now()
    ratings = 0
    tags = 0
    with _connect(db_path) as conn:
        changes_before = conn.total_changes
        for match in report.matches:
            if match.status != "matched" or match.track_path is None:
                continue
            track = match.track
            metadata = SourceTrackMetadata(
                track_path=match.track_path,
                source=track.source,
                source_track_id=track.source_track_id,
                legacy_rating=track.legacy_rating,
                genre=track.genre,
                comment=track.comment,
                comment2=track.comment2,
                imported_at=imported_at,
            )
            _upsert_source_metadata(conn, metadata)
            if track.legacy_rating is not None:
                ratings += 1
            conn.execute(
                "delete from track_tags where track_path = ? and source = ?",
                (match.track_path, track.source),
            )
            for category, value in track.tags:
                _upsert_tag(
                    conn,
                    track_path=match.track_path,
                    category=category,
                    value=value,
                    source=track.source,
                    approved=True,
                    confidence=None,
                    updated_at=imported_at,
                )
                tags += 1
        cursor = conn.execute(
            """
            insert into library_imports (
                source_type, source_file, imported_at, dry_run, parsed_count,
                matched_count, unmatched_count, ambiguous_count, invalid_count
            ) values (?, ?, ?, 0, ?, ?, ?, ?, ?)
            """,
            (
                report.source_type,
                report.source_file,
                imported_at,
                report.parsed_count,
                report.matched_count,
                report.unmatched_count,
                report.ambiguous_count,
                report.invalid_count,
            ),
        )
        changes = conn.total_changes - changes_before
        import_id = int(cursor.lastrowid) if cursor.lastrowid is not None else None
    return ImportReport(
        source_type=report.source_type,
        source_file=report.source_file,
        dry_run=False,
        matches=report.matches,
        imported_ratings=ratings,
        imported_tags=tags,
        database_changes=changes,
        import_id=import_id,
        imported_at=imported_at,
    )


def list_training_rows(db_path: Path = DEFAULT_COLLECTION_DB_PATH) -> list[sqlite3.Row]:
    with _connect(db_path) as conn:
        return conn.execute(
            """
            select t.path, t.artist, t.title, t.album, t.duration_seconds,
                   t.audio_format, t.spotify_uri, p.energy, p.personal_rating,
                   p.set_role,
                   max(case when s.source = 'rekordbox' then s.legacy_rating end)
                       as rekordbox_legacy_rating,
                   max(case when s.source = 'traktor' then s.legacy_rating end)
                       as traktor_legacy_rating
            from tracks t
            left join track_profiles p on p.track_path = t.path
            left join track_source_metadata s on s.track_path = t.path
            group by t.path order by lower(t.path)
            """
        ).fetchall()


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    return conn


def _track_exists(conn: sqlite3.Connection, track_path: str) -> bool:
    return (
        conn.execute("select 1 from tracks where path = ?", (track_path,)).fetchone()
        is not None
    )


def _upsert_tag(
    conn: sqlite3.Connection,
    *,
    track_path: str,
    category: str,
    value: str,
    source: str,
    approved: bool,
    confidence: float | None,
    updated_at: str,
) -> None:
    conn.execute(
        """
        insert into track_tags (
            track_path, category, value, source, approved, confidence, updated_at
        ) values (?, ?, ?, ?, ?, ?, ?)
        on conflict(track_path, category, value, source) do update set
            approved = excluded.approved,
            confidence = excluded.confidence,
            updated_at = excluded.updated_at
        """,
        (track_path, category, value, source, int(approved), confidence, updated_at),
    )


def _upsert_source_metadata(
    conn: sqlite3.Connection,
    metadata: SourceTrackMetadata,
) -> None:
    conn.execute(
        """
        insert into track_source_metadata (
            track_path, source, source_track_id, legacy_rating, genre,
            comment, comment2, imported_at
        ) values (?, ?, ?, ?, ?, ?, ?, ?)
        on conflict(track_path, source) do update set
            source_track_id = excluded.source_track_id,
            legacy_rating = excluded.legacy_rating,
            genre = excluded.genre,
            comment = excluded.comment,
            comment2 = excluded.comment2,
            imported_at = excluded.imported_at
        """,
        (
            metadata.track_path,
            metadata.source,
            metadata.source_track_id,
            metadata.legacy_rating,
            metadata.genre,
            metadata.comment,
            metadata.comment2,
            metadata.imported_at,
        ),
    )


def _profile_from_row(row: sqlite3.Row) -> TrackProfile:
    return TrackProfile(
        track_path=str(row["track_path"]),
        energy=row["energy"],
        personal_rating=row["personal_rating"],
        set_role=cast(ProfileRole | None, row["set_role"]),
        notes=row["notes"],
        updated_at=str(row["updated_at"]),
    )


def _tag_from_row(row: sqlite3.Row) -> TrackTag:
    return TrackTag(
        track_path=str(row["track_path"]),
        category=cast(TagCategory, row["category"]),
        value=str(row["value"]),
        source=cast(TagSource, row["source"]),
        approved=bool(row["approved"]),
        confidence=row["confidence"],
        updated_at=str(row["updated_at"]),
    )


def _source_metadata_from_row(row: sqlite3.Row) -> SourceTrackMetadata:
    return SourceTrackMetadata(
        track_path=str(row["track_path"]),
        source=cast(ImportSource, row["source"]),
        source_track_id=row["source_track_id"],
        legacy_rating=row["legacy_rating"],
        genre=row["genre"],
        comment=row["comment"],
        comment2=row["comment2"],
        imported_at=str(row["imported_at"]),
    )


def _track_from_row(row: sqlite3.Row) -> LocalTrack:
    return LocalTrack(
        path=Path(row["path"]),
        title=row["title"],
        artist=row["artist"],
        album=row["album"],
        comment=row["comment"],
        genre=row["genre"],
        release_date=row["release_date"],
        file_created_at=row["file_created_at"],
        duration_seconds=row["duration_seconds"],
        bitrate=row["bitrate"],
        audio_format=row["audio_format"],
        artwork_mime=row["artwork_mime"],
        spotify_uri=row["spotify_uri"],
        soundcloud_url=row["soundcloud_url"],
        spotify_link_skipped_at=row["spotify_link_skipped_at"],
        indexed_at=row["indexed_at"],
    )


def _validate_scale(name: str, value: int | None) -> None:
    if value is not None and (isinstance(value, bool) or not 1 <= value <= 5):
        raise ValueError(f"{name} must be unset or an integer from 1 to 5")


def _validate_tag(category: str, value: str, source: str) -> str:
    if category not in TAG_CATEGORIES:
        raise ValueError(f"Invalid tag category: {category}")
    if source not in TAG_SOURCES:
        raise ValueError(f"Invalid tag source: {source}")
    normalized = " ".join(value.split()).casefold()
    if not normalized:
        raise ValueError("Tag value cannot be empty")
    return normalized


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
