import spotipy
from spotipy.oauth2 import SpotifyOAuth, CacheFileHandler

from typing import List, Dict
from dotenv import load_dotenv

from crate_digger.utils.logging import get_logger


logger = get_logger(__name__)


def get_spotify_client(scope: str) -> spotipy.Spotify:
    load_dotenv()

    cache_handler = CacheFileHandler(
        cache_path=f".spotipy_cache/.cache-{scope}"
    )
    auth = SpotifyOAuth(scope=scope, cache_handler=cache_handler)
    sp = spotipy.Spotify(auth_manager=auth)

    logger.info(f"Instantiated Spotipy client for scope {scope}")
    return sp


def add_new_releases_to_playlist(client: spotipy.Spotify, record_labels: List[str], target_playlist: str):
    uris_to_add = []

    for label in record_labels:
        relevant_releases = fetch_relevant_releases(client, label)
        n_releases = len(relevant_releases)
        logger.info(f"Fetched {n_releases} new release{'s' if n_releases != 1 else ''} for label {label}")

        for release in relevant_releases:
            release_tracks_to_add = get_track_uris_for_album(client, release["uri"])
            n_tracks = len(release_tracks_to_add)
            logger.info(f"Fetched {n_tracks} track{'s' if n_tracks != 1 else ''} for release {release['name']}")
            uris_to_add.extend(release_tracks_to_add)

    snapshot_id = client.playlist_add_items(target_playlist, uris_to_add)
    
    n_added_tracks = len(uris_to_add)
    logger.info(f"Added {n_added_tracks} new track{'s' if n_added_tracks != 1 else ''} to the playlist")

    return uris_to_add, snapshot_id


def fetch_relevant_releases(client: spotipy.Spotify, label: str) -> List[Dict]:
    new_releases = fetch_new_releases(client, label)
    relevant_releases = filter_relevant_releases(new_releases)
    return relevant_releases


def fetch_new_releases(client: spotipy.Spotify, label: str) -> List[Dict]:
    new_releases = client.search(f"label:{label} tag:new", type="album")["albums"]["items"]
    return new_releases


def filter_relevant_releases(releases: List[Dict]) -> List[Dict]:
    eps_and_singles = [release for release in releases if release["album_type"].lower() in ("ep", "single")]
    return eps_and_singles


def get_track_uris_for_album(client: spotipy.Spotify, album_uri: str) -> List[str]:
    album_tracks = client.album_tracks(album_uri)["items"]
    track_uris = [track["uri"] for track in album_tracks]
    return track_uris
