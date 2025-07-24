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


def fetch_and_add(client: spotipy.Spotify, record_labels: List[str], target_playlist: str) -> Dict[str, Dict[str, List[Dict]]]:
    uris_to_add = []
    track_info_to_send = {}

    for label in record_labels:
        relevant_releases = fetch_relevant_releases(client, label)

        if relevant_releases:
            track_info_to_send[label] = {}

        for release in relevant_releases:
            released_tracks = get_album_tracks(client, release)
            tracks_to_add = remove_extended_versions(released_tracks)

            uris_to_add.extend(get_uris(tracks_to_add))
            track_info_to_send[label][release["name"]] = released_tracks

    add_to_playlist(client, target_playlist, uris_to_add)

    return track_info_to_send


def fetch_relevant_releases(client: spotipy.Spotify, label: str) -> List[Dict]:
    new_releases = fetch_new_releases(client, label)
    relevant_releases = filter_relevant_releases(new_releases)

    n_releases = len(relevant_releases)
    logger.info(f"Fetched {n_releases} new release{'s' if n_releases != 1 else ''} for label {label}")

    return relevant_releases


def fetch_new_releases(client: spotipy.Spotify, label: str) -> List[Dict]:
    new_releases = client.search(f"label:{label} tag:new", type="album")["albums"]["items"]
    return new_releases


def filter_relevant_releases(releases: List[Dict]) -> List[Dict]:
    eps_and_singles = [release for release in releases if release["album_type"].lower() in ("ep", "single")]
    return eps_and_singles


def get_album_tracks(client: spotipy.Spotify, album: Dict) -> List[Dict]:
    album_tracks = client.album_tracks(album["uri"])["items"]

    for track in album_tracks:
        track["is_added"] = False

    n_album_tracks = len(album_tracks)
    logger.info(f"Fetched {n_album_tracks} track{'s' if n_album_tracks != 1 else ''} for release {album['name']}")

    return album_tracks


def get_uris(tracks: List[Dict]) -> List[str]:
    track_uris = [track["uri"] for track in tracks]
    return track_uris


def remove_extended_versions(tracks: List[Dict]) -> List[Dict]:
    sorted_tracks = sorted(tracks, key=lambda t: t["name"])

    unique_tracks = []
    unique_track_titles = set()

    for track in sorted_tracks:
        if not ("Extended" in track["name"] and any(t in track["name"] for t in unique_track_titles)):
            unique_track_titles.add(track["name"])
            unique_tracks.append(track)
            track["is_added"] = True

    n_dropped_tracks = len(tracks) - len(unique_tracks)

    if n_dropped_tracks:
        logger.info(f"Dropped {n_dropped_tracks} extended mix{'es' if n_dropped_tracks != 1 else ''}")

    return unique_tracks


def add_to_playlist(client: spotipy.Spotify, playlist_id: str, track_uris: List[str]) -> Dict:
    snapshot_id = client.playlist_add_items(playlist_id, track_uris)

    n_added_tracks = len(track_uris)
    logger.info(f"Added {n_added_tracks} new track{'s' if n_added_tracks != 1 else ''} to the playlist")

    return snapshot_id
