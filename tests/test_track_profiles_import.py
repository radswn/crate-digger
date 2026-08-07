import json
import sqlite3
from pathlib import Path

import pytest

from crate_digger.collection.importers.rekordbox import parse_rekordbox
from crate_digger.collection.importers.traktor import (
    convert_traktor_rating,
    parse_traktor,
)
from crate_digger.collection.index import _ensure_schema
from crate_digger.collection.library_import import import_library, write_json_report
from crate_digger.collection.matching import (
    IndexedTrack,
    apply_path_maps,
    match_tracks,
    normalize_path,
    parse_path_map,
)
from crate_digger.collection.models import ImportedTrack
from crate_digger.collection.profiles import (
    add_tag,
    get_profile,
    list_source_metadata,
    list_tags,
    remove_manual_tag,
    upsert_manual_profile,
)


FIXTURES = Path(__file__).parent / "fixtures"


def _imported(path: str, *, artist: str = "Ada", title: str = "Track") -> ImportedTrack:
    return ImportedTrack(
        source="rekordbox",
        source_path=path,
        source_track_id="1",
        title=title,
        artist=artist,
        genre=None,
        comment=None,
        comment2=None,
        legacy_rating=None,
        tags=(),
    )


def _seed_track(
    db_path: Path,
    path: str,
    *,
    title: str = "Track",
    artist: str = "Ada",
) -> None:
    with sqlite3.connect(db_path) as conn:
        _ensure_schema(conn)
        conn.execute(
            """
            insert into tracks (
                path, stem, title, artist, audio_format, artwork_checked,
                size, mtime_ns, indexed_at
            ) values (?, ?, ?, ?, 'MP3', 1, 1, 1, '2026-01-01T00:00:00+00:00')
            """,
            (path, Path(path).stem, title, artist),
        )


def test_rekordbox_parser_handles_urls_unicode_tags_and_invalid_records():
    tracks = parse_rekordbox(FIXTURES / "rekordbox_collection.xml")

    assert len(tracks) == 3
    assert normalize_path(tracks[0].source_path or "") == "C:/Music/Night Drive.mp3"
    assert tracks[0].legacy_rating == 4
    assert set(tracks[0].tags) == {
        ("palette", "tech"),
        ("groove", "groovy"),
        ("legacy", "strange tag"),
    }
    assert "Zażółć.flac" in normalize_path(tracks[1].source_path or "")
    assert tracks[1].legacy_rating is None
    assert tracks[2].invalid_reason == "Missing track location"


def test_traktor_parser_reconstructs_windows_and_posix_paths_and_converts_rating():
    tracks = parse_traktor(FIXTURES / "traktor_collection.nml")

    assert tracks[0].source_path == "C:/Music/Night Drive.mp3"
    assert tracks[0].source_track_id == "stable-1"
    assert tracks[0].legacy_rating == 5
    assert set(tracks[0].tags) == {
        ("groove", "rolling"),
        ("palette", "deep"),
        ("legacy", "odd tag"),
    }
    assert tracks[1].source_path == "/mnt/music/unrated.flac"
    assert tracks[1].legacy_rating is None
    assert tracks[2].invalid_reason == "Missing track location"
    assert [convert_traktor_rating(str(value)) for value in (0, 1, 51, 102, 255)] == [
        None,
        1,
        1,
        2,
        5,
    ]


def test_parsers_reject_well_formed_files_of_the_wrong_type(tmp_path):
    invalid = tmp_path / "not-a-collection.xml"
    invalid.write_text("<collection />", encoding="utf-8")

    with pytest.raises(ValueError, match="Invalid Rekordbox XML"):
        parse_rekordbox(invalid)
    with pytest.raises(ValueError, match="Invalid Traktor NML"):
        parse_traktor(invalid)


def test_matching_uses_paths_maps_case_and_conservative_metadata_fallback():
    indexed = [
        IndexedTrack("/music/exact.mp3", "Track", "Ada"),
        IndexedTrack("/music/Case.MP3", "Case", "Ada"),
        IndexedTrack("/new/Track.mp3", "Track", "Ada"),
    ]
    path_map = parse_path_map(r"D:\Music=/new")
    matches = match_tracks(
        [
            _imported("/music/exact.mp3"),
            _imported("/MUSIC/case.mp3", title="Case"),
            _imported(r"D:\Music\Track.mp3"),
            _imported("/elsewhere/Track.mp3"),
            _imported("/missing/no.mp3"),
        ],
        indexed,
        (path_map,),
    )

    assert [match.status for match in matches] == [
        "matched",
        "matched",
        "matched",
        "matched",
        "unmatched",
    ]
    assert matches[1].reason == "Case-insensitive path"
    assert matches[3].reason == "Unique filename, artist, and title"
    assert apply_path_maps(r"D:\Music\Track.mp3", (path_map,)) == "/new/Track.mp3"


def test_matching_never_chooses_an_ambiguous_metadata_fallback():
    match = match_tracks(
        [_imported("/lost/Track.mp3")],
        [
            IndexedTrack("/one/Track.mp3", "Track", "Ada"),
            IndexedTrack("/two/Track.mp3", "Track", "Ada"),
        ],
    )[0]

    assert match.status == "ambiguous"
    assert match.candidate_paths == ("/one/Track.mp3", "/two/Track.mp3")


@pytest.mark.parametrize("value", ["source-only", "=destination", "source="])
def test_malformed_path_map_is_rejected(value):
    with pytest.raises(ValueError, match="Invalid path map"):
        parse_path_map(value)


def test_existing_schema_upgrades_and_foreign_keys_cascade(tmp_path):
    db_path = tmp_path / "collection.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            create table tracks (
                path text primary key, stem text not null, title text, artist text,
                album text, duration_seconds real, bitrate integer, audio_format text,
                size integer not null, mtime_ns integer not null, indexed_at text not null
            )
            """
        )
        _ensure_schema(conn)
        tables = {
            row[0]
            for row in conn.execute(
                "select name from sqlite_master where type = 'table'"
            )
        }

    assert {
        "track_profiles",
        "track_tags",
        "track_source_metadata",
        "library_imports",
    } <= tables

    _seed_track(db_path, "/music/cascade.mp3")
    upsert_manual_profile(
        db_path,
        track_path="/music/cascade.mp3",
        energy=3,
        personal_rating=None,
        set_role=None,
        notes=None,
    )
    with sqlite3.connect(db_path) as conn:
        _ensure_schema(conn)
        conn.execute("delete from tracks where path = '/music/cascade.mp3'")
        assert conn.execute("select count(*) from track_profiles").fetchone()[0] == 0


def test_import_is_idempotent_separates_sources_and_preserves_manual_data(tmp_path):
    db_path = tmp_path / "collection.sqlite3"
    local_path = "C:/Music/Night Drive.mp3"
    _seed_track(db_path, local_path, title="Night Drive")
    upsert_manual_profile(
        db_path,
        track_path=local_path,
        energy=5,
        personal_rating=4,
        set_role="peak",
        notes="trusted",
    )
    add_tag(
        db_path,
        track_path=local_path,
        category="palette",
        value="tech",
    )

    first = import_library(
        source="rekordbox",
        source_file=FIXTURES / "rekordbox_collection.xml",
        db_path=db_path,
    )
    second = import_library(
        source="rekordbox",
        source_file=FIXTURES / "rekordbox_collection.xml",
        db_path=db_path,
    )
    traktor = import_library(
        source="traktor",
        source_file=FIXTURES / "traktor_collection.nml",
        db_path=db_path,
    )

    assert first.matched_count == second.matched_count == traktor.matched_count == 1
    assert get_profile(db_path, track_path=local_path).energy == 5  # type: ignore[union-attr]
    metadata = list_source_metadata(db_path, track_path=local_path)
    assert [(item.source, item.legacy_rating) for item in metadata] == [
        ("rekordbox", 4),
        ("traktor", 5),
    ]
    tags = list_tags(db_path, track_path=local_path)
    assert len(tags) == len({(tag.category, tag.value, tag.source) for tag in tags})
    assert any(tag.source == "manual" and tag.value == "tech" for tag in tags)

    assert remove_manual_tag(
        db_path, track_path=local_path, category="palette", value="tech"
    )
    remaining = list_tags(db_path, track_path=local_path)
    assert not any(tag.source == "manual" and tag.value == "tech" for tag in remaining)
    assert any(tag.source == "rekordbox" and tag.value == "tech" for tag in remaining)


def test_dry_run_and_json_report_do_not_write(tmp_path):
    db_path = tmp_path / "collection.sqlite3"
    _seed_track(db_path, "C:/Music/Night Drive.mp3", title="Night Drive")
    report = import_library(
        source="rekordbox",
        source_file=FIXTURES / "rekordbox_collection.xml",
        db_path=db_path,
        dry_run=True,
    )
    output = tmp_path / "reports" / "import.json"
    write_json_report(report, output)

    with sqlite3.connect(db_path) as conn:
        assert (
            conn.execute("select count(*) from track_source_metadata").fetchone()[0]
            == 0
        )
        assert conn.execute("select count(*) from library_imports").fetchone()[0] == 0
    data = json.loads(output.read_text(encoding="utf-8"))
    assert data["summary"]["matched_tracks"] == 1
    assert data["matches"][0]["source_path"].endswith("Night%20Drive.mp3")


def test_dry_run_does_not_create_a_missing_database(tmp_path):
    db_path = tmp_path / "nested" / "collection.sqlite3"

    report = import_library(
        source="rekordbox",
        source_file=FIXTURES / "rekordbox_collection.xml",
        db_path=db_path,
        dry_run=True,
    )

    assert report.matched_count == 0
    assert not db_path.exists()
    assert not db_path.parent.exists()
