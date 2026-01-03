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
