import json
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from crate_digger.cli import main
from crate_digger.collection.importers.rekordbox import parse_rekordbox_document
from crate_digger.collection.importers.traktor import parse_traktor_document
from crate_digger.collection.matching import PathMap
from crate_digger.collection.migration import (
    migrate_rekordbox_to_traktor,
    restore_traktor_artwork,
)
from crate_digger.collection.timing import _crc16, _mp3_frame_offset_ms


def _find(parent: ET.Element, path: str) -> ET.Element:
    found = parent.find(path)
    assert found is not None
    return found


def _source(tmp_path):
    audio = tmp_path / "audio"
    audio.mkdir()
    (audio / "Zażółć & cue.wav").write_bytes(b"audio")
    (audio / "voice.wav").write_bytes(b"voice")
    source = tmp_path / "rekordbox.xml"
    source.write_text(
        """<DJ_PLAYLISTS Version="1.0.0">
<COLLECTION Entries="2">
  <TRACK TrackID="101" Name="Zażółć &amp; cue" Artist="Ada" Rating="204"
      Comments=" /* Groovy / House */ " AverageBpm="120" Tonality="8A"
      DateAdded="2026-09-19" TotalTime="240" BitRate="320" Size="2048"
      Location="file://localhost/C:/Music/Za%C5%BC%C3%B3%C5%82%C4%87%20%26%20cue.wav">
    <TEMPO Inizio="0.125" Bpm="120" Metro="4/4" Battito="1" />
    <TEMPO Inizio="20" Bpm="128" Metro="4/4" Battito="3" />
    <POSITION_MARK Name="Memory" Type="0" Start="16" Num="-1" />
    <POSITION_MARK Name="Drop" Type="0" Start="32.5" Num="0" />
    <POSITION_MARK Name="Loop" Type="4" Start="64" End="72" Num="7" />
  </TRACK>
  <TRACK TrackID="102" Name="Voice" Rating="51" Location="file://localhost/C:/Music/voice.wav" />
</COLLECTION>
<PLAYLISTS><NODE Type="0" Name="ROOT"><NODE Type="0" Name="Sets">
  <NODE Type="1" Name="main / peak" Entries="3">
    <TRACK Key="102"/><TRACK Key="101"/><TRACK Key="102"/>
  </NODE>
  <NODE Type="1" Name="Empty" Entries="0" />
</NODE></NODE></PLAYLISTS></DJ_PLAYLISTS>""",
        encoding="utf-8",
    )
    return source, (PathMap("C:/Music", str(audio)),)


def test_standalone_windows_migration_preserves_metadata_and_playlist_order(tmp_path):
    source, maps = _source(tmp_path)
    original = source.read_bytes()
    target = tmp_path / "traktor.nml"
    preview = migrate_rekordbox_to_traktor(source, target, path_maps=maps)
    assert not preview.blocked
    assert preview.source_tracks == 2  # Playlist TRACK references aren't audio entries.
    assert not target.exists()
    result = migrate_rekordbox_to_traktor(source, target, path_maps=maps, dry_run=False)
    assert not result.blocked
    assert source.read_bytes() == original
    root = ET.parse(target).getroot()
    assert root.get("VERSION") == "20"
    entries = root.findall("COLLECTION/ENTRY")
    assert len(entries) == 2
    assert all(entry.get("AUDIO_ID") is None for entry in entries)
    first = entries[0]
    assert _find(first, "LOCATION").attrib == {
        "VOLUME": "C:",
        "DIR": "/:Music/:",
        "FILE": "Zażółć & cue.wav",
    }
    assert _find(first, "INFO").get("RANKING") == "204"
    assert _find(first, "INFO").get("COMMENT") == " /* Groovy / House */ "
    assert _find(first, "INFO").get("IMPORT_DATE") == "2026/09/19"
    assert _find(first, "INFO").get("BITRATE") == "320000"
    assert _find(first, "MUSICAL_KEY").get("VALUE") == "21"
    assert first.get("LOCK") == "1"
    markers = first.findall("CUE_V2")
    grids = [c for c in markers if c.get("TYPE") == "4"]
    assert [float(_find(g, "GRID").attrib["BPM"]) for g in grids] == [120, 128]
    assert [float(g.attrib["START"]) for g in grids] == [125, 20937.5]
    cues = [c for c in markers if c.get("TYPE") != "4"]
    assert [
        (
            c.get("HOTCUE"),
            c.get("TYPE"),
            float(c.attrib["START"]),
            float(c.attrib["LEN"]),
        )
        for c in cues
    ] == [
        ("-1", "0", 16000, 0),
        ("0", "0", 32500, 0),
        ("7", "5", 64000, 8000),
    ]
    for subnodes in root.findall(".//SUBNODES"):
        assert int(subnodes.attrib["COUNT"]) == len(subnodes.findall("NODE"))
    parsed = parse_traktor_document(target)
    assert len(parsed.tracks) == 2  # Playlist ENTRY references aren't audio entries.
    assert parsed.playlists[0].folder_path == ("Rekordbox", "Sets")
    assert parsed.playlists[0].track_paths == (
        "C:/Music/voice.wav",
        "C:/Music/Zażółć & cue.wav",
        "C:/Music/voice.wav",
    )
    assert parsed.playlists[1].track_paths == ()
    assert [g.bpm for g in parsed.tracks[0].beatgrids] == [120, 128]


def test_missing_audio_blocks_apply_and_reports_exact_track(tmp_path, capsys):
    source, maps = _source(tmp_path)
    Path(maps[0].destination, "voice.wav").unlink()
    target = tmp_path / "existing.nml"
    target.write_bytes(b"existing target")
    report = tmp_path / "report.json"
    assert (
        main(
            [
                "library",
                "migrate-rekordbox-to-traktor",
                str(source),
                str(target),
                "--path-map",
                f"{maps[0].source}={maps[0].destination}",
                "--report",
                str(report),
                "--apply",
            ]
        )
        == 2
    )
    assert target.read_bytes() == b"existing target"
    data = json.loads(report.read_text())
    assert data["blocked"]
    missing = [item for item in data["tracks"] if item["error"]]
    assert len(missing) == 1
    assert missing[0]["source_id"] == "102"
    assert missing[0]["error"] == "Audio file not found"
    assert "No output written" in capsys.readouterr().out


def test_repeat_export_is_identical_and_changed_output_is_backed_up(tmp_path):
    source, maps = _source(tmp_path)
    target = tmp_path / "out.nml"
    migrate_rekordbox_to_traktor(source, target, path_maps=maps, dry_run=False)
    original = target.read_bytes()
    repeat = migrate_rekordbox_to_traktor(source, target, path_maps=maps, dry_run=False)
    assert target.read_bytes() == original
    assert repeat.backups == ()
    tree = ET.parse(source)
    _find(tree.getroot(), "COLLECTION/TRACK").set("Rating", "255")
    tree.write(source)
    changed = migrate_rekordbox_to_traktor(
        source, target, path_maps=maps, dry_run=False
    )
    assert len(changed.backups) == 1
    assert Path(changed.backups[0]).read_bytes() == original
    assert (
        _find(ET.parse(target).getroot(), "COLLECTION/ENTRY/INFO").get("RANKING")
        == "255"
    )


def test_copied_audio_uses_explicit_target_paths_and_keeps_originals(tmp_path):
    source, maps = _source(tmp_path)
    target = tmp_path / "out.nml"
    copies = tmp_path / "copies"
    result = migrate_rekordbox_to_traktor(
        source,
        target,
        path_maps=maps,
        copy_audio_to=copies,
        target_path_maps=(PathMap(str(copies), "D:/TraktorMusic"),),
        dry_run=False,
    )
    assert result.audio_files_copied == 2
    assert (copies / "voice.wav").read_bytes() == b"voice"
    assert Path(maps[0].destination, "voice.wav").read_bytes() == b"voice"
    assert all(
        (t.source_path or "").startswith("D:/TraktorMusic/")
        for t in parse_traktor_document(target).tracks
    )


def test_all_native_rekordbox_rating_values(tmp_path):
    source, _ = _source(tmp_path)
    tree = ET.parse(source)
    for raw, stars in [(0, None), (51, 1), (102, 2), (153, 3), (204, 4), (255, 5)]:
        _find(tree.getroot(), "COLLECTION/TRACK").set("Rating", str(raw))
        tree.write(source)
        assert parse_rekordbox_document(source).tracks[0].legacy_rating == stars


def test_migration_retains_existing_traktor_cover_cache_references(tmp_path):
    source, maps = _source(tmp_path)
    reference = tmp_path / "old-traktor.nml"
    reference.write_text(
        """<NML VERSION="20"><COLLECTION ENTRIES="1"><ENTRY>
        <LOCATION VOLUME="C:" DIR="/:Music/:" FILE="Zażółć &amp; cue.wav"/>
        <INFO COVERARTID="033/EXISTINGCOVER" RANKING="51" COMMENT="old comment"/>
        </ENTRY></COLLECTION></NML>""",
        encoding="utf-8",
    )
    target = tmp_path / "out.nml"
    report = migrate_rekordbox_to_traktor(
        source, target, path_maps=maps, traktor_reference=reference, dry_run=False
    )
    assert report.cover_art_references == 1
    info = _find(ET.parse(target).getroot(), "COLLECTION/ENTRY/INFO")
    assert info.get("COVERARTID") == "033/EXISTINGCOVER"
    assert info.get("RANKING") == "204"
    assert info.get("COMMENT") == " /* Groovy / House */ "
    # Re-running without the reference keeps the previously exported artwork.
    migrate_rekordbox_to_traktor(source, target, path_maps=maps, dry_run=False)
    assert (
        _find(ET.parse(target).getroot(), "COLLECTION/ENTRY/INFO").get("COVERARTID")
        == "033/EXISTINGCOVER"
    )


def test_artwork_repair_changes_only_missing_references_and_matches_literal_filenames(
    tmp_path,
):
    root = ET.fromstring(
        """<NML><COLLECTION ENTRIES="2">
    <ENTRY TITLE="Leading space"><LOCATION VOLUME="C:" DIR="/:Music/:" FILE=" song.wav"/>
    <INFO RANKING="204" COMMENT="keep me"/><CUE_V2 START="1000" TYPE="0" HOTCUE="1"/></ENTRY>
    <ENTRY TITLE="No space"><LOCATION VOLUME="C:" DIR="/:Music/:" FILE="song.wav"/>
    <INFO COVERARTID="new/manual"/></ENTRY></COLLECTION><PLAYLISTS/></NML>"""
    )
    before = ET.tostring(root)
    reference = tmp_path / "reference.nml"
    reference.write_text(
        """<NML><COLLECTION>
    <ENTRY><LOCATION VOLUME="C:" DIR="/:Music/:" FILE=" song.wav"/><INFO COVERARTID="old/spaced"/></ENTRY>
    <ENTRY><LOCATION VOLUME="C:" DIR="/:Music/:" FILE="song.wav"/><INFO COVERARTID="old/unspaced"/></ENTRY>
    </COLLECTION></NML>"""
    )
    assert restore_traktor_artwork(root, reference) == 1
    entries = root.findall("COLLECTION/ENTRY")
    assert _find(entries[0], "INFO").attrib.pop("COVERARTID") == "old/spaced"
    assert ET.tostring(root) == before
    saved = tmp_path / "leading-space.nml"
    ET.ElementTree(root).write(saved)
    assert parse_traktor_document(saved).tracks[0].source_path == "C:/Music/ song.wav"


def test_mp3_decoder_offsets_depend_on_header_crc_and_sample_rate():
    assert _crc16(b"123456789") == 0xBB3D  # CRC-16/ARC reference check value.
    frame = bytearray(256)
    frame[36:40] = b"Xing"
    frame[40:44] = (15).to_bytes(4, "big")
    frame[156:160] = b"Lavc"
    assert _mp3_frame_offset_ms(bytes(frame), 36, 44100, 1) == pytest.approx(26.122449)
    assert _mp3_frame_offset_ms(bytes(frame), 36, 48000, 1) == 24
    frame[156:160] = b"LAME"
    assert _mp3_frame_offset_ms(bytes(frame), 36, 44100, 1) > 0  # Invalid CRC.
    frame[190:192] = _crc16(bytes(frame[:190])).to_bytes(2, "big")
    assert _mp3_frame_offset_ms(bytes(frame), 36, 44100, 1) == 0
    frame[36:40] = b"none"
    assert _mp3_frame_offset_ms(bytes(frame), 36, 44100, 1) == 0


def test_decoder_offset_applies_to_cues_and_grids_but_not_loop_length(
    tmp_path, monkeypatch
):
    source, maps = _source(tmp_path)
    monkeypatch.setattr(
        "crate_digger.collection.migration.rekordbox_to_traktor_offset_ms",
        lambda path: 26.0,
    )
    target = tmp_path / "out.nml"
    report = migrate_rekordbox_to_traktor(source, target, path_maps=maps, dry_run=False)
    assert report.tracks[0].timing_offset_ms == 26
    track = parse_traktor_document(target).tracks[0]
    assert track.cues[0].start_ms == 16026
    assert track.cues[2].start_ms == 64026
    assert track.cues[2].length_ms == 8000
    assert track.beatgrids[0].start_ms == 151
