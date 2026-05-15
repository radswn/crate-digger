from crate_digger.collection.scanner import discover_audio_files, scan_collection


def test_discover_audio_files_recurses_supported_extensions(tmp_path):
    music_dir = tmp_path / "music"
    nested_dir = music_dir / "nested"
    nested_dir.mkdir(parents=True)
    mp3 = music_dir / "track.mp3"
    flac = nested_dir / "track.flac"
    text = nested_dir / "notes.txt"
    mp3.write_bytes(b"not real audio")
    flac.write_bytes(b"not real audio")
    text.write_text("ignore me")

    assert set(discover_audio_files([music_dir])) == {mp3, flac}


def test_scan_collection_falls_back_to_path_metadata(tmp_path):
    track = tmp_path / "Artist - Title.mp3"
    track.write_bytes(b"not real audio")

    results = scan_collection([tmp_path])

    assert len(results) == 1
    assert results[0].path == track
    assert results[0].display_title == "Artist - Title"
    assert results[0].display_artist == "Unknown artist"
    assert results[0].audio_format == "MP3"
