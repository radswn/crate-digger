from crate_digger.collection.scanner import (
    discover_audio_files,
    overwrite_embedded_artwork,
    read_track_metadata,
    scan_collection,
)


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
    assert results[0].file_created_at is not None


def test_read_track_metadata_reads_extended_tags(tmp_path, monkeypatch):
    track = tmp_path / "tagged.flac"
    track.write_bytes(b"not real audio")

    class Info:
        length = 123.4
        bitrate = 900000

    class Audio:
        tags = {
            "title": ["After Coma"],
            "artist": ["Omon Breaker"],
            "album": ["Standard Deviation 2"],
            "comment": ["-=TechnoRulez=-"],
            "genre": ["Techno"],
            "date": ["2024-03-01"],
        }
        info = Info()

    def fake_file(path, easy=True):
        return Audio() if easy else None

    monkeypatch.setattr("crate_digger.collection.scanner.File", fake_file)

    result = read_track_metadata(track)

    assert result.title == "After Coma"
    assert result.artist == "Omon Breaker"
    assert result.album == "Standard Deviation 2"
    assert result.comment == "-=TechnoRulez=-"
    assert result.genre == "Techno"
    assert result.release_date == "2024-03-01"
    assert result.file_created_at is not None


def test_overwrite_embedded_artwork_updates_flac_cover(tmp_path, monkeypatch):
    track = tmp_path / "track.flac"
    track.write_bytes(b"not real audio")
    saved = {}

    class Picture:
        type = 0
        mime = ""
        desc = ""
        data = b""

    class Audio:
        def clear_pictures(self):
            saved["cleared"] = True

        def add_picture(self, picture):
            saved["picture"] = picture

        def save(self):
            saved["saved"] = True

    monkeypatch.setattr("crate_digger.collection.scanner.FLAC", lambda path: Audio())
    monkeypatch.setattr("crate_digger.collection.scanner.Picture", Picture)

    assert overwrite_embedded_artwork(track, mime="image/jpeg", data=b"cover")
    assert saved["cleared"] is True
    assert saved["saved"] is True
    assert saved["picture"].type == 3
    assert saved["picture"].mime == "image/jpeg"
    assert saved["picture"].data == b"cover"


def test_overwrite_embedded_artwork_updates_wav_cover(tmp_path, monkeypatch):
    track = tmp_path / "track.wav"
    track.write_bytes(b"not real audio")
    saved = {}

    class Tags:
        def delall(self, key):
            saved["deleted"] = key

        def add(self, frame):
            saved["frame"] = frame

    class Audio:
        tags = None

        def add_tags(self):
            saved["added_tags"] = True
            self.tags = Tags()

        def save(self):
            saved["saved"] = True

    monkeypatch.setattr("crate_digger.collection.scanner.WAVE", lambda path: Audio())

    assert overwrite_embedded_artwork(track, mime="image/png", data=b"cover")
    assert saved["added_tags"] is True
    assert saved["deleted"] == "APIC"
    assert saved["saved"] is True
    assert saved["frame"].mime == "image/png"
    assert saved["frame"].data == b"cover"


def test_overwrite_embedded_artwork_updates_aiff_cover(tmp_path, monkeypatch):
    track = tmp_path / "track.aiff"
    track.write_bytes(b"not real audio")
    saved = {}

    class Tags:
        def delall(self, key):
            saved["deleted"] = key

        def add(self, frame):
            saved["frame"] = frame

    class Audio:
        tags = None

        def add_tags(self):
            saved["added_tags"] = True
            self.tags = Tags()

        def save(self):
            saved["saved"] = True

    monkeypatch.setattr("crate_digger.collection.scanner.AIFF", lambda path: Audio())

    assert overwrite_embedded_artwork(track, mime="image/jpeg", data=b"cover")
    assert saved["added_tags"] is True
    assert saved["deleted"] == "APIC"
    assert saved["saved"] is True
    assert saved["frame"].mime == "image/jpeg"
    assert saved["frame"].data == b"cover"


def test_overwrite_embedded_artwork_ignores_unsupported_format(tmp_path):
    track = tmp_path / "track.wav"
    track.write_bytes(b"not real audio")

    assert not overwrite_embedded_artwork(track, mime="image/jpeg", data=b"cover")
