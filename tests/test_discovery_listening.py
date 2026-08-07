import json
import re
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from crate_digger.cli import main
from crate_digger.discover.listening import (
    PlaylistConflict,
    SpotifyPlaylistAdapter,
    accept_playlist_snapshot,
    bind_playlist,
    confirm_progress,
    end,
    list_runs,
    preview,
    reconcile,
    run_detail,
    start,
)
from crate_digger.discover.models import SpotifyEntityTrack
from crate_digger.discover.repository import (
    connect,
    latest_snapshot_version,
    upsert_spotify_tracks,
)
from crate_digger.discover.sessions import (
    build_session,
    get_session,
    list_session_items,
    record_feedback,
)
from crate_digger.discover.taste import rebuild_taste_index
from crate_digger.web.app import create_app


URI = "spotify:playlist:session"


def test_spotipy_adapter_uses_current_playlist_items_endpoint():
    class MockClient:
        def __init__(self):
            self.calls = []

        def playlist(self, uri):
            assert uri == URI
            return {
                "uri": URI,
                "owner": {"id": "me"},
                "public": False,
                "snapshot_id": "snapshot-1",
            }

        def _get(self, path, **kwargs):
            self.calls.append(("get", path, kwargs))
            return {"items": [{"item": {"uri": "spotify:track:one"}}], "next": None}

        def _put(self, path, **kwargs):
            self.calls.append(("put", path, kwargs))

        def current_user(self):
            return {"id": "me"}

        def tracks(self, ids):
            self.calls.append(("tracks", ids))
            return {
                "tracks": [{"id": track_id, "is_playable": True} for track_id in ids]
            }

    client = MockClient()
    adapter = SpotifyPlaylistAdapter(client)
    assert adapter.snapshot(URI)["track_uris"] == ["spotify:track:one"]
    adapter.replace(URI, [])
    assert adapter.unavailable_tracks(["spotify:track:one"]) == set()
    assert client.calls == [
        ("get", "playlists/session/items", {"limit": 50, "offset": 0}),
        ("put", "playlists/session/items", {"payload": {"uris": []}}),
        ("tracks", ["one"]),
    ]


class FakeSpotify:
    def __init__(self):
        self.uris = []
        self.version = 1
        self.calls = []
        self.fail_after_replace = False
        self.history = []
        self.history_error = False
        self.unavailable_uris = set()

    def owner_id(self):
        return "me"

    def snapshot(self, playlist_uri):
        assert playlist_uri == URI
        return {
            "uri": URI,
            "owner_id": "me",
            "public": False,
            "snapshot_id": str(self.version),
            "track_uris": list(self.uris),
        }

    def replace(self, playlist_uri, uris):
        assert playlist_uri == URI
        self.calls.append((playlist_uri, list(uris)))
        self.uris = list(uris)
        self.version += 1
        if self.fail_after_replace:
            self.fail_after_replace = False
            raise TimeoutError("response lost")

    def recently_played(self, *, before=None):
        if self.history_error:
            raise RuntimeError("missing scope")
        entries = [
            x
            for x in self.history
            if before is None
            or int(
                datetime.fromisoformat(
                    x["played_at"].replace("Z", "+00:00")
                ).timestamp()
                * 1000
            )
            < before
        ]
        return {"items": entries[:50], "next": "more" if len(entries) > 50 else None}

    def unavailable_tracks(self, uris):
        return set(uris) & self.unavailable_uris


def setup(tmp_path, count=3):
    db = tmp_path / "state.sqlite3"
    tracks = [
        SpotifyEntityTrack(
            spotify_track_id=f"track-{i}",
            spotify_uri=f"spotify:track:track-{i}",
            title=f"Track {i}",
            artists=((f"artist-{i}", f"Artist {i}"),),
            spotify_release_id=f"release-{i}",
            release_title=f"Release {i}",
            release_date="2026-01-01",
            raw_label_name=f"Label {i}",
        )
        for i in range(count)
    ]
    upsert_spotify_tracks(db, tracks, label_aliases={}, source="manual")
    rebuild_taste_index(db)
    session = build_session(db, size=count).session.session_id
    settings = {
        "spotify": {
            key: f"spotify:playlist:protected{index}"
            for index, key in enumerate(
                (
                    "to_listen_playlist",
                    "to_download_playlist",
                    "test_playlist",
                    "acapella_playlist",
                    "followed_labels_playlist",
                )
            )
        }
    }
    fake = FakeSpotify()
    bind_playlist(db, fake, settings, URI)
    return db, session, fake, settings


def test_publish_retry_conflict_end_resume_and_decisions(tmp_path):
    db, session, fake, settings = setup(tmp_path)
    plan = preview(db, fake, settings, session)
    assert len(plan["items"]) == 3
    assert fake.calls == []
    fake.fail_after_replace = True
    with pytest.raises(TimeoutError):
        start(db, fake, settings, session)
    run = start(db, fake, settings, session)
    assert run["state"] == "active"
    assert len(fake.calls) == 1
    assert fake.uris == [row["track_uri"] for row in plan["items"]]
    assert start(db, fake, settings, session)["id"] == run["id"]
    fake.uris.append("spotify:track:outsider")
    fake.version += 1
    with pytest.raises(PlaylistConflict):
        end(db, fake, settings, run["id"])
    assert fake.uris[-1] == "spotify:track:outsider"
    fake.uris.pop()
    accept_playlist_snapshot(db, fake, settings)
    first = run["items"][0]
    confirm_progress(db, run["id"], first["item_id"])
    record_feedback(db, session_id=session, item_id=first["item_id"], decision="keep")
    fake.fail_after_replace = True
    with pytest.raises(TimeoutError):
        end(db, fake, settings, run["id"])
    ended = end(db, fake, settings, run["id"])
    assert ended["state"] == "ended"
    assert fake.uris == []
    current = get_session(db, session)
    assert current is not None and current.status == "open"
    resumed = start(db, fake, settings, session, resume=True)
    assert resumed["id"] != run["id"]
    assert [row["position"] for row in resumed["items"]] == [
        row["position"] for row in run["items"][1:]
    ]
    end(db, fake, settings, resumed["id"])
    assert [
        row["position"]
        for row in preview(db, fake, settings, session, resume=True)["items"]
    ] == [row["position"] for row in run["items"][1:]]
    assert run_detail(db, run["id"])["items"][0]["decision"] == "keep"
    assert all(uri == URI for uri, _ in fake.calls)


def test_snapshot_change_with_identical_tracks_allows_cleanup(tmp_path):
    db, session, fake, settings = setup(tmp_path, 2)
    run = start(db, fake, settings, session)
    fake.version += 1  # Spotify changed metadata/snapshot, not the track order.
    assert end(db, fake, settings, run["id"])["state"] == "ended"
    assert fake.uris == []
    assert len(fake.calls) == 2


def test_unavailable_multiple_sessions_and_completed_cleanup(tmp_path):
    db, session, fake, settings = setup(tmp_path, 2)
    protected = {"spotify": {**settings["spotify"], "to_listen_playlist": URI}}
    with pytest.raises(ValueError, match="protected"):
        preview(db, fake, protected, session)
    fake.owner_id = lambda: "different-account"
    with pytest.raises(PlaylistConflict):
        preview(db, fake, settings, session)
    fake.owner_id = lambda: "me"
    second = build_session(db, size=1).session.session_id
    with connect(db) as conn:
        conn.execute(
            "update discovery_tracks set spotify_uri='unavailable' where spotify_track_id='track-0'"
        )
    plan = preview(db, fake, settings, session)
    assert len(plan["unavailable"]) == 1
    fake.unavailable_uris = {plan["items"][0]["track_uri"]}
    assert len(preview(db, fake, settings, session)["unavailable"]) == 2
    fake.unavailable_uris.clear()
    run = start(db, fake, settings, session)
    with pytest.raises(ValueError):
        start(db, fake, settings, second)
    for row in run["items"]:
        record_feedback(
            db, session_id=session, item_id=row["item_id"], decision="maybe"
        )
    # An unavailable item still needs a decision before the session completes.
    with connect(db) as conn:
        missing_id = conn.execute(
            "select id from discovery_session_items where session_id=? and id!=?",
            (session, run["items"][0]["item_id"]),
        ).fetchone()[0]
    record_feedback(db, session_id=session, item_id=missing_id, decision="skip")
    current = get_session(db, session)
    assert current is not None and current.status == "completed"
    assert end(db, fake, settings, run["id"])["state"] == "ended"


def test_observations_are_uncertain_and_do_not_advance(tmp_path):
    db, session, fake, settings = setup(tmp_path, 2)
    run = start(db, fake, settings, session)
    started = datetime.fromisoformat(run["started_at"])
    inside = (started + timedelta(seconds=1)).isoformat()
    with connect(db) as conn:
        conn.execute(
            "update discovery_listening_runs set ended_at=? where id=?",
            ((started + timedelta(minutes=5)).isoformat(), run["id"]),
        )
    fake.history = [
        {"played_at": inside, "track": {"uri": run["items"][0]["track_uri"]}},
        {
            "played_at": (started + timedelta(seconds=2)).isoformat(),
            "track": {"uri": run["items"][0]["track_uri"]},
        },
        {"played_at": inside, "track": {"uri": "spotify:track:unrelated"}},
        {
            "played_at": (started - timedelta(minutes=1)).isoformat(),
            "track": {"uri": run["items"][1]["track_uri"]},
        },
    ]
    reviewed = reconcile(db, fake, run["id"])
    assert len(reviewed["observations"]) == 2
    assert reviewed["items"][0]["progress"] == "observed heard"
    assert reviewed["items"][1]["progress"] == "unheard"
    assert all(row["decision"] is None for row in reviewed["items"])
    assert reviewed["confirmed_position"] is None
    fake.history_error = True
    assert reconcile(db, fake, run["id"])["history_status"] == "unavailable"
    assert end(db, fake, settings, run["id"])["state"] == "ended"


def test_empty_and_partial_recent_history(tmp_path):
    db, session, fake, settings = setup(tmp_path, 1)
    run = start(db, fake, settings, session)
    assert reconcile(db, fake, run["id"])["history_status"] == "empty"
    played_at = (
        datetime.fromisoformat(run["started_at"]) + timedelta(seconds=1)
    ).isoformat()
    event = {"played_at": played_at, "track": {"uri": run["items"][0]["track_uri"]}}
    fake.recently_played = lambda *, before=None: {"items": [event], "next": "more"}
    with connect(db) as conn:
        conn.execute(
            "update discovery_listening_runs set ended_at=? where id=?",
            (
                (datetime.fromisoformat(played_at) + timedelta(minutes=1)).isoformat(),
                run["id"],
            ),
        )
    reviewed = reconcile(db, fake, run["id"])
    assert reviewed["history_status"] == "partial"
    assert len(reviewed["observations"]) == 1
    assert reviewed["confirmed_position"] is None


def test_restart_between_sqlite_acknowledgements(tmp_path):
    db, session, fake, settings = setup(tmp_path, 1)
    run = start(db, fake, settings, session)
    with connect(db) as conn:
        conn.execute(
            "update discovery_listening_runs set state='publishing' where id=?",
            (run["id"],),
        )
    assert start(db, fake, settings, session)["state"] == "active"
    assert len(fake.calls) == 1
    assert end(db, fake, settings, run["id"])["state"] == "ended"
    with connect(db) as conn:
        conn.execute(
            "update discovery_listening_runs set state='ending' where id=?",
            (run["id"],),
        )
    assert end(db, fake, settings, run["id"])["state"] == "ended"
    assert len(fake.calls) == 2


def test_cli_and_dashboard_controls(tmp_path, monkeypatch, capsys):
    db, session, fake, settings = setup(tmp_path, 1)
    config = tmp_path / "config.toml"
    config.write_text(
        """[spotify]
to-listen-playlist="listen"
test-playlist="test"
followed-labels-playlist="labels"
to-download-playlist="download"
acapella-playlist="acapella"
session-playlist="spotify:playlist:session"
scopes=["playlist-read-private"]
[collection]
music-dirs=[]
"""
    )
    monkeypatch.setattr("crate_digger.cli.get_spotify_client", lambda scope: fake)
    monkeypatch.setattr(
        "crate_digger.cli.SpotifyPlaylistAdapter", lambda client: client
    )
    assert (
        main(
            [
                "discover",
                "listening",
                "preview",
                str(session),
                "--db-path",
                str(db),
                "--config",
                str(config),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["remote_mutation"] is False
    client = TestClient(create_app(config_path=str(config), db_path=db))
    monkeypatch.setattr(
        "crate_digger.web.discover.get_spotify_client", lambda scope: fake
    )
    monkeypatch.setattr(
        "crate_digger.web.discover.SpotifyPlaylistAdapter", lambda client: client
    )
    page = client.get("/discover", params={"session_id": session})
    assert "Start listening" in page.text and "Finish session" in page.text
    preview_page = client.post(
        "/discover/listening", data={"session_id": session, "action": "preview"}
    )
    assert preview_page.status_code == 200
    assert "Publishable (1)" in preview_page.text and fake.calls == []
    response = client.post(f"/api/discover/sessions/{session}/listening/start", json={})
    assert response.status_code == 200
    run_id = response.json()["id"]
    assert (
        "Open Spotify playlist"
        in client.get("/discover", params={"session_id": session}).text
    )
    assert (
        client.get(f"/api/discover/sessions/{session}/listening").json()["runs"][0][
            "id"
        ]
        == run_id
    )
    assert (
        client.post(
            "/discover/listening",
            data={"session_id": session, "run_id": run_id, "action": "end"},
            follow_redirects=False,
        ).status_code
        == 303
    )
    assert list_runs(db, session)[0]["state"] == "ended"


def review_client(tmp_path, monkeypatch, db, fake):
    config = tmp_path / "config.toml"
    config.write_text(
        """[spotify]
to-listen-playlist="listen"
test-playlist="test"
followed-labels-playlist="labels"
to-download-playlist="download"
acapella-playlist="acapella"
session-playlist="spotify:playlist:session"
scopes=["playlist-read-private"]
[collection]
music-dirs=[]
"""
    )
    monkeypatch.setattr(
        "crate_digger.web.discover.get_spotify_client", lambda scope: fake
    )
    monkeypatch.setattr(
        "crate_digger.web.discover.SpotifyPlaylistAdapter", lambda client: client
    )
    return TestClient(create_app(config_path=str(config), db_path=db))


def test_dashboard_orders_thirty_tracks_down_columns(tmp_path, monkeypatch):
    db, session, fake, _settings = setup(tmp_path, 30)
    client = review_client(tmp_path, monkeypatch, db, fake)
    page = client.get("/discover", params={"session_id": session})
    assert page.status_code == 200
    assert re.findall(r'class="number">(\d+)</span>', page.text) == [
        f"{position:02d}" for position in range(1, 31)
    ]
    assert "--rows-desktop:10;--rows-tablet:15;--rows-mobile:30" in page.text
    stylesheet = client.get("/static/discover.css")
    assert stylesheet.status_code == 200
    assert "grid-auto-flow:column" in stylesheet.text
    assert fake.calls == []


def test_dashboard_saves_batch_and_unmarked_choices(tmp_path, monkeypatch):
    db, session, fake, settings = setup(tmp_path, 3)
    client = review_client(tmp_path, monkeypatch, db, fake)
    items = list_session_items(db, session)
    page = client.get("/discover", params={"session_id": session})
    assert page.status_code == 200
    assert page.text.count('<div class="song" data-pending="true">') == 3
    assert page.text.count('action="/discover/finish"') == 1
    assert 'action="/discover/feedback"' not in page.text
    assert all(item.decision is None for item in list_session_items(db, session))
    run = start(db, fake, settings, session)
    assert len(fake.calls) == 1

    malformed = client.post(
        "/discover/finish",
        content=f"session_id={session}&unmarked=later&decision_{items[0].item_id}=keep&decision_{items[0].item_id}=skip",
        headers={"content-type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    assert malformed.status_code == 303
    assert all(item.decision is None for item in list_session_items(db, session))
    assert list_runs(db, session)[0]["state"] == "active"

    previous_version = latest_snapshot_version(db)
    response = client.post(
        "/discover/finish",
        data={
            "session_id": session,
            "unmarked": "later",
            f"decision_{items[0].item_id}": "keep",
            f"decision_{items[1].item_id}": "skip",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "1 left for later" in response.text
    assert [item.decision for item in list_session_items(db, session)] == [
        "keep",
        "pass",
        None,
    ]
    current = get_session(db, session)
    assert current is not None and current.status == "open"
    assert latest_snapshot_version(db) == previous_version + 1
    assert list_runs(db, session)[0]["state"] == "ended"
    assert fake.uris == [] and len(fake.calls) == 2
    assert run["playlist_uri"] == URI

    client.post("/discover/finish", data={"session_id": session, "unmarked": "skip"})
    assert [item.decision for item in list_session_items(db, session)] == [
        "keep",
        "pass",
        "pass",
    ]
    current = get_session(db, session)
    assert current is not None and current.status == "completed"
    assert latest_snapshot_version(db) == previous_version + 2
    assert len(fake.calls) == 2
    assert "Build next 30-track session" in client.get("/discover").text


def test_dashboard_retry_cleanup_keeps_saved_reviews(tmp_path, monkeypatch):
    db, session, fake, settings = setup(tmp_path, 1)
    client = review_client(tmp_path, monkeypatch, db, fake)
    item = list_session_items(db, session)[0]
    start(db, fake, settings, session)
    fake.fail_after_replace = True
    response = client.post(
        "/discover/finish",
        data={
            "session_id": session,
            "unmarked": "later",
            f"decision_{item.item_id}": "keep",
        },
        follow_redirects=True,
    )
    assert "Reviews are saved; playlist cleanup needs a retry" in response.text
    assert list_session_items(db, session)[0].decision == "keep"
    assert list_runs(db, session)[0]["state"] == "ending"
    assert "Retry playlist cleanup" in response.text
    version = latest_snapshot_version(db)
    retry = client.post(
        "/discover/finish",
        data={"session_id": session, "unmarked": "later"},
        follow_redirects=True,
    )
    assert "Playlist cleared" in retry.text
    assert list_runs(db, session)[0]["state"] == "ended"
    assert latest_snapshot_version(db) == version
