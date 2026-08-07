import csv
import sqlite3
from pathlib import Path

from crate_digger.cli import main
from crate_digger.collection.index import _ensure_schema


FIXTURES = Path(__file__).parent / "fixtures"


def _seed(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        _ensure_schema(conn)
        conn.execute(
            """
            insert into tracks (
                path, stem, title, artist, album, duration_seconds, audio_format,
                spotify_uri, artwork_checked, size, mtime_ns, indexed_at
            ) values (
                'C:/Music/Night Drive.mp3', 'Night Drive', 'Night Drive', 'Ada',
                'Night Work', 240, 'MP3', 'spotify:track:1', 1, 1, 1,
                '2026-01-01T00:00:00+00:00'
            )
            """
        )


def test_library_cli_import_status_report_and_training_export(tmp_path, capsys):
    db_path = tmp_path / "collection.sqlite3"
    _seed(db_path)
    report_path = tmp_path / "report.json"

    assert (
        main(
            [
                "library",
                "import-rekordbox",
                str(FIXTURES / "rekordbox_collection.xml"),
                "--db-path",
                str(db_path),
                "--report",
                str(report_path),
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "Source type: rekordbox" in output
    assert "Matched tracks: 1" in output
    assert report_path.is_file()

    assert main(["library", "status", "--db-path", str(db_path)]) == 0
    status = capsys.readouterr().out
    assert "Total indexed tracks: 1" in status
    assert "Tracks imported from Rekordbox: 1" in status

    csv_path = tmp_path / "training.csv"
    assert (
        main(
            [
                "library",
                "export-training-data",
                str(csv_path),
                "--db-path",
                str(db_path),
            ]
        )
        == 0
    )
    with csv_path.open(encoding="utf-8", newline="") as handle:
        row = next(csv.DictReader(handle))
    assert row["path"] == "C:/Music/Night Drive.mp3"
    assert row["rekordbox_legacy_rating"] == "4"
    assert row["rekordbox_tags"] == "groove:groovy|legacy:strange tag|palette:tech"


def test_library_cli_dry_run_and_invalid_input_return_codes(tmp_path, capsys):
    db_path = tmp_path / "collection.sqlite3"
    _seed(db_path)
    assert (
        main(
            [
                "library",
                "import-traktor",
                str(FIXTURES / "traktor_collection.nml"),
                "--db-path",
                str(db_path),
                "--dry-run",
            ]
        )
        == 0
    )
    assert "Dry run" in capsys.readouterr().out
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("select count(*) from library_imports").fetchone()[0] == 0

    assert (
        main(
            [
                "library",
                "import-rekordbox",
                str(tmp_path / "missing.xml"),
                "--db-path",
                str(db_path),
            ]
        )
        == 2
    )
    assert "Invalid Rekordbox XML" in capsys.readouterr().err
