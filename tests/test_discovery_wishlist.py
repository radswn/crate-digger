from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from crate_digger.discover.models import SpotifyEntityTrack
from crate_digger.discover.repository import connect, upsert_spotify_tracks
from crate_digger.discover.sessions import build_session, list_session_items
from crate_digger.discover.wishlist import (
    confirm_local_match,
    list_wishlist,
    playlist_import_preview,
    preview_local_match,
    set_progress,
    set_wanted,
    wishlist_events,
)
from crate_digger.web.app import create_app


def setup_db(tmp_path: Path) -> tuple[Path, int, int]:
    db = tmp_path / "state.sqlite3"
    upsert_spotify_tracks(
        db,
        [
            SpotifyEntityTrack(
                spotify_track_id="one",
                spotify_uri="spotify:track:one",
                title="One",
                artists=(("artist", "Ada"),),
                spotify_release_id="album",
                release_title="Album",
                release_date="2026-09-01",
                raw_label_name="Label",
            )
        ],
        label_aliases={},
        source="manual",
    )
    session_id = build_session(db, size=1).session.session_id
    return db, session_id, list_session_items(db, session_id)[0].item_id


def config_file(tmp_path: Path) -> Path:
    config = tmp_path / "config.toml"
    config.write_text(
        "[spotify]\n"
        'to-listen-playlist = "listen"\n'
        'test-playlist = "test"\n'
        'followed-labels-playlist = "labels"\n'
        'to-download-playlist = "spotify:playlist:download"\n'
        'acapella-playlist = "acapella"\n'
        'scopes = ["playlist-read-private"]\n'
        "[collection]\nmusic-dirs = []\n",
        encoding="utf-8",
    )
    return config


def test_want_is_independent_of_taste_and_history_is_idempotent(tmp_path: Path):
    db, session_id, item_id = setup_db(tmp_path)
    client = TestClient(create_app(config_path=str(config_file(tmp_path)), db_path=db))
    page = client.get("/discover", params={"session_id": session_id})
    assert f'name="want_{item_id}" value="1"' in page.text
    result = client.post(
        "/discover/finish",
        data={
            "session_id": str(session_id),
            "unmarked": "later",
            f"decision_{item_id}": "skip",
            f"want_{item_id}": "1",
        },
        follow_redirects=True,
    )
    assert result.status_code == 200
    assert list_session_items(db, session_id)[0].decision == "pass"
    assert (
        client.get(
            f"/api/discover/sessions/{session_id}/items/{item_id}/history"
        ).json()["events"][0]["to_decision"]
        == "pass"
    )
    assert list_wishlist(db)[0]["status"] == "wanted"
    assert len(wishlist_events(db, "one")) == 1
    assert not set_wanted(db, "one", True)
    assert len(wishlist_events(db, "one")) == 1
    assert client.get("/wishlist").status_code == 200
    assert "Taste: pass" in client.get("/wishlist").text
    assert (
        client.post("/api/wishlist/one", json={"wanted": False}).json()["status"]
        == "removed"
    )
    assert list_wishlist(db) == []
    assert len(wishlist_events(db, "one")) == 2
    assert (
        client.post("/api/wishlist/missing", json={"wanted": True}).status_code == 404
    )


def test_progress_retries_and_invalid_transitions(tmp_path: Path):
    db, _session_id, _item_id = setup_db(tmp_path)
    assert set_wanted(db, "one", True)
    assert set_progress(db, "one", "searching")
    assert set_progress(db, "one", "unavailable", note="No exact mix found")
    assert set_progress(db, "one", "wanted")
    assert not set_progress(db, "one", "wanted")
    with pytest.raises(ValueError, match="Cannot move"):
        set_progress(db, "one", "ready")
    assert [event["to_status"] for event in wishlist_events(db, "one")] == [
        "wanted",
        "unavailable",
        "searching",
        "wanted",
    ]
    assert wishlist_events(db, "one")[1]["note"] == "No exact mix found"


class FakeSpotify:
    def __init__(self):
        self.tracks = [
            {
                "id": "one",
                "uri": "spotify:track:one",
                "name": "One",
                "artists": [{"id": "artist", "name": "Ada"}],
                "album": {"id": "album", "name": "Album"},
            },
            {
                "id": "two",
                "uri": "spotify:track:two",
                "name": "Two",
                "artists": [{"id": "artist", "name": "Ada"}],
                "album": {"id": "album", "name": "Album"},
            },
        ]
        self.calls = 0

    def playlist_items(self, uri, *, limit, offset, additional_types):
        assert uri == "spotify:playlist:download"
        assert (limit, offset, additional_types) == (100, 0, ("track",))
        self.calls += 1
        return {"items": [{"track": item} for item in self.tracks], "next": None}


def test_import_reads_current_spotify_items_and_counts_unavailable():
    class CurrentSpotify:
        def _get(self, path, *, limit, offset):
            assert path == "playlists/download/items"
            assert (limit, offset) == (50, 0)
            return {
                "items": [
                    {"item": {"id": "one", "uri": "spotify:track:one", "name": "One"}},
                    {"item": None},
                    {
                        "item": {
                            "id": "episode",
                            "uri": "spotify:episode:episode",
                            "name": "Episode",
                            "type": "episode",
                        }
                    },
                ],
                "next": None,
            }

    preview = playlist_import_preview(CurrentSpotify(), "spotify:playlist:download")
    assert preview["total"] == 3
    assert preview["skipped"] == 2
    assert [track.spotify_track_id for track in preview["tracks"]] == ["one"]


def test_import_requires_preview_and_keeps_removed_decision(
    tmp_path: Path, monkeypatch
):
    db, _session_id, _item_id = setup_db(tmp_path)
    set_wanted(db, "one", True)
    set_wanted(db, "one", False)
    fake = FakeSpotify()
    monkeypatch.setattr(
        "crate_digger.web.discover.get_spotify_client", lambda scope: fake
    )
    client = TestClient(create_app(config_path=str(config_file(tmp_path)), db_path=db))
    preview = client.post("/wishlist/import-preview")
    assert preview.status_code == 200
    assert "1 new to the wishlist" in preview.text
    assert "playlist will not be changed" in preview.text
    import re

    fingerprint = re.search(r'name="fingerprint" value="([a-f0-9]+)"', preview.text)
    assert fingerprint is not None
    fake.tracks.append(
        {
            "id": "three",
            "uri": "spotify:track:three",
            "name": "Three",
            "artists": [],
            "album": {},
        }
    )
    conflict = client.post(
        "/wishlist/import",
        data={
            "fingerprint": fingerprint.group(1),
            "playlist_uri": "spotify:playlist:download",
        },
        follow_redirects=True,
    )
    assert "Playlist changed since preview" in conflict.text
    assert list_wishlist(db) == []
    fresh = client.post("/wishlist/import-preview")
    fingerprint = re.search(r'name="fingerprint" value="([a-f0-9]+)"', fresh.text)
    assert fingerprint is not None
    imported = client.post(
        "/wishlist/import",
        data={
            "fingerprint": fingerprint.group(1),
            "playlist_uri": "spotify:playlist:download",
        },
        follow_redirects=True,
    )
    assert imported.status_code == 200
    assert "Imported 2 wanted tracks" in imported.text
    assert {row["spotify_track_id"] for row in list_wishlist(db)} == {"two", "three"}
    assert len(wishlist_events(db, "one")) == 2
    with connect(db) as conn:
        assert conn.execute("select count(*) from discovery_tracks").fetchone()[0] == 3
    again = client.post(
        "/wishlist/import",
        data={
            "fingerprint": fingerprint.group(1),
            "playlist_uri": "spotify:playlist:download",
        },
        follow_redirects=True,
    )
    assert "Imported 0 wanted tracks" in again.text
    assert fake.calls == 5


def test_local_match_requires_explicit_identity_and_unchanged_indexed_file(
    tmp_path: Path,
):
    db, _session_id, _item_id = setup_db(tmp_path)
    set_wanted(db, "one", True)
    audio = tmp_path / "Ada - One.mp3"
    audio.write_bytes(b"audio fixture")
    stat = audio.stat()
    with connect(db) as conn:
        conn.execute(
            """insert into tracks
               (path, stem, title, artist, album, duration_seconds, bitrate,
                audio_format, size, mtime_ns, indexed_at)
               values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(audio),
                audio.stem,
                "One",
                "Ada",
                "Album",
                360.0,
                320000,
                "mp3",
                stat.st_size,
                stat.st_mtime_ns,
                "2026-09-25T00:00:00Z",
            ),
        )
    client = TestClient(create_app(config_path=str(config_file(tmp_path)), db_path=db))
    search = client.get("/wishlist/one/match", params={"q": "Ada"})
    assert search.status_code == 200
    assert "Review match" in search.text
    page = client.get("/wishlist/one/match", params={"q": "Ada", "path": str(audio)})
    assert page.status_code == 200
    assert "I verified that this file is the exact recording and mix" in page.text
    preview = preview_local_match(db, "one", str(audio))
    with pytest.raises(ValueError, match="Confirm"):
        confirm_local_match(
            db, "one", str(audio), preview["fingerprint"], verified=False
        )
    audio.write_bytes(b"changed audio fixture")
    with pytest.raises(ValueError, match="changed since"):
        confirm_local_match(
            db, "one", str(audio), preview["fingerprint"], verified=True
        )
    stat = audio.stat()
    with connect(db) as conn:
        conn.execute(
            "update tracks set size = ?, mtime_ns = ? where path = ?",
            (stat.st_size, stat.st_mtime_ns, str(audio)),
        )
    fresh = preview_local_match(db, "one", str(audio))
    assert confirm_local_match(
        db, "one", str(audio), fresh["fingerprint"], verified=True
    )
    assert list_wishlist(db)[0]["status"] == "needs_curation"
    with connect(db) as conn:
        assert (
            conn.execute(
                "select spotify_uri from tracks where path = ?", (str(audio),)
            ).fetchone()[0]
            == "spotify:track:one"
        )
        assert conn.execute(
            "select local_track_path from discovery_tracks where spotify_track_id = 'one'"
        ).fetchone()[0] == str(audio)
    assert [event["to_status"] for event in wishlist_events(db, "one")[:2]] == [
        "needs_curation",
        "acquired",
    ]
    assert not confirm_local_match(
        db, "one", str(audio), fresh["fingerprint"], verified=True
    )
