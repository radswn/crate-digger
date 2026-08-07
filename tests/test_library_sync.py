import sqlite3
from pathlib import Path
from urllib.parse import quote

from crate_digger.cli import main
from crate_digger.collection.exporters import decode_profile_marker
from crate_digger.collection.importers.rekordbox import parse_rekordbox_document
from crate_digger.collection.importers.traktor import parse_traktor_document
from crate_digger.collection.index import _ensure_schema
from crate_digger.collection.library_sync import (
    list_sync_conflicts,
    resolve_sync_conflict,
    sync_libraries,
    watch_libraries,
)
from crate_digger.collection.profiles import add_tag, upsert_manual_profile


LOCAL_PATH = "C:/Music/Night Drive.mp3"


def _seed_track(db_path: Path, local_path: str = LOCAL_PATH) -> None:
    with sqlite3.connect(db_path) as conn:
        _ensure_schema(conn)
        conn.execute(
            """
            insert into tracks (
                path, stem, title, artist, album, audio_format, artwork_checked,
                size, mtime_ns, indexed_at
            ) values (?, 'Night Drive', 'Night Drive', 'Ada', 'Night Work',
                      'MP3', 1, 1, 1, '2026-01-01T00:00:00+00:00')
            """,
            (local_path,),
        )


def _write_rekordbox(
    path: Path, *, rating: int = 4, local_path: str = LOCAL_PATH
) -> None:
    location = (
        "file://localhost"
        + ("/" if len(local_path) >= 2 and local_path[1] == ":" else "")
        + quote(local_path, safe="/:")
    )
    path.write_text(
        f"""<?xml version="1.0" encoding="UTF-8"?>
<DJ_PLAYLISTS Version="1.0.0">
  <COLLECTION Entries="1">
    <TRACK TrackID="101" Name="Night Drive" Artist="Ada" Album="Night Work"
      Genre="Techno" Label="Sync Records" AverageBpm="126.5" Tonality="8A"
      Colour="0xFF0000" PlayCount="12" DateAdded="2026-01-02"
      Location="{location}"
      Comments="trusted /* Tech */" Rating="{rating}">
      <TEMPO Inizio="0.125" Bpm="126.5" Metro="4/4" Battito="1" />
      <POSITION_MARK Name="Drop" Type="0" Start="32.5" Num="1" />
      <POSITION_MARK Name="Loop" Type="4" Start="64" End="72" Num="2" />
    </TRACK>
  </COLLECTION>
  <PLAYLISTS><NODE Type="0" Name="ROOT"><NODE Type="0" Name="Sets">
    <NODE Type="1" Name="Friday" Entries="1"><TRACK Key="101" /></NODE>
  </NODE></NODE></PLAYLISTS>
</DJ_PLAYLISTS>
""",
        encoding="utf-8",
    )


def _write_traktor(path: Path, *, rating: int = 5) -> None:
    path.write_text(
        f"""<?xml version="1.0" encoding="UTF-8"?>
<NML VERSION="19"><COLLECTION ENTRIES="1">
  <ENTRY TITLE="Night Drive" ARTIST="Ada" AUDIO_ID="stable-traktor">
    <LOCATION DIR="/:Music/:" FILE="Night Drive.mp3" VOLUME="C:" />
    <ALBUM TITLE="Night Work" />
    <INFO GENRE="Techno" LABEL="Sync Records" COMMENT="trusted /* Tech */"
      RANKING="{rating * 51}" PLAYCOUNT="12" IMPORT_DATE="2026-01-02"
      KEY="8A" COLOR="0xFF0000" />
    <TEMPO BPM="126.5" />
    <CUE_V2 NAME="Grid" TYPE="4" START="125" LEN="0" HOTCUE="-1" />
    <CUE_V2 NAME="Drop" TYPE="0" START="32500" LEN="0" HOTCUE="1" />
    <CUE_V2 NAME="Loop" TYPE="5" START="64000" LEN="8000" HOTCUE="2" />
  </ENTRY>
</COLLECTION><PLAYLISTS><NODE TYPE="FOLDER" NAME="$ROOT"><SUBNODES COUNT="1">
  <NODE TYPE="FOLDER" NAME="Sets"><SUBNODES COUNT="1">
    <NODE TYPE="PLAYLIST" NAME="Friday"><PLAYLIST ENTRIES="1" TYPE="LIST">
      <ENTRY><PRIMARYKEY TYPE="TRACK" KEY="C:/:Music/:Night Drive.mp3" /></ENTRY>
    </PLAYLIST></NODE>
  </SUBNODES></NODE>
</SUBNODES></NODE></PLAYLISTS></NML>
""",
        encoding="utf-8",
    )


def test_extended_parsers_read_cues_grids_and_playlists(tmp_path):
    rekordbox = tmp_path / "collection.xml"
    traktor = tmp_path / "collection.nml"
    _write_rekordbox(rekordbox)
    _write_traktor(traktor)

    rb = parse_rekordbox_document(rekordbox)
    tr = parse_traktor_document(traktor)

    assert rb.tracks[0].bpm == tr.tracks[0].bpm == 126.5
    assert rb.tracks[0].musical_key == tr.tracks[0].musical_key == "8A"
    assert rb.tracks[0].beatgrids[0].start_ms == 125
    assert tr.tracks[0].beatgrids[0].start_ms == 125
    assert [cue.kind for cue in rb.tracks[0].cues] == ["cue", "loop"]
    assert [cue.kind for cue in tr.tracks[0].cues] == ["cue", "loop"]
    assert rb.playlists[0].folder_path == tr.playlists[0].folder_path == ("Sets",)
    assert rb.playlists[0].name == tr.playlists[0].name == "Friday"


def test_migration_dry_run_then_apply_exports_profiles_and_playlist(tmp_path):
    db_path = tmp_path / "collection.sqlite3"
    rekordbox = tmp_path / "collection.xml"
    traktor = tmp_path / "collection.nml"
    _seed_track(db_path)
    _write_rekordbox(rekordbox)
    upsert_manual_profile(
        db_path,
        track_path=LOCAL_PATH,
        energy=4,
        personal_rating=5,
        set_role="peak",
        notes="migration test",
    )
    add_tag(
        db_path,
        track_path=LOCAL_PATH,
        category="groove",
        value="rolling",
    )

    preview = sync_libraries(
        rekordbox_path=rekordbox,
        traktor_path=traktor,
        db_path=db_path,
        source_of_truth="rekordbox",
    )
    assert preview.dry_run
    assert preview.matched_rekordbox == 1
    assert preview.traktor_changes > 0
    assert not traktor.exists()

    applied = sync_libraries(
        rekordbox_path=rekordbox,
        traktor_path=traktor,
        db_path=db_path,
        dry_run=False,
        source_of_truth="rekordbox",
    )

    assert not applied.dry_run
    migrated = parse_traktor_document(traktor)
    assert migrated.tracks[0].legacy_rating == 4
    assert migrated.tracks[0].label == "Sync Records"
    assert [cue.kind for cue in migrated.tracks[0].cues] == ["cue", "loop"]
    assert migrated.playlists[0].track_paths == (LOCAL_PATH,)
    profile = decode_profile_marker(migrated.tracks[0].comment)
    assert profile is not None
    assert profile["energy"] == 4
    assert profile["personal_rating"] == 5
    assert profile["tags"] == ["groove:rolling"]
    assert rekordbox.read_text(encoding="utf-8").startswith("<?xml version")


def test_manual_conflict_blocks_writes_and_can_be_resolved(tmp_path):
    db_path = tmp_path / "collection.sqlite3"
    rekordbox = tmp_path / "collection.xml"
    traktor = tmp_path / "collection.nml"
    _seed_track(db_path)
    _write_rekordbox(rekordbox, rating=4)
    _write_traktor(traktor, rating=5)
    before_rekordbox = rekordbox.read_bytes()
    before_traktor = traktor.read_bytes()

    report = sync_libraries(
        rekordbox_path=rekordbox,
        traktor_path=traktor,
        db_path=db_path,
        dry_run=False,
        conflict_policy="manual",
    )

    assert report.unresolved_conflicts == 1
    assert rekordbox.read_bytes() == before_rekordbox
    assert traktor.read_bytes() == before_traktor
    conflict = list_sync_conflicts(db_path)[0]
    assert conflict.field_name == "rating"
    repeated = sync_libraries(
        rekordbox_path=rekordbox,
        traktor_path=traktor,
        db_path=db_path,
        dry_run=False,
        conflict_policy="manual",
    )
    assert repeated.unresolved_conflicts == 1
    assert rekordbox.read_bytes() == before_rekordbox
    assert traktor.read_bytes() == before_traktor
    resolve_sync_conflict(
        db_path, conflict_id=conflict.conflict_id, resolution="rekordbox"
    )

    resolved = sync_libraries(
        rekordbox_path=rekordbox,
        traktor_path=traktor,
        db_path=db_path,
        dry_run=False,
        conflict_policy="manual",
    )
    assert resolved.unresolved_conflicts == 0
    assert parse_traktor_document(traktor).tracks[0].legacy_rating == 4
    assert list_sync_conflicts(db_path) == []
    assert list_sync_conflicts(db_path, include_resolved=True)[0].status == "resolved"


def test_traktor_to_rekordbox_migration_preserves_library_structure(tmp_path):
    db_path = tmp_path / "collection.sqlite3"
    traktor = tmp_path / "collection.nml"
    rekordbox = tmp_path / "collection.xml"
    _seed_track(db_path)
    _write_traktor(traktor, rating=5)

    result = sync_libraries(
        rekordbox_path=rekordbox,
        traktor_path=traktor,
        db_path=db_path,
        dry_run=False,
        source_of_truth="traktor",
    )

    assert result.rekordbox_changes > 0
    migrated = parse_rekordbox_document(rekordbox)
    assert migrated.tracks[0].legacy_rating == 5
    assert migrated.tracks[0].bpm == 126.5
    assert migrated.tracks[0].beatgrids[0].start_ms == 125
    assert [cue.kind for cue in migrated.tracks[0].cues] == ["cue", "loop"]
    assert migrated.playlists[0].folder_path == ("Sets",)
    assert migrated.playlists[0].track_paths == (
        "file://localhost/C:/Music/Night%20Drive.mp3",
    )


def test_migration_can_copy_audio_without_overwriting_existing_files(tmp_path):
    audio = tmp_path / "source" / "Night Drive.mp3"
    audio.parent.mkdir()
    audio.write_bytes(b"audio-content")
    db_path = tmp_path / "collection.sqlite3"
    rekordbox = tmp_path / "collection.xml"
    traktor = tmp_path / "collection.nml"
    destination = tmp_path / "traktor-music"
    _seed_track(db_path, str(audio))
    _write_rekordbox(rekordbox, local_path=str(audio))

    preview = sync_libraries(
        rekordbox_path=rekordbox,
        traktor_path=traktor,
        db_path=db_path,
        source_of_truth="rekordbox",
        copy_audio_to=destination,
    )
    assert preview.audio_files_copied == 1
    assert not destination.exists()

    applied = sync_libraries(
        rekordbox_path=rekordbox,
        traktor_path=traktor,
        db_path=db_path,
        dry_run=False,
        source_of_truth="rekordbox",
        copy_audio_to=destination,
    )
    copied = destination / audio.name
    assert applied.audio_files_copied == 1
    assert copied.read_bytes() == b"audio-content"
    assert parse_traktor_document(traktor).tracks[0].source_path == str(copied)
    assert audio.is_file()


def test_preferred_source_sync_makes_backups_and_watch_supports_bounded_runs(tmp_path):
    db_path = tmp_path / "collection.sqlite3"
    rekordbox = tmp_path / "collection.xml"
    traktor = tmp_path / "collection.nml"
    backups = tmp_path / "backups"
    _seed_track(db_path)
    _write_rekordbox(rekordbox, rating=3)
    _write_traktor(traktor, rating=5)

    report = sync_libraries(
        rekordbox_path=rekordbox,
        traktor_path=traktor,
        db_path=db_path,
        dry_run=False,
        conflict_policy="prefer-rekordbox",
        backup_dir=backups,
    )
    assert report.unresolved_conflicts == 0
    assert report.backups
    assert all(Path(path).is_file() for path in report.backups)
    assert parse_traktor_document(traktor).tracks[0].legacy_rating == 3

    watched = list(
        watch_libraries(
            rekordbox_path=rekordbox,
            traktor_path=traktor,
            db_path=db_path,
            interval=0.25,
            conflict_policy="manual",
            max_cycles=1,
        )
    )
    assert len(watched) == 1


def test_library_migration_cli_is_dry_run_by_default(tmp_path, capsys):
    db_path = tmp_path / "collection.sqlite3"
    rekordbox = tmp_path / "collection.xml"
    traktor = tmp_path / "collection.nml"
    report = tmp_path / "migration.json"
    audio = tmp_path / "Night Drive.wav"
    audio.write_bytes(b"audio")
    _write_rekordbox(rekordbox, local_path=str(audio))

    arguments = [
        "library",
        "migrate-rekordbox-to-traktor",
        str(rekordbox),
        str(traktor),
        "--db-path",
        str(db_path),
        "--report",
        str(report),
    ]
    assert main(arguments) == 0
    assert "Dry run" in capsys.readouterr().out
    assert report.is_file()
    assert not traktor.exists()

    assert main([*arguments, "--apply"]) == 0
    assert traktor.is_file()
    assert "Tracks ready: 1/1" in capsys.readouterr().out
    assert not db_path.exists()
