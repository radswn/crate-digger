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
    "genre": (
        "case when genre is null or genre = '' then 1 else 0 end",
        "lower(coalesce(genre, ''))",
    ),
    "release_date": (
        "case when release_date is null or release_date = '' then 1 else 0 end",
        "release_date",
    ),
    "file_created_at": (
        "case when file_created_at is null or file_created_at = '' then 1 else 0 end",
        "file_created_at",
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


def get_track_artwork(
    db_path: Path = DEFAULT_COLLECTION_DB_PATH,
    *,
    path: str,
) -> tuple[str, bytes] | None:
    with sqlite3.connect(db_path) as conn:
        _ensure_schema(conn)
        row = conn.execute(
            """
            select artwork_mime, artwork_data
            from tracks
            where path = ?
            """,
            (path,),
        ).fetchone()

    if row is None or row[0] is None or row[1] is None:
        return None
    return str(row[0]), bytes(row[1])


def get_track_for_spotify_linking(
    db_path: Path = DEFAULT_COLLECTION_DB_PATH,
    *,
    path: str | None = None,
) -> LocalTrack | None:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        _ensure_schema(conn)
        if path is not None:
            row = conn.execute(
                """
                select
                    path,
                    title,
                    artist,
                    album,
                    comment,
                    genre,
                    release_date,
                    file_created_at,
                    duration_seconds,
                    bitrate,
                    audio_format,
                    artwork_mime,
                    spotify_uri,
                    soundcloud_url,
                    spotify_link_skipped_at,
                    indexed_at
                from tracks
                where path = ?
                """,
                (path,),
            ).fetchone()
        else:
            row = conn.execute(
                """
                select
                    path,
                    title,
                    artist,
                    album,
                    comment,
                    genre,
                    release_date,
                    file_created_at,
                    duration_seconds,
                    bitrate,
                    audio_format,
                    artwork_mime,
                    spotify_uri,
                    soundcloud_url,
                    spotify_link_skipped_at,
                    indexed_at
                from tracks
                where spotify_uri is null
                  and soundcloud_url is null
                  and spotify_link_skipped_at is null
                order by lower(coalesce(nullif(artist, ''), '')), lower(coalesce(nullif(title, ''), stem)), lower(path)
                limit 1
                """
            ).fetchone()

    if row is None:
        return None
    return _track_from_row(row)


def list_tracks_missing_spotify_artwork(
    db_path: Path = DEFAULT_COLLECTION_DB_PATH,
) -> list[LocalTrack]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        _ensure_schema(conn)
        rows = conn.execute(
            """
            select
                path,
                title,
                artist,
                album,
                comment,
                genre,
                release_date,
                file_created_at,
                duration_seconds,
                bitrate,
                audio_format,
                artwork_mime,
                spotify_uri,
                soundcloud_url,
                spotify_link_skipped_at,
                indexed_at
            from tracks
            where spotify_uri is not null
              and artwork_mime is null
            order by lower(coalesce(nullif(artist, ''), '')),
                     lower(coalesce(nullif(title, ''), stem)),
                     lower(path)
            """
        ).fetchall()

    return [_track_from_row(row) for row in rows]


def list_tracks_pending_spotify_linking(
    db_path: Path = DEFAULT_COLLECTION_DB_PATH,
) -> list[LocalTrack]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        _ensure_schema(conn)
        rows = conn.execute(
            """
            select
                path,
                title,
                artist,
                album,
                comment,
                genre,
                release_date,
                file_created_at,
                duration_seconds,
                bitrate,
                audio_format,
                artwork_mime,
                spotify_uri,
                soundcloud_url,
                spotify_link_skipped_at,
                indexed_at
            from tracks
            where spotify_uri is null
              and soundcloud_url is null
              and spotify_link_skipped_at is null
            order by lower(coalesce(nullif(artist, ''), '')),
                     lower(coalesce(nullif(title, ''), stem)),
                     lower(path)
            """
        ).fetchall()

    return [_track_from_row(row) for row in rows]


def list_tracks_for_comment_cleanup(
    db_path: Path = DEFAULT_COLLECTION_DB_PATH,
) -> list[LocalTrack]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        _ensure_schema(conn)
        rows = conn.execute(
            """
            select
                path,
                title,
                artist,
                album,
                comment,
                genre,
                release_date,
                file_created_at,
                duration_seconds,
                bitrate,
                audio_format,
                artwork_mime,
                spotify_uri,
                soundcloud_url,
                spotify_link_skipped_at,
                indexed_at
            from tracks
            order by lower(coalesce(nullif(artist, ''), '')),
                     lower(coalesce(nullif(title, ''), stem)),
                     lower(path)
            """
        ).fetchall()

    return [_track_from_row(row) for row in rows]


def set_track_spotify_uri(
    db_path: Path = DEFAULT_COLLECTION_DB_PATH,
    *,
    path: str,
    spotify_uri: str,
) -> None:
    with sqlite3.connect(db_path) as conn:
        _ensure_schema(conn)
        conn.execute(
            """
            update tracks
            set spotify_uri = ?,
                soundcloud_url = null,
                spotify_link_skipped_at = null
            where path = ?
            """,
            (spotify_uri, path),
        )


def set_track_soundcloud_url(
    db_path: Path = DEFAULT_COLLECTION_DB_PATH,
    *,
    path: str,
    soundcloud_url: str,
) -> None:
    with sqlite3.connect(db_path) as conn:
        _ensure_schema(conn)
        conn.execute(
            """
            update tracks
            set soundcloud_url = ?,
                spotify_uri = null,
                spotify_link_skipped_at = null
            where path = ?
            """,
            (soundcloud_url, path),
        )


def delete_track(
    db_path: Path = DEFAULT_COLLECTION_DB_PATH,
    *,
    path: str,
) -> bool:
    with sqlite3.connect(db_path) as conn:
        _ensure_schema(conn)
        cursor = conn.execute("delete from tracks where path = ?", (path,))
        return cursor.rowcount > 0


def skip_track_spotify_link(
    db_path: Path = DEFAULT_COLLECTION_DB_PATH,
    *,
    path: str,
) -> None:
    with sqlite3.connect(db_path) as conn:
        _ensure_schema(conn)
        conn.execute(
            """
            update tracks
            set spotify_link_skipped_at = ?
            where path = ?
            """,
            (datetime.now(timezone.utc).isoformat(), path),
        )


def refresh_track_metadata(
    db_path: Path = DEFAULT_COLLECTION_DB_PATH,
    *,
    path: str,
) -> bool:
    track_path = Path(path)
    try:
        stat = track_path.stat()
    except OSError:
        delete_track(db_path, path=path)
        return False

    track = read_track_metadata(track_path)
    with sqlite3.connect(db_path) as conn:
        _ensure_schema(conn)
        _upsert_track(conn, track, size=stat.st_size, mtime_ns=stat.st_mtime_ns)

    return True


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
    spotify: str,
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
            spotify=spotify,
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
            select
                path,
                title,
                artist,
                album,
                comment,
                genre,
                release_date,
                file_created_at,
                duration_seconds,
                bitrate,
                audio_format,
                artwork_mime,
                spotify_uri,
                soundcloud_url,
                spotify_link_skipped_at,
                indexed_at
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
    conn.execute("pragma foreign_keys = on")
    conn.execute(
        """
        create table if not exists tracks (
            path text primary key,
            stem text not null,
            title text,
            artist text,
            album text,
            comment text,
            genre text,
            release_date text,
            file_created_at text,
            duration_seconds real,
            bitrate integer,
            audio_format text,
            artwork_mime text,
            artwork_data blob,
            artwork_checked integer not null default 0,
            spotify_uri text,
            soundcloud_url text,
            spotify_link_skipped_at text,
            size integer not null,
            mtime_ns integer not null,
            indexed_at text not null
        )
        """
    )
    _ensure_column(conn, "tracks", "artwork_mime", "text")
    _ensure_column(conn, "tracks", "comment", "text")
    _ensure_column(conn, "tracks", "genre", "text")
    _ensure_column(conn, "tracks", "release_date", "text")
    _ensure_column(conn, "tracks", "file_created_at", "text")
    _ensure_column(conn, "tracks", "artwork_data", "blob")
    _ensure_column(conn, "tracks", "artwork_checked", "integer not null default 0")
    _ensure_column(conn, "tracks", "spotify_uri", "text")
    _ensure_column(conn, "tracks", "soundcloud_url", "text")
    _ensure_column(conn, "tracks", "spotify_link_skipped_at", "text")
    conn.execute("create index if not exists idx_tracks_format on tracks(audio_format)")
    conn.execute("create index if not exists idx_tracks_title on tracks(title)")
    conn.execute("create index if not exists idx_tracks_artist on tracks(artist)")
    conn.execute("create index if not exists idx_tracks_album on tracks(album)")
    conn.execute("create index if not exists idx_tracks_genre on tracks(genre)")
    conn.execute(
        "create index if not exists idx_tracks_release_date on tracks(release_date)"
    )
    conn.execute("create index if not exists idx_tracks_bitrate on tracks(bitrate)")
    conn.execute(
        "create index if not exists idx_tracks_duration on tracks(duration_seconds)"
    )
    conn.execute(
        "create index if not exists idx_tracks_spotify_uri on tracks(spotify_uri)"
    )
    conn.execute(
        "create index if not exists idx_tracks_soundcloud_url on tracks(soundcloud_url)"
    )
    conn.execute(
        "create index if not exists idx_tracks_spotify_skipped "
        "on tracks(spotify_link_skipped_at)"
    )
    conn.execute(
        """
        create table if not exists track_profiles (
            track_path text primary key,
            energy integer check (energy between 1 and 5),
            personal_rating integer check (personal_rating between 1 and 5),
            set_role text check (
                set_role in (
                    'warmup', 'builder', 'peak', 'reset', 'afterhours', 'closer'
                )
            ),
            notes text,
            updated_at text not null,
            foreign key (track_path) references tracks(path) on delete cascade
        )
        """
    )
    conn.execute(
        """
        create table if not exists track_tags (
            track_path text not null,
            category text not null check (
                category in ('groove', 'palette', 'mood', 'structure', 'legacy')
            ),
            value text not null,
            source text not null check (
                source in ('manual', 'rekordbox', 'traktor', 'model')
            ),
            approved integer not null check (approved in (0, 1)),
            confidence real,
            updated_at text not null,
            primary key (track_path, category, value, source),
            foreign key (track_path) references tracks(path) on delete cascade
        )
        """
    )
    conn.execute(
        """
        create table if not exists track_source_metadata (
            track_path text not null,
            source text not null check (source in ('rekordbox', 'traktor')),
            source_track_id text,
            legacy_rating integer check (legacy_rating between 1 and 5),
            genre text,
            comment text,
            comment2 text,
            imported_at text not null,
            primary key (track_path, source),
            foreign key (track_path) references tracks(path) on delete cascade
        )
        """
    )
    conn.execute(
        """
        create table if not exists library_imports (
            id integer primary key autoincrement,
            source_type text not null check (source_type in ('rekordbox', 'traktor')),
            source_file text not null,
            imported_at text not null,
            dry_run integer not null check (dry_run in (0, 1)),
            parsed_count integer not null,
            matched_count integer not null,
            unmatched_count integer not null,
            ambiguous_count integer not null,
            invalid_count integer not null
        )
        """
    )
    conn.execute(
        "create index if not exists idx_track_tags_path on track_tags(track_path)"
    )
    conn.execute(
        "create index if not exists idx_source_metadata_source "
        "on track_source_metadata(source)"
    )


def _load_existing_file_state(conn: sqlite3.Connection) -> dict[str, tuple[int, int]]:
    rows = conn.execute(
        """
        select path, size, mtime_ns
        from tracks
        where artwork_checked = 1
          and file_created_at is not null
        """
    ).fetchall()
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
            comment,
            genre,
            release_date,
            file_created_at,
            duration_seconds,
            bitrate,
            audio_format,
            artwork_mime,
            artwork_data,
            artwork_checked,
            spotify_uri,
            soundcloud_url,
            size,
            mtime_ns,
            indexed_at
        )
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        on conflict(path) do update set
            stem = excluded.stem,
            title = excluded.title,
            artist = excluded.artist,
            album = excluded.album,
            comment = excluded.comment,
            genre = excluded.genre,
            release_date = excluded.release_date,
            file_created_at = excluded.file_created_at,
            duration_seconds = excluded.duration_seconds,
            bitrate = excluded.bitrate,
            audio_format = excluded.audio_format,
            artwork_mime = excluded.artwork_mime,
            artwork_data = excluded.artwork_data,
            artwork_checked = excluded.artwork_checked,
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
            track.comment,
            track.genre,
            track.release_date,
            track.file_created_at,
            track.duration_seconds,
            track.bitrate,
            track.audio_format,
            track.artwork_mime,
            track.artwork_data,
            1,
            track.spotify_uri,
            track.soundcloud_url,
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
    spotify: str,
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
                coalesce(comment, '') || ' ' ||
                coalesce(genre, '') || ' ' ||
                coalesce(release_date, '') || ' ' ||
                coalesce(file_created_at, '') || ' ' ||
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

    if spotify == "unlinked":
        parts.append(
            "spotify_uri is null and soundcloud_url is null "
            "and spotify_link_skipped_at is null"
        )
    elif spotify == "linked":
        parts.append("(spotify_uri is not null or soundcloud_url is not null)")
    elif spotify == "skipped":
        parts.append(
            "spotify_uri is null and soundcloud_url is null "
            "and spotify_link_skipped_at is not null"
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


def _ensure_column(
    conn: sqlite3.Connection,
    table_name: str,
    column_name: str,
    column_definition: str,
) -> None:
    columns = {
        str(row[1])
        for row in conn.execute(f"pragma table_info({table_name})").fetchall()
    }
    if column_name not in columns:
        conn.execute(
            f"alter table {table_name} add column {column_name} {column_definition}"
        )
