from pathlib import Path
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

from crate_digger.collection.dj_curation import (
    curation_history,
    get_curation,
    save_curation,
)
from crate_digger.collection.index import _ensure_schema
from crate_digger.collection.profiles import get_profile, upsert_manual_profile
from crate_digger.web.app import create_app


def indexed_track(tmp_path: Path) -> tuple[Path, str]:
    db = tmp_path / "state.sqlite3"
    audio = tmp_path / "One.mp3"
    audio.write_bytes(b"fixture")
    stat = audio.stat()
    import sqlite3

    with sqlite3.connect(db) as conn:
        _ensure_schema(conn)
        conn.execute(
            """insert into tracks
               (path, stem, title, artist, genre, size, mtime_ns, indexed_at)
               values (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(audio),
                audio.stem,
                "One",
                "Ada",
                "Tech House",
                stat.st_size,
                stat.st_mtime_ns,
                "2026-09-25T00:00:00Z",
            ),
        )
    return db, str(audio)


def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        "[spotify]\n"
        'to-listen-playlist = "listen"\n'
        'test-playlist = "test"\n'
        'followed-labels-playlist = "labels"\n'
        'to-download-playlist = "download"\n'
        'acapella-playlist = "acapella"\n'
        'scopes = ["playlist-read-private"]\n'
        "[collection]\nmusic-dirs = []\n",
        encoding="utf-8",
    )
    return path


def test_reviewed_fields_keep_imported_metadata_and_profile_values(tmp_path: Path):
    db, path = indexed_track(tmp_path)
    upsert_manual_profile(
        db,
        track_path=path,
        energy=2,
        personal_rating=5,
        set_role="builder",
        notes="keep",
    )
    assert save_curation(
        db,
        path,
        genre="House",
        energy=3,
        tone=-1,
        character=["rolling", "hypnotic"],
        vocal_presence="instrumental",
        collection_category="DOWNLOAD",
    )
    row = get_curation(db, path)
    assert row["embedded_genre"] == "Tech House"
    assert row["approved_genre"] == "House"
    assert row["tone"] == -1
    assert row["character"] == ["rolling", "hypnotic"]
    profile = get_profile(db, track_path=path)
    assert profile is not None
    assert (
        profile.energy,
        profile.personal_rating,
        profile.set_role,
        profile.notes,
    ) == (3, 5, "builder", "keep")
    assert not save_curation(
        db,
        path,
        genre="House",
        energy=3,
        tone=-1,
        character=["rolling", "hypnotic"],
        vocal_presence="instrumental",
        collection_category="DOWNLOAD",
    )
    assert len(curation_history(db, path)) == 1
    assert curation_history(db, path)[0]["before"]["energy"] == 2
    for invalid in (["rolling", "funky", "driving"], ["unknown"]):
        with pytest.raises(ValueError, match="Character"):
            save_curation(
                db,
                path,
                genre="House",
                energy=3,
                tone=-1,
                character=invalid,
                vocal_presence=None,
                collection_category=None,
            )
    with pytest.raises(ValueError, match="Tone"):
        save_curation(
            db,
            path,
            genre="House",
            energy=3,
            tone=3,
            character=[],
            vocal_presence=None,
            collection_category=None,
        )


def test_curation_dashboard_saves_manual_decision(tmp_path: Path):
    db, path = indexed_track(tmp_path)
    client = TestClient(create_app(config_path=str(config_file(tmp_path)), db_path=db))
    page = client.get("/curate", params={"path": path})
    assert page.status_code == 200
    assert "Embedded genre: Tech House" in page.text
    body = urlencode(
        [
            ("path", path),
            ("genre", "House"),
            ("energy", "4"),
            ("tone", "2"),
            ("character", "funky"),
            ("character", "melodic"),
            ("vocal_presence", "mixed"),
            ("collection_category", "DOWNLOAD"),
        ]
    )
    saved = client.post(
        "/curate",
        content=body,
        headers={"content-type": "application/x-www-form-urlencoded"},
        follow_redirects=True,
    )
    assert saved.status_code == 200
    assert "Reviewed DJ metadata saved" in saved.text
    assert get_curation(db, path)["character"] == ["funky", "melodic"]
    assert "Traktor readiness: preview/export pending" in saved.text
