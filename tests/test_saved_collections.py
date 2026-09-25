import sqlite3
from pathlib import Path
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

from crate_digger.collection.dj_curation import save_curation
from crate_digger.collection.saved_collections import (
    collection_history,
    list_saved_collections,
    preview_collection,
    save_collection,
    validate_rule,
)
from crate_digger.web.app import create_app
from tests.test_dj_curation import config_file, indexed_track


def curated_tracks(tmp_path: Path) -> tuple[Path, list[str]]:
    db, first = indexed_track(tmp_path)
    paths = [first]
    for title in ("Two", "Three"):
        audio = tmp_path / f"{title}.mp3"
        audio.write_bytes(b"fixture")
        stat = audio.stat()
        with sqlite3.connect(db) as conn:
            conn.execute(
                """insert into tracks
                   (path, stem, title, artist, size, mtime_ns, indexed_at)
                   values (?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(audio),
                    title,
                    title,
                    "Ada",
                    stat.st_size,
                    stat.st_mtime_ns,
                    "2026-09-25T00:00:00Z",
                ),
            )
        paths.append(str(audio))
    for path, genre, energy, tone, character in (
        (paths[0], "House", 2, 1, ["funky"]),
        (paths[1], "Techno", 4, -2, ["rolling"]),
        (paths[2], "House", 3, 2, ["funky", "melodic"]),
    ):
        save_curation(
            db,
            path,
            genre=genre,
            energy=energy,
            tone=tone,
            character=character,
            vocal_presence=None,
            collection_category="DOWNLOAD",
        )
    return db, paths


def test_saved_rules_match_reviewed_fields_and_keep_history(tmp_path: Path):
    db, paths = curated_tracks(tmp_path)
    bright = save_collection(
        db,
        name="Bright Funky",
        description="Build gently",
        rule={
            "min_energy": 2,
            "max_energy": 3,
            "min_tone": 1,
            "character_tags": ["funky"],
        },
    )
    assert [row["path"] for row in preview_collection(db, bright)] == [
        paths[0],
        paths[2],
    ]
    dark = save_collection(
        db,
        name="Dark Rolling",
        description="",
        rule={
            "min_energy": 4,
            "max_energy": 5,
            "max_tone": -1,
            "character_tags": ["rolling"],
            "genre": "Techno",
        },
    )
    assert [row["path"] for row in preview_collection(db, dark)] == [paths[1]]
    assert (
        save_collection(
            db,
            name="Bright Funky",
            description="More lift",
            rule={
                "min_energy": 2,
                "max_energy": 3,
                "min_tone": 1,
                "character_tags": ["funky"],
            },
            collection_id=bright,
        )
        == bright
    )
    assert len(collection_history(db, bright)) == 2
    assert collection_history(db, bright)[0]["before"]["description"] == "Build gently"
    assert len(list_saved_collections(db)) == 2
    with pytest.raises(ValueError, match="already has"):
        save_collection(
            db, name="dark rolling", description="", rule={"genre": "House"}
        )
    with pytest.raises(ValueError, match="Minimum Energy"):
        validate_rule({"min_energy": 5, "max_energy": 2})
    with pytest.raises(ValueError, match="Character"):
        validate_rule({"character_tags": ["rolling", "rolling"]})
    with pytest.raises(ValueError, match="Unknown"):
        validate_rule({"sql": "drop table tracks"})


def test_saved_collection_dashboard_previews_without_external_writes(tmp_path: Path):
    db, paths = curated_tracks(tmp_path)
    client = TestClient(create_app(config_path=str(config_file(tmp_path)), db_path=db))
    page = client.get("/collections")
    assert page.status_code == 200
    body = urlencode(
        [
            ("name", "Bright Funky"),
            ("description", "E2–3 bright/funky"),
            ("min_energy", "2"),
            ("max_energy", "3"),
            ("min_tone", "1"),
            ("max_tone", ""),
            ("character_tags", "funky"),
            ("genre", "House"),
            ("collection_category", "DOWNLOAD"),
        ]
    )
    saved = client.post(
        "/collections/save",
        content=body,
        headers={"content-type": "application/x-www-form-urlencoded"},
        follow_redirects=True,
    )
    assert saved.status_code == 200
    assert "2 matching indexed tracks" in saved.text
    assert paths[0] in saved.text and paths[2] in saved.text
    assert "no playlist or file tags were written" in saved.text
