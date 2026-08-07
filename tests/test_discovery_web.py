import textwrap
from pathlib import Path

from fastapi.testclient import TestClient

from crate_digger.discover.models import SpotifyEntityTrack
from crate_digger.discover.repository import upsert_spotify_tracks
from crate_digger.discover.sessions import build_session
from crate_digger.discover.taste import rebuild_taste_index
from crate_digger.web.app import create_app


def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        textwrap.dedent(
            """
            [spotify]
            to-listen-playlist = "listen"
            test-playlist = "test"
            followed-labels-playlist = "labels"
            to-download-playlist = "download"
            acapella-playlist = "acapella"
            scopes = ["playlist-read-private"]

            [collection]
            music-dirs = []

            [discovery]
            freshness-days = 90
            """
        ),
        encoding="utf-8",
    )
    return path


def test_discovery_page_api_feedback_and_explanations(tmp_path):
    db_path = tmp_path / "collection.sqlite3"
    upsert_spotify_tracks(
        db_path,
        [
            SpotifyEntityTrack(
                spotify_track_id="discover-me",
                spotify_uri="spotify:track:discover-me",
                title="Discover Me",
                artists=(("artist", "Ada"),),
                spotify_release_id="release",
                release_title="Discovery Release",
                release_date="2026-08-01",
                raw_label_name="New Label",
                external_url="https://open.spotify.com/track/discover-me",
            )
        ],
        label_aliases={},
        source="discovered_label",
    )
    rebuild_taste_index(db_path)
    result = build_session(db_path, size=1, freshness_days=90)
    session_id = result.session.session_id
    item_id = result.items[0].item_id
    client = TestClient(
        create_app(config_path=str(config_file(tmp_path)), db_path=db_path)
    )

    page = client.get("/discover", params={"session_id": session_id})
    assert page.status_code == 200
    assert "Taste-Aware Discovery Sessions" in page.text
    assert "Discover Me" in page.text
    assert "Why selected" in page.text
    assert "Open in Spotify" in page.text

    explain = client.get(f"/api/discover/sessions/{session_id}/items/{item_id}/explain")
    assert explain.status_code == 200
    original_reasons = explain.json()["reasons"]
    feedback = client.post(
        f"/api/discover/sessions/{session_id}/items/{item_id}/feedback",
        json={"decision": "maybe"},
    )
    assert feedback.status_code == 200
    assert feedback.json()["decision"] == "maybe"
    repeated = client.post(
        f"/api/discover/sessions/{session_id}/items/{item_id}/feedback",
        json={"decision": "maybe"},
    )
    assert repeated.status_code == 200
    assert (
        client.get(
            f"/api/discover/sessions/{session_id}/items/{item_id}/explain"
        ).json()["reasons"]
        == original_reasons
    )
    stats = client.get("/api/discover/stats")
    assert stats.status_code == 200
    assert (
        stats.json()["decision_rates_by_bucket"][result.items[0].bucket]["maybe"] == 1.0
    )
    assert "artist_affinity_statistics" in stats.json()
    assert client.get("/health").status_code == 200
