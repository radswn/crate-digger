from dataclasses import replace
from pathlib import Path
from typing import Any

from crate_digger.discover.models import IndexSummary, SpotifyEntityTrack
from crate_digger.discover.repository import (
    list_local_spotify_tracks,
    list_release_ids_missing_labels,
    list_track_uris_missing_metadata,
    mark_followed_labels,
    upsert_release_metadata,
    upsert_spotify_tracks,
)
from crate_digger.utils.spotify import batch, playlist_name_matches_label


SPOTIFY_TRACK_PREFIX = "spotify:track:"
SPOTIFY_ALBUM_PREFIX = "spotify:album:"


def index_local_collection(
    db_path: Path,
    *,
    label_aliases: dict[str, str],
) -> IndexSummary:
    rows = list_local_spotify_tracks(db_path)
    tracks: list[SpotifyEntityTrack] = []
    skipped = 0
    for row in rows:
        track_id = spotify_id(str(row["spotify_uri"]), "track")
        if track_id is None:
            skipped += 1
            continue
        tracks.append(
            SpotifyEntityTrack(
                spotify_track_id=track_id,
                spotify_uri=f"spotify:track:{track_id}",
                title=str(row["title"] or Path(row["path"]).stem),
                artists=(),
                spotify_release_id=None,
                release_title=row["album"],
                release_date=row["release_date"],
                raw_label_name=None,
                local_track_path=str(row["path"]),
                external_url=f"https://open.spotify.com/track/{track_id}",
            )
        )
    created, already, aliases_applied = upsert_spotify_tracks(
        db_path,
        tracks,
        label_aliases=label_aliases,
        source="manual",
        provenance={"kind": "indexed_local_dj_library"},
    )
    return IndexSummary(
        tracks_inspected=len(rows),
        tracks_with_spotify_ids=len(tracks),
        created_candidates=created,
        already_indexed=already,
        skipped=skipped,
        missing_metadata=len(tracks),
        label_aliases_applied=aliases_applied,
    )


def enrich_missing_spotify_metadata(
    client: Any,
    db_path: Path,
    *,
    label_aliases: dict[str, str],
) -> IndexSummary:
    uris = list_track_uris_missing_metadata(db_path)
    payloads: list[dict[str, Any]] = []
    for uri_batch in batch(uris, 50):
        response = client.tracks(list(uri_batch))
        payloads.extend(
            item for item in response.get("tracks", []) if isinstance(item, dict)
        )

    release_ids = {
        album_id
        for item in payloads
        if isinstance(item.get("album"), dict)
        and isinstance(album_id := item["album"].get("id"), str)
    }
    release_ids.update(list_release_ids_missing_labels(db_path))
    albums: dict[str, dict[str, Any]] = {}
    for release_batch in batch(sorted(release_ids), 20):
        response = client.albums(list(release_batch))
        for album in response.get("albums", []):
            if isinstance(album, dict) and isinstance(album.get("id"), str):
                albums[album["id"]] = album

    release_result = upsert_release_metadata(
        db_path,
        [
            (
                album_id,
                str(album.get("name") or album_id),
                _string(album.get("release_date")),
                _string(album.get("label")),
            )
            for album_id, album in albums.items()
        ],
        label_aliases=label_aliases,
    )

    tracks = [
        parsed
        for item in payloads
        if (parsed := spotify_track_from_payload(item, albums=albums)) is not None
    ]
    created, already, aliases_applied = upsert_spotify_tracks(
        db_path,
        tracks,
        label_aliases=label_aliases,
        source="manual",
        provenance={"kind": "spotify_metadata_enrichment"},
    )
    linked_releases = sum(track.spotify_release_id is not None for track in tracks)
    linked_labels = release_result[0]
    return IndexSummary(
        tracks_inspected=len(uris),
        tracks_with_spotify_ids=len(tracks),
        tracks_linked_to_releases=linked_releases,
        tracks_linked_to_labels=linked_labels,
        created_candidates=created,
        already_indexed=already,
        missing_metadata=len(uris) - linked_labels,
        label_aliases_applied=aliases_applied + release_result[1],
    )


def index_existing_playlists(
    client: Any,
    db_path: Path,
    *,
    followed_labels: list[str],
    configured_playlists: dict[str, str],
    label_aliases: dict[str, str],
) -> IndexSummary:
    mark_followed_labels(db_path, followed_labels, label_aliases)
    playlists = _user_playlists(client)
    inspected = 0
    spotify_count = 0
    created = 0
    already = 0
    skipped = 0
    aliases_applied = 0

    configured_types = {
        configured_playlists.get("to_download", ""): "curated_positive",
        configured_playlists.get("acapella", ""): "curated_positive",
        configured_playlists.get("to_listen", ""): "ingested_listening",
        configured_playlists.get("followed_labels", ""): "followed_label_seed",
    }
    configured_types.pop("", None)

    for playlist in playlists:
        uri = playlist.get("uri")
        name = playlist.get("name")
        if not isinstance(uri, str) or not isinstance(name, str):
            continue
        matched_label = next(
            (
                label
                for label in followed_labels
                if playlist_name_matches_label(name, label)
            ),
            None,
        )
        source_type = configured_types.get(uri)
        if source_type is None and matched_label is None:
            continue
        if source_type is None:
            source_type = "followed_label_catalog"
        discovery_source = (
            "followed_label"
            if matched_label is not None or source_type == "followed_label_seed"
            else "manual"
        )
        payloads = _playlist_tracks(client, uri)
        inspected += len(payloads)
        parsed_tracks: list[SpotifyEntityTrack] = []
        for payload in payloads:
            parsed = spotify_track_from_payload(payload)
            if parsed is None:
                skipped += 1
                continue
            if matched_label and parsed.raw_label_name is None:
                parsed = replace(parsed, raw_label_name=matched_label)
            parsed_tracks.append(parsed)
        spotify_count += len(parsed_tracks)
        result = upsert_spotify_tracks(
            db_path,
            parsed_tracks,
            label_aliases=label_aliases,
            source=discovery_source,
            provenance={
                "kind": source_type,
                "playlist_uri": uri,
                "playlist_name": name,
                "followed_label": matched_label,
            },
            playlist=(uri, name, source_type),
        )
        created += result[0]
        already += result[1]
        aliases_applied += result[2]

    return IndexSummary(
        tracks_inspected=inspected,
        tracks_with_spotify_ids=spotify_count,
        created_candidates=created,
        already_indexed=already,
        skipped=skipped,
        missing_metadata=len(list_track_uris_missing_metadata(db_path)),
        label_aliases_applied=aliases_applied,
    )


def spotify_track_from_payload(
    payload: dict[str, Any],
    *,
    albums: dict[str, dict[str, Any]] | None = None,
) -> SpotifyEntityTrack | None:
    track_id = payload.get("id")
    uri = payload.get("uri")
    title = payload.get("name")
    if not isinstance(track_id, str):
        track_id = spotify_id(str(uri or ""), "track")
    if not track_id or not isinstance(title, str):
        return None
    if not isinstance(uri, str):
        uri = f"spotify:track:{track_id}"
    artists = tuple(
        (artist["id"], artist["name"])
        for artist in payload.get("artists", [])
        if isinstance(artist, dict)
        and isinstance(artist.get("id"), str)
        and isinstance(artist.get("name"), str)
    )
    raw_album = payload.get("album")
    album: dict[str, Any] = raw_album if isinstance(raw_album, dict) else {}
    release_id = album.get("id")
    if not isinstance(release_id, str):
        release_id = spotify_id(str(album.get("uri", "")), "album")
    full_album: dict[str, Any] = (albums or {}).get(release_id or "", album)
    external_urls = payload.get("external_urls")
    external_url = (
        external_urls.get("spotify") if isinstance(external_urls, dict) else None
    )
    return SpotifyEntityTrack(
        spotify_track_id=track_id,
        spotify_uri=uri,
        title=title,
        artists=artists,
        spotify_release_id=release_id,
        release_title=_string(full_album.get("name") or album.get("name")),
        release_date=_string(
            full_album.get("release_date") or album.get("release_date")
        ),
        raw_label_name=_string(full_album.get("label")),
        track_number=_integer(payload.get("track_number")),
        disc_number=_integer(payload.get("disc_number")),
        duration_ms=_integer(payload.get("duration_ms")),
        preview_url=_string(payload.get("preview_url")),
        external_url=_string(external_url)
        or f"https://open.spotify.com/track/{track_id}",
    )


def spotify_id(value: str, entity_type: str) -> str | None:
    prefix = f"spotify:{entity_type}:"
    if value.startswith(prefix):
        candidate = value.removeprefix(prefix).split("?", maxsplit=1)[0]
        return candidate or None
    marker = f"open.spotify.com/{entity_type}/"
    if marker in value:
        candidate = value.split(marker, maxsplit=1)[1].split("?", maxsplit=1)[0]
        return candidate.strip("/") or None
    return None


def _user_playlists(client: Any) -> list[dict[str, Any]]:
    playlists: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = client.current_user_playlists(limit=50, offset=offset)
        items = [item for item in page.get("items", []) if isinstance(item, dict)]
        playlists.extend(items)
        if not page.get("next"):
            return playlists
        offset += 50


def _playlist_tracks(client: Any, playlist_uri: str) -> list[dict[str, Any]]:
    tracks: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = client.playlist_items(
            playlist_uri,
            limit=100,
            offset=offset,
            additional_types=("track",),
        )
        for item in page.get("items", []):
            if isinstance(item, dict) and isinstance(item.get("track"), dict):
                tracks.append(item["track"])
        if not page.get("next"):
            return tracks
        offset += 100


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
