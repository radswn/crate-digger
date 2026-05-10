import pytest

from crate_digger.main.export_playlist import get_playlist_uri
from crate_digger.utils.config import SpotifyConfig


def make_spotify_config() -> SpotifyConfig:
    return {
        "to_listen_playlist": "pl:listen",
        "test_playlist": "pl:test",
        "followed_labels_playlist": "pl:labels",
        "to_download_playlist": "pl:download",
        "acapella_playlist": "pl:acapella",
        "scopes": ["playlist-read-private"],
    }


def test_get_playlist_uri_selects_to_download_playlist():
    config = make_spotify_config()

    assert get_playlist_uri(config, "to-download") == "pl:download"


def test_get_playlist_uri_selects_acapella_playlist():
    config = make_spotify_config()

    assert get_playlist_uri(config, "acapella") == "pl:acapella"


def test_get_playlist_uri_rejects_unknown_playlist():
    config = make_spotify_config()

    with pytest.raises(ValueError, match="Unknown playlist export"):
        get_playlist_uri(config, "other")
