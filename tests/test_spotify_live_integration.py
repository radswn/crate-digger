from collections.abc import Iterator
from pathlib import Path

import pytest
from spotipy import SpotifyException

from crate_digger.utils.config import get_settings
from crate_digger.utils.spotify import (
    fetch_playlist_album_uris,
    fetch_playlist_track_queries,
    format_track_query,
    get_spotify_client,
)


LIVE_TEST_TRACK_URI = "spotify:track:4cOdK2wGLETKBW3PvgPWqT"


def _spotify_cache_exists(scope: str) -> bool:
    project_root = Path(__file__).resolve().parents[1]
    cache_path = project_root / ".spotipy_cache" / f".cache-{scope.replace(',', '_')}"
    return cache_path.exists()


@pytest.fixture(scope="module")
def spotify_live():
    settings = get_settings()
    spotify_config = settings["spotify"]
    scope = " ".join(spotify_config["scopes"])

    if not _spotify_cache_exists(scope):
        pytest.skip(f"Spotify OAuth cache missing for scope {scope!r}")

    client = get_spotify_client(scope)
    playlist_uri = spotify_config["test_playlist"]

    try:
        playlist = client.playlist(
            playlist_uri,
            fields="id,name,owner(id),tracks(total)",
            additional_types=("track",),
        )
    except SpotifyException as exc:
        pytest.skip(f"Spotify test playlist is not accessible: {exc}")

    return client, playlist_uri, playlist


@pytest.fixture
def added_track_position(spotify_live) -> Iterator[int]:
    client, playlist_uri, _playlist = spotify_live
    before = client.playlist_items(
        playlist_uri,
        fields="total,snapshot_id",
        limit=1,
        additional_types=("track",),
    )
    added_position = before["total"]
    add_result = client.playlist_add_items(playlist_uri, [LIVE_TEST_TRACK_URI])

    try:
        yield added_position
    finally:
        client.playlist_remove_specific_occurrences_of_items(
            playlist_uri,
            [{"uri": LIVE_TEST_TRACK_URI, "positions": [added_position]}],
            snapshot_id=add_result.get("snapshot_id"),
        )


@pytest.mark.spotify_live
def test_configured_test_playlist_is_accessible(spotify_live):
    _client, playlist_uri, playlist = spotify_live

    assert playlist_uri.startswith("spotify:playlist:")
    assert playlist["id"] == playlist_uri.removeprefix("spotify:playlist:")
    assert playlist["tracks"]["total"] >= 0


@pytest.mark.spotify_live
def test_fetch_playlist_album_uris_reads_configured_test_playlist(spotify_live):
    client, playlist_uri, _playlist = spotify_live

    album_uris = fetch_playlist_album_uris(client, playlist_uri)

    assert len(album_uris) == len(set(album_uris))
    assert all(uri.startswith("spotify:album:") for uri in album_uris)


@pytest.mark.spotify_live
def test_fetch_playlist_track_queries_sees_track_added_to_test_playlist(
    spotify_live, added_track_position
):
    client, playlist_uri, _playlist = spotify_live
    expected_query = format_track_query(client.track(LIVE_TEST_TRACK_URI))

    queries = fetch_playlist_track_queries(client, playlist_uri)

    assert queries[added_track_position] == expected_query
