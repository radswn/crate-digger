import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from crate_digger.collection.index import _ensure_schema
from crate_digger.collection.models import SourceTrackMetadata
from crate_digger.collection.profiles import (
    add_tag,
    get_profile,
    list_tags,
    upsert_source_metadata,
)
from crate_digger.web.app import create_app


def _seed_audio(db_path: Path, audio_path: Path, *, title: str) -> None:
    audio_path.write_bytes(b"ID3" + b"\0" * 100)
    with sqlite3.connect(db_path) as conn:
        _ensure_schema(conn)
        conn.execute(
            """
            insert into tracks (
                path, stem, title, artist, album, duration_seconds, audio_format,
                artwork_checked, size, mtime_ns, indexed_at
            ) values (?, ?, ?, 'Ada', 'Night Work', 123, 'MP3', 1, 103, 1,
                      '2026-01-01T00:00:00+00:00')
            """,
            (str(audio_path), audio_path.stem, title),
        )


def test_profiles_page_update_modes_and_existing_dashboard(tmp_path):
    db_path = tmp_path / "collection.sqlite3"
    first = tmp_path / "first.mp3"
    second = tmp_path / "second.mp3"
    _seed_audio(db_path, first, title="First Track")
    _seed_audio(db_path, second, title="Second Track")
    upsert_source_metadata(
        db_path,
        metadata=SourceTrackMetadata(
            track_path=str(first),
            source="rekordbox",
            source_track_id="1",
            legacy_rating=4,
            genre="Techno",
            comment="/* Tech */",
            comment2=None,
            imported_at="2026-01-01T00:00:00+00:00",
        ),
    )
    upsert_source_metadata(
        db_path,
        metadata=SourceTrackMetadata(
            track_path=str(first),
            source="traktor",
            source_track_id="traktor-1",
            legacy_rating=5,
            genre="Tech House",
            comment="/* Rolling */",
            comment2=None,
            imported_at="2026-01-01T00:00:00+00:00",
        ),
    )
    add_tag(
        db_path,
        track_path=str(first),
        category="palette",
        value="tech",
        source="rekordbox",
    )
    client = TestClient(create_app(db_path=db_path))

    page = client.get("/profiles")
    assert page.status_code == 200
    assert "Track Profiles" in page.text
    assert "First Track" in page.text
    assert 'preload="metadata"' in page.text
    assert "Legacy rating: 4" in page.text
    assert client.get("/health").json() == {"status": "ok"}

    saved = client.post(
        "/profiles/update",
        data={
            "path": str(first),
            "mode": "missing-energy",
            "energy": "4",
            "personal_rating": "5",
            "set_role": "builder",
            "notes": "play after midnight",
            "tag_palette_tech": "on",
            "tag_groove_groovy": "on",
        },
        follow_redirects=False,
    )
    assert saved.status_code == 303
    assert "mode=missing-energy" in saved.headers["location"]
    assert f"path={str(second).replace('/', '%2F')}" in saved.headers["location"]
    profile = get_profile(db_path, track_path=str(first))
    assert profile is not None
    assert (profile.energy, profile.personal_rating, profile.set_role) == (
        4,
        5,
        "builder",
    )
    manual = [
        tag.value
        for tag in list_tags(db_path, track_path=str(first))
        if tag.source == "manual"
    ]
    assert manual == ["groovy", "tech"]

    imported = client.get("/profiles", params={"mode": "imported"})
    assert "First Track" in imported.text
    conflicts = client.get("/profiles", params={"mode": "conflicts"})
    assert "First Track" in conflicts.text
    all_tracks = client.get("/api/profiles", params={"mode": "all"}).json()
    assert all_tracks["count"] == 2


def test_profile_api_validates_energy_role_and_toggles_tags(tmp_path):
    db_path = tmp_path / "collection.sqlite3"
    audio = tmp_path / "track.mp3"
    _seed_audio(db_path, audio, title="Track")
    client = TestClient(create_app(db_path=db_path))

    assert (
        client.post(
            "/api/profiles/update", json={"path": str(audio), "energy": 6}
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/profiles/update",
            json={"path": str(audio), "energy": 3, "set_role": "opening"},
        ).status_code
        == 400
    )
    response = client.post(
        "/api/profiles/update",
        json={
            "path": str(audio),
            "mode": "all",
            "energy": 3,
            "set_role": "warmup",
            "manual_tags": ["mood:dark", "legacy:custom"],
            "tag_groove_rolling": True,
        },
    )
    assert response.status_code == 200
    assert response.json()["mode"] == "all"
    assert {(tag["category"], tag["value"]) for tag in response.json()["tags"]} == {
        ("mood", "dark"),
        ("legacy", "custom"),
        ("groove", "rolling"),
    }


def test_audio_endpoint_only_serves_indexed_existing_files(tmp_path):
    db_path = tmp_path / "collection.sqlite3"
    indexed = tmp_path / "indexed.mp3"
    unindexed = tmp_path / "unindexed.mp3"
    _seed_audio(db_path, indexed, title="Indexed")
    unindexed.write_bytes(b"not indexed")
    client = TestClient(create_app(db_path=db_path))

    served = client.get("/profiles/audio", params={"path": str(indexed)})
    assert served.status_code == 200
    assert served.content.startswith(b"ID3")
    assert served.headers["content-type"].startswith("audio/mpeg")
    ranged = client.get(
        "/profiles/audio",
        params={"path": str(indexed)},
        headers={"Range": "bytes=0-2"},
    )
    assert ranged.status_code == 206
    assert ranged.content == b"ID3"
    assert (
        client.get("/profiles/audio", params={"path": str(unindexed)}).status_code
        == 404
    )
    indexed.unlink()
    assert (
        client.get("/profiles/audio", params={"path": str(indexed)}).status_code == 404
    )
