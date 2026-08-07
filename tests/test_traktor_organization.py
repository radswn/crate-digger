import sqlite3
import wave
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from crate_digger.collection.index import _delete_missing_tracks, _ensure_schema
from crate_digger.collection.matching import PathMap
from crate_digger.collection.traktor_organization import (
    _traktor_process,
    apply,
    classify,
    import_nml,
    preview,
    relink,
    review,
    set_category,
)


def _nml(path: Path, root: Path, *, moved: bool = False) -> None:
    song = "moved.mp3" if moved else "song.mp3"
    path.write_text(
        f"""<?xml version="1.0" encoding="UTF-8"?>
<NML VERSION="19" EXTRA="keep">
  <COLLECTION ENTRIES="5">
    <ENTRY TITLE="Song" ARTIST="Ada" AUDIO_ID="stable">
      <LOCATION VOLUME="C:" DIR="/:Download/:" FILE="{song}" />
      <INFO GENRE="House" RATING="hello [CD_HIDE] [CD_TEST] world" RANKING="255" />
      <CUE_V2 NAME="cue" START="12" /><UNKNOWN VALUE="keep" /><!--keep note-->
    </ENTRY>
    <ENTRY TITLE="Vocal" ARTIST="Bea"><LOCATION VOLUME="C:" DIR="/:Acapellas/:" FILE="vocal.wav" /><INFO RATING="private" /></ENTRY>
    <ENTRY TITLE="Recording" ARTIST="Cid"><LOCATION VOLUME="C:" DIR="/:Recordings/:" FILE="short.wav" /><INFO /></ENTRY>
    <ENTRY TITLE="Missing" ARTIST="Dee"><LOCATION VOLUME="C:" DIR="/:Download/:" FILE="missing.mp3" /><INFO /></ENTRY>
    <ENTRY TITLE="Odd" ARTIST="Eve"><LOCATION VOLUME="C:" DIR="/:Unknown/:" FILE="odd.mp3" /><INFO /></ENTRY>
  </COLLECTION>
  <PLAYLISTS><NODE TYPE="FOLDER" NAME="$ROOT"><SUBNODES COUNT="2">
    <NODE TYPE="PLAYLIST" NAME="Mine"><PLAYLIST UUID="user-uuid" ENTRIES="1" TYPE="LIST"><ENTRY><PRIMARYKEY TYPE="TRACK" KEY="C:/:Download/:{song}" /></ENTRY></PLAYLIST></NODE>
    <NODE TYPE="FOLDER" NAME="Crate Digger"><SUBNODES COUNT="1"><NODE TYPE="PLAYLIST" NAME="00 Main Tracks"><PLAYLIST UUID="stable-main" ENTRIES="0" TYPE="LIST" /></NODE></SUBNODES></NODE>
  </SUBNODES></NODE></PLAYLISTS>
</NML>""",
        encoding="utf-8",
    )


def _setup(tmp_path: Path):
    source = tmp_path / "collection.nml"
    audio = tmp_path / "audio"
    for folder in ("Download", "Acapellas", "Recordings", "Unknown"):
        (audio / folder).mkdir(parents=True)
    (audio / "Download" / "song.mp3").write_bytes(b"media")
    with wave.open(str(audio / "Recordings" / "short.wav"), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(1)
        wav.setframerate(8000)
        wav.writeframes(b"\x80" * 8000)
    _nml(source, audio)
    db = tmp_path / "collection.sqlite3"
    with sqlite3.connect(db) as conn:
        _ensure_schema(conn)
        conn.execute(
            """insert into tracks (path,stem,size,mtime_ns,indexed_at)
            values (?,?,?,?,?)""",
            (str(audio / "Download" / "song.mp3"), "song", 5, 1, "now"),
        )
    maps = (PathMap("C:", str(audio)),)
    return source, db, maps


def test_import_review_preview_apply_and_idempotence(tmp_path: Path):
    source, db, maps = _setup(tmp_path)
    before = source.read_bytes()
    result = import_nml(source, db, maps)
    assert result["total"] == 5
    assert result["linked"] == 1
    assert result["missing_media"] == 3
    assert result["unclassified"] == 1
    assert Path(result["database_backup"]).is_file()
    rows = review(db)
    assert {row["category"] for row in rows} >= {
        "DOWNLOAD",
        "ACAPELLAS",
        "REVIEW",
        None,
    }
    assert (
        next(row for row in rows if row["title"] == "Recording")["category"] == "REVIEW"
    )
    with pytest.raises(ValueError, match="Unclassified"):
        preview(source, db, tmp_path / "preview")
    odd = next(row for row in rows if row["title"] == "Odd")
    set_category(db, odd["id"], "LISTENING")
    database_before = db.read_bytes()
    report = preview(source, db, tmp_path / "preview")
    assert source.read_bytes() == before
    assert db.read_bytes() == database_before
    assert report["playlist_counts"]["00 Main Tracks"] == 2
    assert report["playlist_counts"]["01 DJ Downloads"] == 2
    assert report["playlist_counts"]["Short Recordings - Review"] == 1
    assert (
        report["entries"][0]["comment2_after"]
        == "hello   world [CD_MAIN] [CD_DOWNLOAD]"
    )
    written = apply(Path(report["report"]), tmp_path / "backups")
    assert written["changed"] is True
    assert Path(written["backup"]).read_bytes() == before
    root = ET.parse(source).getroot()
    song = root.find("COLLECTION/ENTRY")
    assert song is not None
    info = song.find("INFO")
    cue = song.find("CUE_V2")
    unknown = song.find("UNKNOWN")
    assert info is not None and info.get("RANKING") == "255"
    assert cue is not None and cue.get("NAME") == "cue"
    assert unknown is not None and unknown.get("VALUE") == "keep"
    assert b"<!--keep note-->" in source.read_bytes()
    assert root.find("PLAYLISTS/NODE/SUBNODES/NODE[@NAME='Mine']") is not None
    main_playlist = root.find(".//NODE[@NAME='00 Main Tracks']/PLAYLIST")
    assert main_playlist is not None and main_playlist.get("UUID") == "stable-main"
    assert apply(Path(report["report"]))["changed"] is False
    with sqlite3.connect(db) as conn:
        assert (
            conn.execute("select genre from canonical_track_metadata").fetchone()[0]
            == "House"
        )
        conn.execute("pragma foreign_keys=on")
        _delete_missing_tracks(conn, set())
        assert conn.execute("select count(*) from traktor_entries").fetchone()[0] == 5


def test_changed_source_and_manual_override_reimport(tmp_path: Path):
    source, db, maps = _setup(tmp_path)
    import_nml(source, db, maps)
    rows = review(db)
    odd = next(row for row in rows if row["title"] == "Odd")
    set_category(db, odd["id"], "LISTENING")
    report = preview(source, db, tmp_path / "preview")
    source.write_bytes(source.read_bytes().replace(b'EXTRA="keep"', b'EXTRA="edited"'))
    with pytest.raises(ValueError, match="changed after preview"):
        apply(Path(report["report"]))
    import_nml(source, db, maps)
    assert (
        next(row for row in review(db) if row["title"] == "Odd")["category"]
        == "LISTENING"
    )


def test_path_move_without_audio_id_requires_explicit_relink(tmp_path: Path):
    source, db, maps = _setup(tmp_path)
    import_nml(source, db, maps)
    vocal = next(row for row in review(db) if row["title"] == "Vocal")
    set_category(db, vocal["id"], "EDITS")
    source.write_bytes(
        source.read_bytes().replace(b'FILE="vocal.wav"', b'FILE="vocal-new.wav"')
    )
    import_nml(source, db, maps)
    new = next(row for row in review(db) if row["title"] == "Vocal")
    assert new["id"] != vocal["id"]
    assert new["category"] == "ACAPELLAS"
    relink(db, vocal["id"], new["id"], "Confirmed same recording in Traktor")
    import_nml(source, db, maps)
    corrected = next(row for row in review(db) if row["title"] == "Vocal")
    assert corrected["category"] == "EDITS"
    assert corrected["relink_from_id"] == vocal["id"]
    assert corrected["relink_evidence"] == "Confirmed same recording in Traktor"


def test_stable_audio_id_retains_manual_category_on_move(tmp_path: Path):
    source, db, maps = _setup(tmp_path)
    import_nml(source, db, maps)
    song = next(row for row in review(db) if row["title"] == "Song")
    set_category(db, song["id"], "EDITS")
    source.write_bytes(
        source.read_bytes().replace(b'FILE="song.mp3"', b'FILE="moved.mp3"')
    )
    import_nml(source, db, maps)
    moved = next(row for row in review(db) if row["title"] == "Song")
    assert moved["id"] == song["id"]
    assert moved["category"] == "EDITS"


def test_preview_and_apply_reject_changed_database_or_running_traktor(
    tmp_path: Path, monkeypatch
):
    source, db, maps = _setup(tmp_path)
    import_nml(source, db, maps)
    odd = next(row for row in review(db) if row["title"] == "Odd")
    set_category(db, odd["id"], "LISTENING")
    report = preview(source, db, tmp_path / "preview")
    set_category(db, odd["id"], "TEST")
    with pytest.raises(ValueError, match="Categories changed"):
        apply(Path(report["report"]))
    report = preview(source, db, tmp_path / "preview")
    monkeypatch.setattr(
        "crate_digger.collection.traktor_organization._traktor_running", lambda: True
    )
    with pytest.raises(ValueError, match="Traktor is running"):
        apply(Path(report["report"]))


def test_unreadable_recording_is_review_category(tmp_path: Path):
    source, db, maps = _setup(tmp_path)
    (tmp_path / "audio" / "Recordings" / "short.wav").unlink()
    import_nml(source, db, maps)
    recording = next(row for row in review(db) if row["title"] == "Recording")
    assert recording["category"] == "REVIEW"
    assert "Recording duration unavailable" in recording["rule_reason"]
    assert recording["review_state"] == "review"


def test_apply_preserves_external_change_detected_before_replace(
    tmp_path: Path, monkeypatch
):
    source, db, maps = _setup(tmp_path)
    import_nml(source, db, maps)
    odd = next(row for row in review(db) if row["title"] == "Odd")
    set_category(db, odd["id"], "LISTENING")
    report = preview(source, db, tmp_path / "preview")
    original = source.read_bytes()
    external = original.replace(b'EXTRA="keep"', b'EXTRA="external"')
    calls = 0

    def change_before_replace() -> bool:
        nonlocal calls
        calls += 1
        if calls == 2:
            source.write_bytes(external)
        return False

    monkeypatch.setattr(
        "crate_digger.collection.traktor_organization._traktor_running",
        change_before_replace,
    )
    with pytest.raises(ValueError, match="Source changed"):
        apply(Path(report["report"]), tmp_path / "backups")
    assert source.read_bytes() == external


def test_process_detection_ignores_collection_path_argument():
    assert not _traktor_process(
        "python",
        ["python", "-m", "crate_digger.cli", "/music/Traktor 4.5.0/collection.nml"],
    )
    assert _traktor_process("Traktor.exe", ["C:\\Apps\\Traktor.exe"])
    assert _traktor_process("wine", ["wine", "C:\\Apps\\Traktor Pro 4.exe"])


@pytest.mark.parametrize(
    ("directory", "filename", "expected"),
    [
        ("/:Traktor/:Stems/:", "track.stem.mp4", "STEMS"),
        ("/:Factory Sounds/:", "demo.wav", "DEMOS"),
        ("/:Sampler/:", "kick.wav", "FX"),
        ("/:Ableton Projects/:", "asset.wav", "PRODUCTION"),
        ("/:Edits/:", "song.mp3", "EDITS"),
        ("/:Test/:", "song.mp3", "TEST"),
        ("/:Acapellas/:", "vocal.wav", "ACAPELLAS"),
        ("/:Download/:", "song.mp3", "DOWNLOAD"),
        ("/:Spoti Local/:", "song.mp3", "LISTENING"),
    ],
)
def test_legacy_folder_categories(directory: str, filename: str, expected: str):
    entry = ET.Element("ENTRY", TITLE="Contains edit word")
    ET.SubElement(entry, "LOCATION", DIR=directory, FILE=filename, VOLUME="C:")
    assert classify(entry, None)[0] == expected
