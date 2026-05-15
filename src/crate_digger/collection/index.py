import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from math import ceil
from pathlib import Path

from crate_digger.collection.models import LocalTrack
from crate_digger.collection.scanner import discover_audio_files, read_track_metadata

DEFAULT_COLLECTION_DB_PATH = Path(".crate_digger_state/collection.sqlite3")

SORT_COLUMNS = {
    "title": (
        "case when 1 then 0 else 0 end",
        "lower(coalesce(nullif(title, ''), stem))",
    ),
    "artist": (
        "case when artist is null or artist = '' then 1 else 0 end",
        "lower(coalesce(artist, ''))",
    ),
    "album": (
        "case when album is null or album = '' then 1 else 0 end",
        "lower(coalesce(album, ''))",
    ),
    "format": (
        "case when audio_format is null or audio_format = '' then 1 else 0 end",
        "audio_format",
    ),
    "bitrate": ("case when bitrate is null then 1 else 0 end", "bitrate"),
    "duration": (
        "case when duration_seconds is null then 1 else 0 end",
        "duration_seconds",
    ),
    "path": ("case when 1 then 0 else 0 end", "lower(path)"),
}


@dataclass(frozen=True)
class CollectionIndexStats:
    discovered_files: int
    indexed_files: int
    updated_files: int
    deleted_files: int


@dataclass(frozen=True)
class TrackQueryResult:
    tracks: list[LocalTrack]
    filtered_count: int
    total_count: int
    total_pages: int
    page: int
    formats: list[str]


def refresh_collection_index(
    music_dirs: Iterable[str | Path],
    db_path: Path = DEFAULT_COLLECTION_DB_PATH,
) -> CollectionIndexStats:
    """Refresh the SQLite index so request-time dashboard queries stay fast."""

    db_path.parent.mkdir(parents=True, exist_ok=True)
    files = discover_audio_files(music_dirs)
    discovered_paths = {str(path): path for path in files}

    with sqlite3.connect(db_path) as conn:
        _ensure_schema(conn)
        existing = _load_existing_file_state(conn)
        indexed_files = 0
        updated_files = 0

        for path_key, path in discovered_paths.items():
            try:
                stat = path.stat()
            except OSError:
                continue

            file_state = (stat.st_size, stat.st_mtime_ns)
            if existing.get(path_key) == file_state:
                continue

            track = read_track_metadata(path)
            _upsert_track(conn, track, size=stat.st_size, mtime_ns=stat.st_mtime_ns)
            indexed_files += 1
            if path_key in existing:
                updated_files += 1

        deleted_files = _delete_missing_tracks(conn, set(discovered_paths))

    return CollectionIndexStats(
        discovered_files=len(discovered_paths),
        indexed_files=indexed_files,
        updated_files=updated_files,
        deleted_files=deleted_files,
    )


def query_tracks(
    db_path: Path = DEFAULT_COLLECTION_DB_PATH,
    *,
    q: str,
    audio_format: str,
    metadata: str,
    sort: str,
    direction: str,
    page: int,
    page_size: int,
) -> TrackQueryResult:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        _ensure_schema(conn)

        where_sql, params = _build_where_clause(
            q=q,
            audio_format=audio_format,
            metadata=metadata,
        )
        total_count = _count(conn, "")
        filtered_count = _count(conn, where_sql, params)
        total_pages = max(1, ceil(filtered_count / page_size))
        page = min(max(1, page), total_pages)
        offset = (page - 1) * page_size
        missing_expr, sort_expr = SORT_COLUMNS.get(sort, SORT_COLUMNS["title"])
        direction_sql = "desc" if direction == "desc" else "asc"

        rows = conn.execute(
            f"""
            select path, title, artist, album, duration_seconds, bitrate, audio_format
            from tracks
            {where_sql}
            order by {missing_expr} asc, {sort_expr} {direction_sql}, lower(path) asc
            limit ? offset ?
            """,
            (*params, page_size, offset),
        ).fetchall()

        formats = [
            row[0]
            for row in conn.execute(
                """
                select distinct audio_format
                from tracks
                where audio_format is not null and audio_format != ''
                order by audio_format
                """
            ).fetchall()
        ]

    return TrackQueryResult(
        tracks=[_track_from_row(row) for row in rows],
        filtered_count=filtered_count,
        total_count=total_count,
        total_pages=total_pages,
        page=page,
        formats=formats,
    )


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        create table if not exists tracks (
            path text primary key,
            stem text not null,
            title text,
            artist text,
            album text,
            duration_seconds real,
            bitrate integer,
            audio_format text,
            size integer not null,
            mtime_ns integer not null,
            indexed_at text not null
        )
        """
    )
    conn.execute("create index if not exists idx_tracks_format on tracks(audio_format)")
    conn.execute("create index if not exists idx_tracks_title on tracks(title)")
    conn.execute("create index if not exists idx_tracks_artist on tracks(artist)")
    conn.execute("create index if not exists idx_tracks_album on tracks(album)")
    conn.execute("create index if not exists idx_tracks_bitrate on tracks(bitrate)")
    conn.execute(
        "create index if not exists idx_tracks_duration on tracks(duration_seconds)"
    )


def _load_existing_file_state(conn: sqlite3.Connection) -> dict[str, tuple[int, int]]:
    rows = conn.execute("select path, size, mtime_ns from tracks").fetchall()
    return {str(path): (int(size), int(mtime_ns)) for path, size, mtime_ns in rows}


def _upsert_track(
    conn: sqlite3.Connection,
    track: LocalTrack,
    *,
    size: int,
    mtime_ns: int,
) -> None:
    conn.execute(
        """
        insert into tracks (
            path,
            stem,
            title,
            artist,
            album,
            duration_seconds,
            bitrate,
            audio_format,
            size,
            mtime_ns,
            indexed_at
        )
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        on conflict(path) do update set
            stem = excluded.stem,
            title = excluded.title,
            artist = excluded.artist,
            album = excluded.album,
            duration_seconds = excluded.duration_seconds,
            bitrate = excluded.bitrate,
            audio_format = excluded.audio_format,
            size = excluded.size,
            mtime_ns = excluded.mtime_ns,
            indexed_at = excluded.indexed_at
        """,
        (
            str(track.path),
            track.path.stem,
            track.title,
            track.artist,
            track.album,
            track.duration_seconds,
            track.bitrate,
            track.audio_format,
            size,
            mtime_ns,
            datetime.now(timezone.utc).isoformat(),
        ),
    )


def _delete_missing_tracks(conn: sqlite3.Connection, discovered_paths: set[str]) -> int:
    existing_paths = {row[0] for row in conn.execute("select path from tracks")}
    missing_paths = existing_paths.difference(discovered_paths)
    conn.executemany(
        "delete from tracks where path = ?",
        [(path,) for path in missing_paths],
    )
    return len(missing_paths)


def _build_where_clause(
    *,
    q: str,
    audio_format: str,
    metadata: str,
) -> tuple[str, tuple[object, ...]]:
    parts: list[str] = []
    params: list[object] = []

    if q:
        parts.append(
            """
            lower(
                coalesce(title, '') || ' ' ||
                coalesce(artist, '') || ' ' ||
                coalesce(album, '') || ' ' ||
                coalesce(audio_format, '') || ' ' ||
                path
            ) like ?
            """
        )
        params.append(f"%{q.casefold()}%")

    if audio_format:
        parts.append("audio_format = ?")
        params.append(audio_format)

    if metadata == "missing":
        parts.append(
            """
            (
                title is null or title = '' or
                artist is null or artist = '' or
                album is null or album = ''
            )
            """
        )
    elif metadata == "complete":
        parts.append(
            """
            title is not null and title != '' and
            artist is not null and artist != '' and
            album is not null and album != ''
            """
        )

    if not parts:
        return "", ()

    return f"where {' and '.join(parts)}", tuple(params)


def _count(
    conn: sqlite3.Connection,
    where_sql: str,
    params: tuple[object, ...] = (),
) -> int:
    return int(
        conn.execute(f"select count(*) from tracks {where_sql}", params).fetchone()[0]
    )


def _track_from_row(row: sqlite3.Row) -> LocalTrack:
    return LocalTrack(
        path=Path(row["path"]),
        title=row["title"],
        artist=row["artist"],
        album=row["album"],
        duration_seconds=row["duration_seconds"],
        bitrate=row["bitrate"],
        audio_format=row["audio_format"],
    )
