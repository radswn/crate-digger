import re

import pandas as pd

from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List
from dotenv import load_dotenv

from spotipy import Spotify
from spotipy.oauth2 import CacheFileHandler, SpotifyOAuth

from crate_digger.constants import (
    BACKFILL_START_YEAR,
    FETCH_BATCH_SIZE,
    MAX_OFFSET,
    SEARCH_LIMIT,
)
from crate_digger.utils.logging import get_logger, pluralize
from crate_digger.utils.types import SpotifyAlbum, SpotifyTrack


logger = get_logger(__name__)


def get_spotify_client(scope: str) -> Spotify:
    load_dotenv()

    project_root = Path(__file__).resolve().parents[3]
    cache_path = project_root / ".spotipy_cache" / f".cache-{scope}"

    cache_handler = CacheFileHandler(cache_path=cache_path)
    auth = SpotifyOAuth(scope=scope, cache_handler=cache_handler)
    sp = Spotify(auth_manager=auth)

    logger.info(f"Instantiated Spotipy client for scope {scope}")
    return sp


def fetch_and_add(
    client: Spotify,
    record_labels: List[str],
    target_playlist: str,
) -> Dict[str, Dict[str, List[SpotifyTrack]]]:
    uris_to_add = []
    track_info_to_send: Dict[str, Dict[str, List[SpotifyTrack]]] = {}

    for label in record_labels:
        label_tracks_to_add = []
        relevant_releases = fetch_new_relevant_releases(client, label)

        if relevant_releases:
            track_info_to_send[label] = {}

        for release in relevant_releases:
            released_tracks = fetch_album_tracks(client, release)
            label_tracks_to_add.extend(remove_extended_versions(released_tracks))
            track_info_to_send[label][release["name"]] = released_tracks

        logged_tracks = set()
        tracks_to_add = []

        for track in label_tracks_to_add:
            track_key = (track["name"].lower(), tuple(artist["name"].lower() for artist in track["artists"]))
            if track_key in logged_tracks:
                continue
            logged_tracks.add(track_key)
            tracks_to_add.append(track)

        uris_to_add.extend(extract_track_uris(tracks_to_add))

    if track_info_to_send:
        add_to_playlist(client, target_playlist, uris_to_add)

    return track_info_to_send


def fetch_new_relevant_releases(client: Spotify, label: str) -> List[SpotifyAlbum]:
    new_releases = fetch_new_releases(client, label)
    yesterdays_releases = filter_yesterdays_releases(new_releases)
    relevant_releases = filter_exact_label_releases(client, yesterdays_releases, label)

    n_releases = len(relevant_releases)
    logger.info(f"Fetched {n_releases} new {pluralize(n_releases, 'release')} for label {label}")

    return relevant_releases


def fetch_new_releases(client: Spotify, label: str) -> List[SpotifyAlbum]:
    new_releases = client.search(
        f"label:{label.replace("'", '')} tag:new", limit=SEARCH_LIMIT, type="album"
    )["albums"]["items"]
    return new_releases


def filter_yesterdays_releases(releases: List[SpotifyAlbum]) -> List[SpotifyAlbum]:
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    return [r for r in releases if r["release_date"] == yesterday]


def filter_exact_label_releases(client: Spotify, releases: List[SpotifyAlbum], label: str) -> List[SpotifyAlbum]:
    release_uris = [r["uri"] for r in releases]
    releases_with_correct_label = []

    for i in range(0, len(release_uris), FETCH_BATCH_SIZE):
        full_albums = client.albums(release_uris[i:i+FETCH_BATCH_SIZE])["albums"]
        releases_with_correct_label.extend([a for a in full_albums if a["label"] == label])

    return releases_with_correct_label


def fetch_album_tracks(client: Spotify, album: SpotifyAlbum) -> List[SpotifyTrack]:
    album_tracks: List[SpotifyTrack] = client.album_tracks(album["uri"])["items"]
    n_album_tracks = len(album_tracks)
    logger.info(f"Fetched {n_album_tracks} {pluralize(n_album_tracks, 'track')} for release {album['name']}")

    return album_tracks


def extract_track_uris(tracks: List[SpotifyTrack]) -> List[str]:
    track_uris = [track["uri"] for track in tracks]
    return track_uris


def _normalized_title(title: str) -> str:
    """Normalize a track title for comparison (lowercase, punctuation/whitespace collapsed)."""

    return " ".join(re.sub(r"[^\w\s]", "", title.lower()).split())


def _is_extended_version(normalized_title: str) -> bool:
    return "extended" in normalized_title


def _base_title(normalized_title: str) -> str:
    return normalized_title.replace(" extended mix", "").replace(" extended", "")


def remove_extended_versions(tracks: List[SpotifyTrack]) -> List[SpotifyTrack]:
    """Drop extended versions when an original exists, without mutating input track dicts."""

    sorted_tracks = sorted(tracks, key=lambda t: len(t["name"]))

    unique_tracks: List[SpotifyTrack] = []
    seen_titles = set()

    for track in sorted_tracks:
        normalized = _normalized_title(track["name"])
        base = _base_title(normalized)

        if _is_extended_version(normalized) and base in seen_titles:
            continue

        seen_titles.add(base)
        unique_tracks.append(track)

    n_dropped_tracks = len(tracks) - len(unique_tracks)

    if n_dropped_tracks:
        logger.info(f"Dropped {n_dropped_tracks} extended {pluralize(n_dropped_tracks, 'mix', 'mixes')}")

    return unique_tracks


def add_to_playlist(client: Spotify, playlist_id: str, track_uris: List[str]) -> Dict:
    snapshot_id = client.playlist_add_items(playlist_id, track_uris)

    n_added_tracks = len(track_uris)
    logger.info(f"Added {n_added_tracks} new {pluralize(n_added_tracks, 'track')} to the playlist")

    return snapshot_id


def fetch_all_releases(client: Spotify, label: str) -> List[SpotifyAlbum]:
    releases = []
    search_normalized_label = label.replace('\'', '')

    for year in range(BACKFILL_START_YEAR, date.today().year + 1):
        len_beginning = len(releases)

        offset = 0

        page_of_found_releases = client.search(f"label:{search_normalized_label} year:{year}", type="album", offset=offset, limit=SEARCH_LIMIT)["albums"]["items"]

        while page_of_found_releases:
            releases.extend(page_of_found_releases)
            offset += SEARCH_LIMIT

            if offset + SEARCH_LIMIT > MAX_OFFSET: break

            page_of_found_releases = client.search(f"label:{search_normalized_label} year:{year}", type="album", offset=offset, limit=SEARCH_LIMIT)["albums"]["items"]
        len_end = len(releases)
        if len_end != len_beginning:
            logger.info(f"Fetched {len_end - len_beginning} {pluralize(len_end - len_beginning, 'release')} for year {year}")

    logger.info(f"Fetched {len(releases)} releases in total")

    return releases


def parse_releases(releases: List[SpotifyAlbum]) -> pd.DataFrame:
    release_df = pd.DataFrame(releases)

    size_beginning = release_df.shape[0]

    release_df = release_df.drop(["artists", "images", "available_markets", "external_urls"], axis=1)

    release_df = release_df.drop_duplicates(["uri"])

    size_unique = release_df.shape[0]
    n_duplicates_dropped = size_beginning - size_unique
    logger.info(f"Dropped {n_duplicates_dropped} duplicate{'s' if n_duplicates_dropped != 1 else ''}")

    release_df = release_df.sort_values("release_date")
    logger.info(f"{release_df.shape[0]} {pluralize(release_df.shape[0], 'release')} left")

    return release_df


def fetch_all_release_uris(client: Spotify, label: str) -> pd.Series:
    all_releases = fetch_all_releases(client, label)
    parsed_df = parse_releases(all_releases)
    release_uris = parsed_df.uri
    return release_uris


def collect_tracks_from_albums(client: Spotify, album_uris: pd.Series, label: str) -> List[str]:
    total_dropped = 0
    all_track_uris = []

    for i in range(0, len(album_uris), FETCH_BATCH_SIZE):
        uris_batch = album_uris[i:i+FETCH_BATCH_SIZE]
        album_batch = [a for a in client.albums(uris_batch)["albums"] if a["label"] == label]

        for album in album_batch:
            album_tracks = album["tracks"]["items"]
            unique_track_uris = [t["uri"] for t in album_tracks if "extended" not in t["name"].lower()]
            total_dropped += len(album_tracks) - len(unique_track_uris)
            all_track_uris.extend(unique_track_uris)

    logger.info(f"{len(all_track_uris)} {pluralize(len(all_track_uris), 'track')} found")
    logger.info(f"{total_dropped} {pluralize(total_dropped, 'track')} dropped")

    return all_track_uris


def create_playlists(client: Spotify, playlist_name: str, track_uris: List[str], step_size:int=50) -> None:
    for i in range(0, len(track_uris), step_size):
        full_playlist_name = f"{playlist_name} {(i // step_size) + 1:03d}"
        first_track_release_date = fetch_track_release_date(client, track_uris[i])
        last_track_release_date = fetch_track_release_date(client, track_uris[min(i + step_size, len(track_uris)) - 1])

        playlist_description = first_track_release_date + " - " + last_track_release_date

        playlist = client.user_playlist_create(client.me()["id"], full_playlist_name, public=False, description=playlist_description)
        logger.info(f"Created playlist {full_playlist_name} - {playlist['external_urls']['spotify']}")

        client.playlist_add_items(playlist["uri"], track_uris[i:i+step_size])


def fetch_track_release_date(client: Spotify, track_uri: str) -> str:
    return client.track(track_uri)["album"]["release_date"]
