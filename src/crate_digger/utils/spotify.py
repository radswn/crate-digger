import os
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Tuple

import pandas as pd

from dotenv import load_dotenv

from spotipy import Spotify
from spotipy.oauth2 import CacheFileHandler, SpotifyOAuth

from crate_digger.constants import (
    BACKFILL_REQUEST_DELAY_SECONDS,
    BACKFILL_START_YEAR,
    FETCH_BATCH_SIZE,
    MAX_OFFSET,
    SEARCH_LIMIT,
)
from crate_digger.utils.logging import get_logger, pluralize
from crate_digger.utils.types import SpotifyAlbum, SpotifyTrack


logger = get_logger(__name__)

SPOTIFY_REQUEST_TIMEOUT_SECONDS = 10
SPOTIFY_RETRIES = 2
SPOTIFY_BACKOFF_FACTOR = 0.5
SPOTIFY_RETRY_STATUSES = (429, 500, 502, 503, 504)


class SpotifyTokenCacheError(RuntimeError):
    """Raised when non-interactive Spotify auth cannot use the token cache."""


class NonInteractiveSpotifyOAuth(SpotifyOAuth):
    """SpotifyOAuth variant that never prompts for a browser callback."""

    def get_auth_response(self, *args: Any, **kwargs: Any) -> str:
        raise SpotifyTokenCacheError(
            "Spotify OAuth requires an interactive login, but this client was "
            "created with browser auth disabled."
        )


class SpotifyCacheFileHandler(CacheFileHandler):
    """Spotipy cache handler with clearer cache read errors."""

    def __init__(self, cache_path: Path) -> None:
        super().__init__(cache_path=str(cache_path))

    def get_cached_token(self) -> dict[str, Any] | None:
        try:
            return super().get_cached_token()
        except (OSError, ValueError) as exc:
            raise SpotifyTokenCacheError(
                f"Could not read Spotify token cache {self.cache_path!r}: {exc}"
            ) from exc

    def save_token_to_cache(self, token_info: dict[str, Any]) -> None:
        super().save_token_to_cache(token_info)


def sleep_between_requests(delay_seconds: float) -> None:
    """Pause between API calls during broad backfills."""

    if delay_seconds > 0:
        time.sleep(delay_seconds)


def get_spotify_client(
    scope: str,
    *,
    allow_browser_auth: bool | None = None,
) -> Spotify:
    """Create and return an authenticated Spotify client with cached OAuth token.

    Args:
        scope: OAuth scope string for Spotify API permissions
        allow_browser_auth: Whether Spotipy may open an OAuth browser prompt. Defaults
            to true only for interactive local shells.

    Returns:
        Authenticated Spotify client instance
    """
    load_dotenv()

    project_root = Path(__file__).resolve().parents[3]
    normalized_scope = normalize_spotify_scope(scope)
    cache_path = project_root / ".spotipy_cache" / f".cache-{normalized_scope}"
    if allow_browser_auth is None:
        allow_browser_auth = _should_allow_browser_auth()

    cache_handler = SpotifyCacheFileHandler(cache_path=cache_path)
    if not allow_browser_auth:
        _assert_cached_token_covers_scope(
            cache_handler,
            scope=normalized_scope,
            cache_path=cache_path,
        )

    auth_cls = SpotifyOAuth if allow_browser_auth else NonInteractiveSpotifyOAuth
    auth = auth_cls(
        scope=normalized_scope,
        cache_handler=cache_handler,
        open_browser=allow_browser_auth,
    )
    sp = Spotify(
        auth_manager=auth,
        requests_timeout=SPOTIFY_REQUEST_TIMEOUT_SECONDS,
        retries=SPOTIFY_RETRIES,
        status_retries=SPOTIFY_RETRIES,
        backoff_factor=SPOTIFY_BACKOFF_FACTOR,
        status_forcelist=SPOTIFY_RETRY_STATUSES,
    )

    logger.info(f"Instantiated Spotipy client for scope {normalized_scope}")
    return sp


def normalize_spotify_scope(scope: str) -> str:
    """Return a stable scope string for cache names and OAuth validation."""

    return " ".join(scope.replace(",", " ").split())


def _should_allow_browser_auth() -> bool:
    if os.environ.get("CI"):
        return False
    return sys.stdin.isatty()


def _assert_cached_token_covers_scope(
    cache_handler: SpotifyCacheFileHandler,
    *,
    scope: str,
    cache_path: Path,
) -> None:
    token_info = cache_handler.get_cached_token()
    if token_info is None:
        raise SpotifyTokenCacheError(
            f"No Spotify token cache found at {cache_path}. Run an interactive "
            "Spotify login locally and upload the refreshed cache before running "
            "non-interactive jobs."
        )

    token_scope = token_info.get("scope")
    if isinstance(token_scope, str) and _scope_covers(token_scope, scope):
        return

    raise SpotifyTokenCacheError(
        f"Spotify token cache {cache_path} does not cover required scope "
        f"{scope!r}. Cached scope is {token_scope!r}."
    )


def _scope_covers(cached_scope: str, required_scope: str) -> bool:
    cached = set(normalize_spotify_scope(cached_scope).split())
    required = set(normalize_spotify_scope(required_scope).split())
    return required.issubset(cached)


def fetch_followed_labels_from_playlist(
    client: Spotify, playlist_uri: str
) -> List[str]:
    """Read followed record labels from a Spotify playlist of representative tracks.

    The playlist should contain at least one track released by each label to follow.
    Spotify playlist track payloads do not include full album label metadata, so this
    fetches album details in batches and deduplicates labels in playlist order.
    """

    if not playlist_uri.strip():
        raise ValueError("Set [spotify].followed-labels-playlist in config.toml")

    album_uris = fetch_playlist_album_uris(client, playlist_uri)
    labels: List[str] = []
    seen_labels: set[str] = set()

    for uris_chunk in batch(album_uris, FETCH_BATCH_SIZE):
        full_albums = client.albums(uris_chunk)["albums"]
        for album in full_albums:
            label = album.get("label")
            if not label or label in seen_labels:
                continue
            seen_labels.add(label)
            labels.append(label)

    logger.info(f"Fetched {len(labels)} followed {pluralize(len(labels), 'label')}")
    return labels


def fetch_playlist_album_uris(client: Spotify, playlist_uri: str) -> List[str]:
    """Fetch unique album URIs for tracks in a playlist, preserving playlist order."""

    album_uris: List[str] = []
    seen_album_uris: set[str] = set()
    offset = 0
    limit = 100

    while True:
        page = client.playlist_items(
            playlist_uri,
            fields="items(track(album(uri))),next",
            limit=limit,
            offset=offset,
            additional_types=("track",),
        )
        items = page["items"]

        for item in items:
            track = item.get("track") or {}
            album = track.get("album") or {}
            album_uri = album.get("uri")
            if not album_uri or album_uri in seen_album_uris:
                continue
            seen_album_uris.add(album_uri)
            album_uris.append(album_uri)

        if not page.get("next"):
            break

        offset += limit

    return album_uris


def format_track_query(track: Dict[str, Any]) -> str:
    """Format a Spotify track as an artist-title search query."""

    artists = track.get("artists") or []
    artist_names = ", ".join(
        name
        for artist in artists
        if isinstance(artist, dict) and isinstance(name := artist.get("name"), str)
    )
    return f"{artist_names} - {track['name']}"


def fetch_playlist_track_queries(client: Spotify, playlist_uri: str) -> List[str]:
    """Fetch playlist tracks as `artist - title` lines, preserving playlist order."""

    queries: List[str] = []
    offset = 0
    limit = 100

    while True:
        page = client.playlist_items(
            playlist_uri,
            fields="items(track(name,artists(name))),next",
            limit=limit,
            offset=offset,
            additional_types=("track",),
        )

        for item in page["items"]:
            track = item.get("track")
            if not track:
                continue
            queries.append(format_track_query(track))

        if not page.get("next"):
            break

        offset += limit

    return queries


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
    past_week_releases = filter_releases_by_date(new_releases, n_days=7)
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
    """Filter releases to only those with release dates in the past n days.

    Args:
        releases: List of Spotify album objects

    Returns:
        Filtered list containing releases from today minus n days through today
    """
    today = date.today()
    earliest_date = today - timedelta(days=n_days)
    return [
        r
        for r in releases
        if earliest_date <= date.fromisoformat(r["release_date"]) <= today
    ]


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


def normalize_playlist_label_text(text: str) -> str:
    """Normalize labels and playlist names for loose backfill matching."""

    return " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())


def playlist_name_matches_label(playlist_name: str, label: str) -> bool:
    """Return whether a playlist title looks like a backfill for the label."""

    normalized_playlist = normalize_playlist_label_text(playlist_name)
    normalized_label = normalize_playlist_label_text(label)

    if not normalized_label:
        return False

    pattern = rf"(^|\s){re.escape(normalized_label)}(\s|\d|$)"
    return re.search(pattern, normalized_playlist) is not None


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


def fetch_user_playlist_names(client: Spotify) -> List[str]:
    """Fetch all playlists visible to the current Spotify user."""

    playlist_names: List[str] = []
    offset = 0
    limit = 50

    while True:
        page = client.current_user_playlists(limit=limit, offset=offset)
        playlist_names.extend(
            playlist["name"] for playlist in page["items"] if playlist.get("name")
        )

        if not page.get("next"):
            break

        offset += limit

    return playlist_names


def label_has_backfill_playlist(client: Spotify, label: str) -> bool:
    """Check whether existing playlist names suggest this label was backfilled."""

    matching_playlist_names = [
        playlist_name
        for playlist_name in fetch_user_playlist_names(client)
        if playlist_name_matches_label(playlist_name, label)
    ]

    if matching_playlist_names:
        logger.info(
            f"Skipping backfill for {label}; found existing playlist {matching_playlist_names[0]!r}"
        )
        return True

    return False


def fetch_all_releases(
    client: Spotify,
    label: str,
    request_delay_seconds: float = BACKFILL_REQUEST_DELAY_SECONDS,
) -> List[SpotifyAlbum]:
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
        sleep_between_requests(request_delay_seconds)

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
            sleep_between_requests(request_delay_seconds)
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

    if release_df.empty:
        logger.info("0 releases left")
        return pd.DataFrame(columns=pd.Index(["uri", "release_date"]))

    size_beginning = release_df.shape[0]

    release_df = release_df.drop(
        ["artists", "images", "available_markets", "external_urls"],
        axis=1,
        errors="ignore",
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


def fetch_all_release_uris(
    client: Spotify,
    label: str,
    request_delay_seconds: float = BACKFILL_REQUEST_DELAY_SECONDS,
) -> pd.Series:
    """Fetch and parse all release URIs for a label.

    Args:
        client: Authenticated Spotify client
        label: Record label name

    Returns:
        Series of release URIs
    """
    all_releases = fetch_all_releases(client, label, request_delay_seconds)
    parsed_df = parse_releases(all_releases)
    release_uris = parsed_df.uri
    return release_uris


def collect_tracks_from_albums(
    client: Spotify,
    album_uris: pd.Series,
    label: str,
    request_delay_seconds: float = BACKFILL_REQUEST_DELAY_SECONDS,
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
        sleep_between_requests(request_delay_seconds)

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
    client: Spotify,
    playlist_name: str,
    track_uris: List[str],
    step_size: int = 50,
    request_delay_seconds: float = BACKFILL_REQUEST_DELAY_SECONDS,
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
        sleep_between_requests(request_delay_seconds)


def backfill_label_history(
    client: Spotify,
    label: str,
    request_delay_seconds: float = BACKFILL_REQUEST_DELAY_SECONDS,
) -> List[str]:
    """Backfill all known tracks for a label into numbered Spotify playlists."""

    release_uris = fetch_all_release_uris(client, label, request_delay_seconds)
    uris_to_add = collect_tracks_from_albums(
        client, release_uris, label, request_delay_seconds
    )

    if uris_to_add:
        create_playlists(
            client, label, uris_to_add, request_delay_seconds=request_delay_seconds
        )

    return uris_to_add


def fetch_track_release_date(client: Spotify, track_uri: str) -> str:
    """Fetch the release date for a track.

    Args:
        client: Authenticated Spotify client
        track_uri: Spotify track URI

    Returns:
        Release date string from track's album
    """
    return client.track(track_uri)["album"]["release_date"]
