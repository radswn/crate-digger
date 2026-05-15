from crate_digger.collection.index import query_tracks, refresh_collection_index


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
