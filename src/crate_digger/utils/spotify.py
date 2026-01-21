import re

import pandas as pd

from datetime import date, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Tuple
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
    """Create and return an authenticated Spotify client with cached OAuth token.

    Args:
        scope: OAuth scope string for Spotify API permissions

    Returns:
        Authenticated Spotify client instance
    """
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
    """Fetch past week releases from labels, deduplicate, and add to playlist.

    Args:
        client: Authenticated Spotify client
        record_labels: List of record label names to search
        target_playlist: Spotify playlist URI to add tracks to

    Returns:
        Dict mapping labels to their releases and tracks for notification
    """
    uris_to_add = []
    track_info_to_send: Dict[str, Dict[str, List[SpotifyTrack]]] = {}

    for label in record_labels:
        label_tracks_to_add: List[SpotifyTrack] = []
        relevant_releases = fetch_new_relevant_releases(client, label)

        if relevant_releases:
            track_info_to_send[label] = {}

        for release in relevant_releases:
            released_tracks = fetch_album_tracks(client, release)
            filtered_tracks = remove_extended_versions(released_tracks)
            label_tracks_to_add.extend(filtered_tracks)
            track_info_to_send[label][release["name"]] = released_tracks

        deduped_tracks = dedupe_tracks(label_tracks_to_add)
        uris_to_add.extend(extract_track_uris(deduped_tracks))

    if track_info_to_send:
        add_to_playlist(client, target_playlist, uris_to_add)

    return track_info_to_send


def fetch_new_relevant_releases(client: Spotify, label: str) -> List[SpotifyAlbum]:
    """Fetch past week releases from a label with exact label name matching.

    Args:
        client: Authenticated Spotify client
        label: Record label name to search for

    Returns:
        List of album objects released within the past week with exact label match
    """
    new_releases = fetch_new_releases(client, label)
    past_week_releases = filter_releases_by_date(new_releases)
    relevant_releases = filter_exact_label_releases(client, past_week_releases, label)

    n_releases = len(relevant_releases)
    logger.info(
        f"Fetched {n_releases} new {pluralize(n_releases, 'release')} for label {label}"
    )

    return relevant_releases


def fetch_new_releases(client: Spotify, label: str) -> List[SpotifyAlbum]:
    """Search Spotify for new releases tagged with the given label.

    Args:
        client: Authenticated Spotify client
        label: Record label name to search for

    Returns:
        List of album objects from Spotify search results
    """
    new_releases = client.search(
        f"label:{label.replace("'", '')} tag:new", limit=SEARCH_LIMIT, type="album"
    )["albums"]["items"]
    return new_releases


def batch(iterable: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    """Yield fixed-size slices from a sequence."""

    for i in range(0, len(iterable), size):
        yield iterable[i : i + size]


def filter_releases_by_date(
    releases: List[SpotifyAlbum], n_days: int = 7
) -> List[SpotifyAlbum]:
    """Filter releases to only those with release dates within the past n days.

    Args:
        releases: List of Spotify album objects

    Returns:
        Filtered list containing only releases within the past n days
    """
    target_date = date.today() - timedelta(days=n_days)
    return [r for r in releases if date.fromisoformat(r["release_date"]) >= target_date]


def filter_exact_label_releases(
    client: Spotify, releases: List[SpotifyAlbum], label: str
) -> List[SpotifyAlbum]:
    """Fetch full album details and filter to exact label name matches.

    Spotify search may return approximate matches; this verifies the label field.

    Args:
        client: Authenticated Spotify client
        releases: List of album objects from search results
        label: Exact label name to match

    Returns:
        List of albums with exact label match
    """
    release_uris = [r["uri"] for r in releases]
    releases_with_correct_label = []

    for uris_chunk in batch(release_uris, FETCH_BATCH_SIZE):
        full_albums = client.albums(uris_chunk)["albums"]
        releases_with_correct_label.extend(
            [a for a in full_albums if a["label"] == label]
        )

    return releases_with_correct_label


def fetch_album_tracks(client: Spotify, album: SpotifyAlbum) -> List[SpotifyTrack]:
    """Fetch all tracks for a given album.

    Args:
        client: Authenticated Spotify client
        album: Spotify album object

    Returns:
        List of track objects from the album
    """
    album_tracks: List[SpotifyTrack] = client.album_tracks(album["uri"])["items"]
    n_album_tracks = len(album_tracks)
    logger.info(
        f"Fetched {n_album_tracks} {pluralize(n_album_tracks, 'track')} for release {album['name']}"
    )

    return album_tracks


def extract_track_uris(tracks: List[SpotifyTrack]) -> List[str]:
    """Extract Spotify URIs from a list of track objects.

    Args:
        tracks: List of Spotify track objects

    Returns:
        List of Spotify track URIs
    """
    track_uris = [track["uri"] for track in tracks]
    return track_uris


def normalize_title(title: str) -> str:
    """Normalize a track title for comparison (lowercase, punctuation/whitespace collapsed)."""

    return " ".join(re.sub(r"[^\w\s]", "", title.lower()).split())


def is_extended_version(normalized_title: str) -> bool:
    """Check if a normalized title indicates an extended version.

    Args:
        normalized_title: Pre-normalized title string

    Returns:
        True if title contains 'extended' keyword
    """
    return "extended" in normalized_title


def base_title(normalized_title: str) -> str:
    """Extract base title by removing 'extended' suffixes.

    Args:
        normalized_title: Pre-normalized title string

    Returns:
        Base title with extended version indicators removed
    """
    return normalized_title.replace(" extended mix", "").replace(" extended", "")


def remove_extended_versions(tracks: List[SpotifyTrack]) -> List[SpotifyTrack]:
    """Drop extended versions when an original exists, without mutating input track dicts."""

    sorted_tracks = sorted(tracks, key=lambda t: len(t["name"]))

    unique_tracks: List[SpotifyTrack] = []
    seen_titles: set[str] = set()

    for track in sorted_tracks:
        normalized = normalize_title(track["name"])
        base = base_title(normalized)

        if is_extended_version(normalized) and base in seen_titles:
            continue

        seen_titles.add(base)
        unique_tracks.append(track)

    n_dropped_tracks = len(tracks) - len(unique_tracks)

    if n_dropped_tracks:
        logger.info(
            f"Dropped {n_dropped_tracks} extended {pluralize(n_dropped_tracks, 'mix', 'mixes')}"
        )

    return unique_tracks


def dedupe_tracks(tracks: Sequence[SpotifyTrack]) -> List[SpotifyTrack]:
    """Remove duplicate tracks based on (name, artists) key.

    Args:
        tracks: Sequence of Spotify track objects

    Returns:
        Deduplicated list of tracks
    """
    deduped: List[SpotifyTrack] = []
    seen: set[Tuple[str, Tuple[str, ...]]] = set()

    for track in tracks:
        key = (
            track["name"].lower(),
            tuple(artist["name"].lower() for artist in track["artists"]),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(track)

    return deduped


def add_to_playlist(client: Spotify, playlist_id: str, track_uris: List[str]) -> Dict:
    """Add tracks to a Spotify playlist.

    Args:
        client: Authenticated Spotify client
        playlist_id: Spotify playlist URI
        track_uris: List of track URIs to add

    Returns:
        Snapshot ID dict from Spotify API
    """
    snapshot_id = client.playlist_add_items(playlist_id, track_uris)

    n_added_tracks = len(track_uris)
    logger.info(
        f"Added {n_added_tracks} new {pluralize(n_added_tracks, 'track')} to the playlist"
    )

    return snapshot_id


def fetch_all_releases(client: Spotify, label: str) -> List[SpotifyAlbum]:
    """Fetch all releases for a label from BACKFILL_START_YEAR to present.

    Args:
        client: Authenticated Spotify client
        label: Record label name

    Returns:
        List of all album objects for the label
    """
    releases = []
    search_normalized_label = label.replace("'", "")

    for year in range(BACKFILL_START_YEAR, date.today().year + 1):
        len_beginning = len(releases)

        offset = 0

        page_of_found_releases = client.search(
            f"label:{search_normalized_label} year:{year}",
            type="album",
            offset=offset,
            limit=SEARCH_LIMIT,
        )["albums"]["items"]

        while page_of_found_releases:
            releases.extend(page_of_found_releases)
            offset += SEARCH_LIMIT

            if offset + SEARCH_LIMIT > MAX_OFFSET:
                break

            page_of_found_releases = client.search(
                f"label:{search_normalized_label} year:{year}",
                type="album",
                offset=offset,
                limit=SEARCH_LIMIT,
            )["albums"]["items"]
        len_end = len(releases)
        if len_end != len_beginning:
            logger.info(
                f"Fetched {len_end - len_beginning} {pluralize(len_end - len_beginning, 'release')} for year {year}"
            )

    logger.info(f"Fetched {len(releases)} releases in total")

    return releases


def parse_releases(releases: List[SpotifyAlbum]) -> pd.DataFrame:
    """Parse and clean release data into DataFrame with deduplication.

    Args:
        releases: List of Spotify album objects

    Returns:
        Cleaned and deduplicated DataFrame of releases sorted by date
    """
    release_df = pd.DataFrame(releases)

    size_beginning = release_df.shape[0]

    release_df = release_df.drop(
        ["artists", "images", "available_markets", "external_urls"], axis=1
    )

    release_df = release_df.drop_duplicates(["uri"])

    size_unique = release_df.shape[0]
    n_duplicates_dropped = size_beginning - size_unique
    logger.info(
        f"Dropped {n_duplicates_dropped} duplicate{'s' if n_duplicates_dropped != 1 else ''}"
    )

    release_df = release_df.sort_values("release_date")
    logger.info(
        f"{release_df.shape[0]} {pluralize(release_df.shape[0], 'release')} left"
    )

    return release_df


def fetch_all_release_uris(client: Spotify, label: str) -> pd.Series:
    """Fetch and parse all release URIs for a label.

    Args:
        client: Authenticated Spotify client
        label: Record label name

    Returns:
        Series of release URIs
    """
    all_releases = fetch_all_releases(client, label)
    parsed_df = parse_releases(all_releases)
    release_uris = parsed_df.uri
    return release_uris


def collect_tracks_from_albums(
    client: Spotify, album_uris: pd.Series, label: str
) -> List[str]:
    """Collect all track URIs from albums, filtering extended versions.

    Args:
        client: Authenticated Spotify client
        album_uris: Series of album URIs
        label: Exact label name to verify

    Returns:
        List of track URIs with extended versions removed
    """
    total_dropped = 0
    all_track_uris = []

    for uris_batch in batch(list(album_uris), FETCH_BATCH_SIZE):
        album_batch = [
            a for a in client.albums(uris_batch)["albums"] if a["label"] == label
        ]

        for album in album_batch:
            album_tracks = album["tracks"]["items"]
            unique_track_uris = [
                t["uri"] for t in album_tracks if "extended" not in t["name"].lower()
            ]
            total_dropped += len(album_tracks) - len(unique_track_uris)
            all_track_uris.extend(unique_track_uris)

    logger.info(
        f"{len(all_track_uris)} {pluralize(len(all_track_uris), 'track')} found"
    )
    logger.info(f"{total_dropped} {pluralize(total_dropped, 'track')} dropped")

    return all_track_uris


def create_playlists(
    client: Spotify, playlist_name: str, track_uris: List[str], step_size: int = 50
) -> None:
    """Create numbered playlists with batches of tracks and date range descriptions.

    Args:
        client: Authenticated Spotify client
        playlist_name: Base name for playlists (will be numbered)
        track_uris: List of track URIs to split into playlists
        step_size: Number of tracks per playlist (default 50)
    """
    for i in range(0, len(track_uris), step_size):
        full_playlist_name = f"{playlist_name} {(i // step_size) + 1:03d}"
        first_track_release_date = fetch_track_release_date(client, track_uris[i])
        last_track_release_date = fetch_track_release_date(
            client, track_uris[min(i + step_size, len(track_uris)) - 1]
        )

        playlist_description = (
            first_track_release_date + " - " + last_track_release_date
        )

        playlist = client.user_playlist_create(
            client.me()["id"],
            full_playlist_name,
            public=False,
            description=playlist_description,
        )
        logger.info(
            f"Created playlist {full_playlist_name} - {playlist['external_urls']['spotify']}"
        )

        client.playlist_add_items(playlist["uri"], track_uris[i : i + step_size])


def fetch_track_release_date(client: Spotify, track_uri: str) -> str:
    """Fetch the release date for a track.

    Args:
        client: Authenticated Spotify client
        track_uri: Spotify track URI

    Returns:
        Release date string from track's album
    """
    return client.track(track_uri)["album"]["release_date"]
