import sqlite3

from crate_digger.collection.index import (
    _ensure_schema,
    get_track_artwork,
    get_track_for_spotify_linking,
    list_tracks_missing_spotify_artwork,
    list_tracks_for_volume_normalization,
    list_tracks_pending_spotify_linking,
    query_tracks,
    refresh_collection_index,
    refresh_track_metadata,
    set_track_soundcloud_url,
    set_track_spotify_uri,
    skip_track_spotify_link,
)
from crate_digger.collection.models import LocalTrack


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
        spotify="all",
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
        spotify="all",
        sort="title",
        direction="asc",
        page=1,
        page_size=25,
    )

    assert stats.deleted_files == 1
    assert result.total_count == 0


def test_refresh_track_metadata_deletes_missing_track(tmp_path):
    db_path = tmp_path / "collection.sqlite3"
    missing = tmp_path / "missing.mp3"
    with sqlite3.connect(db_path) as conn:
        _ensure_schema(conn)
        conn.execute(
            """
            insert into tracks (
                path,
                stem,
                title,
                audio_format,
                artwork_checked,
                size,
                mtime_ns,
                indexed_at
            )
            values (?, 'missing', 'Missing', 'MP3', 1, 1, 1, '2026-01-01T00:00:00+00:00')
            """,
            (str(missing),),
        )

    assert not refresh_track_metadata(db_path, path=str(missing))
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "select path from tracks where path = ?",
            (str(missing),),
        ).fetchone()

    assert row is None


def test_refresh_collection_index_persists_extended_metadata(tmp_path, monkeypatch):
    music_dir = tmp_path / "music"
    music_dir.mkdir()
    track = music_dir / "tagged.flac"
    track.write_bytes(b"not real audio")
    db_path = tmp_path / "collection.sqlite3"

    def fake_read_track_metadata(path):
        return LocalTrack(
            path=path,
            title="After Coma",
            artist="Omon Breaker",
            album="Standard Deviation 2",
            comment="-=TechnoRulez=-",
            genre="Techno",
            release_date="2024-03-01",
            file_created_at="2026-05-15T10:00:00+00:00",
            duration_seconds=123.4,
            bitrate=900000,
            audio_format="FLAC",
        )

    monkeypatch.setattr(
        "crate_digger.collection.index.read_track_metadata",
        fake_read_track_metadata,
    )

    refresh_collection_index([music_dir], db_path=db_path)
    result = query_tracks(
        db_path,
        q="technorulez",
        audio_format="",
        metadata="all",
        spotify="all",
        sort="genre",
        direction="asc",
        page=1,
        page_size=25,
    )

    assert result.filtered_count == 1
    assert result.tracks[0].comment == "-=TechnoRulez=-"
    assert result.tracks[0].genre == "Techno"
    assert result.tracks[0].release_date == "2024-03-01"
    assert result.tracks[0].file_created_at == "2026-05-15T10:00:00+00:00"


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

    set_track_soundcloud_url(
        db_path,
        path=str(queued.path),
        soundcloud_url="https://soundcloud.com/ada/linked",
    )
    soundcloud_linked = get_track_for_spotify_linking(db_path, path=str(queued.path))
    assert soundcloud_linked is not None
    assert soundcloud_linked.spotify_uri is None
    assert soundcloud_linked.soundcloud_url == "https://soundcloud.com/ada/linked"


def test_list_tracks_missing_spotify_artwork_returns_linked_blank_tracks(tmp_path):
    db_path = tmp_path / "collection.sqlite3"
    with sqlite3.connect(db_path) as conn:
        _ensure_schema(conn)
        for name, spotify_uri, artwork_mime in [
            ("blank", "spotify:track:blank", None),
            ("covered", "spotify:track:covered", "image/jpeg"),
            ("unlinked", None, None),
        ]:
            conn.execute(
                """
                insert into tracks (
                    path,
                    stem,
                    title,
                    artist,
                    audio_format,
                    spotify_uri,
                    artwork_mime,
                    artwork_checked,
                    size,
                    mtime_ns,
                    indexed_at
                )
                values (?, ?, ?, 'Ada', 'MP3', ?, ?, 1, 1, 1, '2026-01-01T00:00:00+00:00')
                """,
                (f"/music/{name}.mp3", name, name.title(), spotify_uri, artwork_mime),
            )

    tracks = list_tracks_missing_spotify_artwork(db_path)

    assert [str(track.path) for track in tracks] == ["/music/blank.mp3"]
    assert tracks[0].spotify_uri == "spotify:track:blank"


def test_list_tracks_pending_spotify_linking_returns_unlinked_unskipped_tracks(
    tmp_path,
):
    db_path = tmp_path / "collection.sqlite3"
    with sqlite3.connect(db_path) as conn:
        _ensure_schema(conn)
        for name, spotify_uri, soundcloud_url, skipped_at in [
            ("pending", None, None, None),
            ("linked", "spotify:track:linked", None, None),
            ("soundcloud", None, "https://soundcloud.com/ada/track", None),
            ("skipped", None, None, "2026-01-01T00:00:00+00:00"),
        ]:
            conn.execute(
                """
                insert into tracks (
                    path,
                    stem,
                    title,
                    artist,
                    audio_format,
                    spotify_uri,
                    soundcloud_url,
                    spotify_link_skipped_at,
                    artwork_checked,
                    size,
                    mtime_ns,
                    indexed_at
                )
                values (?, ?, ?, 'Ada', 'MP3', ?, ?, ?, 1, 1, 1, '2026-01-01T00:00:00+00:00')
                """,
                (
                    f"/music/{name}.mp3",
                    name,
                    name.title(),
                    spotify_uri,
                    soundcloud_url,
                    skipped_at,
                ),
            )

    tracks = list_tracks_pending_spotify_linking(db_path)

    assert [str(track.path) for track in tracks] == ["/music/pending.mp3"]


def test_list_tracks_for_volume_normalization_returns_all_indexed_tracks(tmp_path):
    db_path = tmp_path / "collection.sqlite3"
    with sqlite3.connect(db_path) as conn:
        _ensure_schema(conn)
        for name in ["bravo", "alpha"]:
            conn.execute(
                """
                insert into tracks (
                    path,
                    stem,
                    title,
                    artist,
                    audio_format,
                    artwork_checked,
                    size,
                    mtime_ns,
                    indexed_at
                )
                values (?, ?, ?, 'Ada', 'MP3', 1, 1, 1, '2026-01-01T00:00:00+00:00')
                """,
                (f"/music/{name}.mp3", name, name.title()),
            )

    tracks = list_tracks_for_volume_normalization(db_path)

    assert [str(track.path) for track in tracks] == [
        "/music/alpha.mp3",
        "/music/bravo.mp3",
    ]


def test_query_tracks_filters_by_spotify_status(tmp_path):
    db_path = tmp_path / "collection.sqlite3"
    with sqlite3.connect(db_path) as conn:
        _ensure_schema(conn)
        for name, spotify_uri, soundcloud_url, skipped_at in [
            ("unlinked", None, None, None),
            ("linked", "spotify:track:linked", None, None),
            ("soundcloud", None, "https://soundcloud.com/ada/track", None),
            ("skipped", None, None, "2026-01-01T00:00:00+00:00"),
        ]:
            conn.execute(
                """
                insert into tracks (
                    path,
                    stem,
                    title,
                    audio_format,
                    spotify_uri,
                    soundcloud_url,
                    spotify_link_skipped_at,
                    artwork_checked,
                    size,
                    mtime_ns,
                    indexed_at
                )
                values (?, ?, ?, 'MP3', ?, ?, ?, 1, 1, 1, '2026-01-01T00:00:00+00:00')
                """,
                (
                    f"/music/{name}.mp3",
                    name,
                    name.title(),
                    spotify_uri,
                    soundcloud_url,
                    skipped_at,
                ),
            )

    unlinked = query_tracks(
        db_path,
        q="",
        audio_format="",
        metadata="all",
        spotify="unlinked",
        sort="title",
        direction="asc",
        page=1,
        page_size=10,
    )
    linked = query_tracks(
        db_path,
        q="",
        audio_format="",
        metadata="all",
        spotify="linked",
        sort="title",
        direction="asc",
        page=1,
        page_size=10,
    )
    skipped = query_tracks(
        db_path,
        q="",
        audio_format="",
        metadata="all",
        spotify="skipped",
        sort="title",
        direction="asc",
        page=1,
        page_size=10,
    )

    assert [track.path.name for track in unlinked.tracks] == ["unlinked.mp3"]
    assert [track.path.name for track in linked.tracks] == [
        "linked.mp3",
        "soundcloud.mp3",
    ]
    assert [track.path.name for track in skipped.tracks] == ["skipped.mp3"]
    assert skipped.tracks[0].spotify_link_skipped_at == "2026-01-01T00:00:00+00:00"
