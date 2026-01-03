from unittest.mock import MagicMock

import crate_digger.utils.spotify as m


def _mk_track(name: str, artist: str, uri: str, album_name: str = "Album1"):
    return {
        "name": name,
        "artists": [{"name": artist}],
        "uri": uri,
        "album": {"name": album_name},
    }


def test_fetch_and_add_end_to_end(monkeypatch):
    client = MagicMock()
    release = {"uri": "album:1", "name": "Album1"}

    # Control the releases and tracks returned through the pipeline
    monkeypatch.setattr(
        m,
        "fetch_new_relevant_releases",
        lambda c, label: [release],
    )

    tracks = [
        _mk_track("Track", "A", "u1"),
        _mk_track("Track (Extended Mix)", "A", "u2"),
        _mk_track("Other", "A", "u3"),
        _mk_track("Other", "A", "u3dup"),
    ]

    def _fetch_album_tracks(c, album):
        assert album is release
        return tracks

    monkeypatch.setattr(m, "fetch_album_tracks", _fetch_album_tracks)

    client.playlist_add_items.return_value = "snap123"

    out = m.fetch_and_add(client, ["Label"], target_playlist="plid")

    # Extended mix removed, duplicate "Other" deduped by name+artist
    client.playlist_add_items.assert_called_once_with("plid", ["u1", "u3"])

    # Track info is grouped per label and album name
    assert out == {"Label": {"Album1": tracks}}


def test_fetch_and_add_no_releases_found(monkeypatch):
    client = MagicMock()

    monkeypatch.setattr(m, "fetch_new_relevant_releases", lambda c, label: [])

    out = m.fetch_and_add(client, ["Label"], target_playlist="plid")

    # No playlist additions when no releases
    client.playlist_add_items.assert_not_called()
    assert out == {}


def test_fetch_and_add_all_extended_versions(monkeypatch):
    client = MagicMock()
    release = {"uri": "album:1", "name": "Album1"}

    monkeypatch.setattr(m, "fetch_new_relevant_releases", lambda c, label: [release])

    tracks = [
        _mk_track("Track (Extended Mix)", "A", "u1"),
        _mk_track("Other Extended", "B", "u2"),
    ]
    monkeypatch.setattr(m, "fetch_album_tracks", lambda c, album: tracks)

    client.playlist_add_items.return_value = "snap123"

    out = m.fetch_and_add(client, ["Label"], target_playlist="plid")

    # Extended versions kept when no originals exist
    # Reverse order because of sorting by ascending name length
    client.playlist_add_items.assert_called_once_with("plid", ["u2", "u1"])


def test_fetch_and_add_multiple_labels(monkeypatch):
    client = MagicMock()

    def mock_fetch_releases(c, label):
        return [{"uri": f"album:{label}", "name": f"Album-{label}"}]

    def mock_fetch_tracks(c, album):
        return [_mk_track("Track", "A", f"uri:{album['name']}")]

    monkeypatch.setattr(m, "fetch_new_relevant_releases", mock_fetch_releases)
    monkeypatch.setattr(m, "fetch_album_tracks", mock_fetch_tracks)
    client.playlist_add_items.return_value = "snap123"

    out = m.fetch_and_add(client, ["Label1", "Label2"], target_playlist="plid")

    # Both labels processed and combined
    assert "Label1" in out
    assert "Label2" in out
    client.playlist_add_items.assert_called_once()
    call_args = client.playlist_add_items.call_args[0]
    assert len(call_args[1]) == 2  # 2 tracks total


def test_create_playlists_end_to_end(monkeypatch):
    client = MagicMock()
    client.me.return_value = {"id": "user123"}

    created_playlists = []

    def mock_create(user_id, name, public, description):
        pl = {"uri": f"pl:{len(created_playlists)}", "external_urls": {"spotify": "http://x"}}
        created_playlists.append({"name": name, "description": description})
        return pl

    client.user_playlist_create.side_effect = mock_create

    release_dates = {"t1": "2020-01-01", "t2": "2020-01-02", "t3": "2020-01-03"}
    monkeypatch.setattr(m, "fetch_track_release_date", lambda c, uri: release_dates[uri])

    m.create_playlists(client, "My Label", ["t1", "t2", "t3"], step_size=2)

    # Created 2 playlists (2 tracks each, then 1)
    assert len(created_playlists) == 2
    assert created_playlists[0]["name"] == "My Label 001"
    assert created_playlists[0]["description"] == "2020-01-01 - 2020-01-02"
    assert created_playlists[1]["name"] == "My Label 002"
    assert created_playlists[1]["description"] == "2020-01-03 - 2020-01-03"


def test_batch_pagination_exact_boundary(monkeypatch):
    """Test that batch helper handles exact multiples correctly."""
    items = [f"uri:{i}" for i in range(20)]
    batches = list(m.batch(items, 20))

    assert len(batches) == 1
    assert len(batches[0]) == 20
