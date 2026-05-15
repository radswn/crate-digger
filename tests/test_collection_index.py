import sqlite3

from crate_digger.collection.index import (
    _ensure_schema,
    get_track_artwork,
    get_track_for_spotify_linking,
    query_tracks,
    refresh_collection_index,
    set_track_spotify_uri,
    skip_track_spotify_link,
)


def test_refresh_collection_index_indexes_and_reuses_unchanged_files(tmp_path):
    music_dir = tmp_path / "music"
    music_dir.mkdir()
    track = music_dir / "Artist - Title.mp3"
    track.write_bytes(b"not real audio")
    db_path = tmp_path / "collection.sqlite3"

    first = refresh_collection_index([music_dir], db_path=db_path)
    second = refresh_collection_index([music_dir], db_path=db_path)
    result = query_tracks(
        db_path,
        q="artist",
        audio_format="",
        metadata="all",
        sort="title",
        direction="asc",
        page=1,
        page_size=25,
    )

    assert first.discovered_files == 1
    assert first.indexed_files == 1
    assert second.indexed_files == 0
    assert result.total_count == 1
    assert result.tracks[0].display_title == "Artist - Title"


def test_refresh_collection_index_deletes_missing_files(tmp_path):
    music_dir = tmp_path / "music"
    music_dir.mkdir()
    track = music_dir / "gone.mp3"
    track.write_bytes(b"not real audio")
    db_path = tmp_path / "collection.sqlite3"

    refresh_collection_index([music_dir], db_path=db_path)
    track.unlink()
    stats = refresh_collection_index([music_dir], db_path=db_path)
    result = query_tracks(
        db_path,
        q="",
        audio_format="",
        metadata="all",
        sort="title",
        direction="asc",
        page=1,
        page_size=25,
    )

    assert stats.deleted_files == 1
    assert result.total_count == 0


def test_get_track_artwork_returns_indexed_cover_blob(tmp_path):
    db_path = tmp_path / "collection.sqlite3"
    track_path = "/music/covered.mp3"
    with sqlite3.connect(db_path) as conn:
        _ensure_schema(conn)
        conn.execute(
            """
            insert into tracks (
                path,
                stem,
                title,
                audio_format,
                artwork_mime,
                artwork_data,
                artwork_checked,
                size,
                mtime_ns,
                indexed_at
            )
            values (?, 'covered', 'Covered', 'MP3', 'image/jpeg', ?, 1, 1, 1, '2026-01-01T00:00:00+00:00')
            """,
            (track_path, b"image-bytes"),
        )

    assert get_track_artwork(db_path, path=track_path) == ("image/jpeg", b"image-bytes")


def test_spotify_link_queue_saves_and_skips_tracks(tmp_path):
    music_dir = tmp_path / "music"
    music_dir.mkdir()
    first = music_dir / "first.mp3"
    second = music_dir / "second.mp3"
    first.write_bytes(b"not real audio")
    second.write_bytes(b"not real audio")
    db_path = tmp_path / "collection.sqlite3"
    refresh_collection_index([music_dir], db_path=db_path)

    queued = get_track_for_spotify_linking(db_path)
    assert queued is not None

    set_track_spotify_uri(
        db_path,
        path=str(queued.path),
        spotify_uri="spotify:track:linked",
    )
    next_queued = get_track_for_spotify_linking(db_path)
    assert next_queued is not None
    assert next_queued.path != queued.path

    skip_track_spotify_link(db_path, path=str(next_queued.path))

    linked = get_track_for_spotify_linking(db_path, path=str(queued.path))
    assert linked is not None
    assert get_track_for_spotify_linking(db_path) is None
    assert linked.spotify_uri == "spotify:track:linked"
