"""Download intent and its audit trail, separate from taste feedback."""

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Literal, cast

from crate_digger.discover.indexer import _playlist_tracks, spotify_track_from_payload
from crate_digger.discover.models import SpotifyEntityTrack
from crate_digger.discover.repository import connect, now_iso, upsert_spotify_tracks

WishlistStatus = Literal[
    "wanted",
    "searching",
    "acquired",
    "needs_curation",
    "ready",
    "unavailable",
    "removed",
]


def set_wanted(
    db_path: Path,
    spotify_track_id: str,
    wanted: bool,
    *,
    source: str = "dashboard",
) -> bool:
    with connect(db_path) as conn:
        return _set_wanted(conn, spotify_track_id, wanted, source=source)


def _set_wanted(
    conn: sqlite3.Connection,
    spotify_track_id: str,
    wanted: bool,
    *,
    source: str,
    restore_removed: bool = True,
) -> bool:
    track = conn.execute(
        "select local_track_path from discovery_tracks where spotify_track_id = ?",
        (spotify_track_id,),
    ).fetchone()
    if track is None:
        raise KeyError(f"Unknown Spotify track: {spotify_track_id}")
    old = conn.execute(
        "select status from discovery_wishlist where spotify_track_id = ?",
        (spotify_track_id,),
    ).fetchone()
    previous = str(old["status"]) if old else None
    if wanted:
        if previous == "removed" and not restore_removed:
            return False
        status = (
            ("needs_curation" if track["local_track_path"] else "wanted")
            if previous in (None, "removed")
            else previous
        )
    else:
        status = "removed"
    if status == previous or (not wanted and previous is None):
        return False
    now = now_iso()
    conn.execute(
        """insert into discovery_wishlist
           (spotify_track_id, status, source, created_at, updated_at)
           values (?, ?, ?, ?, ?)
           on conflict(spotify_track_id) do update set
             status = excluded.status, source = excluded.source,
             updated_at = excluded.updated_at""",
        (spotify_track_id, status, source, now, now),
    )
    conn.execute(
        """insert into discovery_wishlist_events
           (spotify_track_id, from_status, to_status, source, created_at)
           values (?, ?, ?, ?, ?)""",
        (spotify_track_id, previous, status, source, now),
    )
    return True


def set_progress(
    db_path: Path,
    spotify_track_id: str,
    status: WishlistStatus,
    *,
    note: str | None = None,
) -> bool:
    """Record a manual search/retry decision; never infer acquisition from a filename."""
    allowed = {
        "wanted": {"searching", "unavailable"},
        "searching": {"wanted", "unavailable"},
        "unavailable": {"wanted", "searching"},
    }
    with connect(db_path) as conn:
        row = conn.execute(
            "select status from discovery_wishlist where spotify_track_id = ?",
            (spotify_track_id,),
        ).fetchone()
        previous = str(row["status"]) if row else None
        if previous == status:
            return False
        if status not in allowed.get(previous or "", set()):
            raise ValueError(
                f"Cannot move wishlist track from {previous or 'absent'} to {status}"
            )
        now = now_iso()
        conn.execute(
            "update discovery_wishlist set status = ?, updated_at = ? where spotify_track_id = ?",
            (status, now, spotify_track_id),
        )
        conn.execute(
            """insert into discovery_wishlist_events
               (spotify_track_id, from_status, to_status, source, note, created_at)
               values (?, ?, ?, 'dashboard', ?, ?)""",
            (spotify_track_id, previous, status, note, now),
        )
    return True


def wishlist_statuses(db_path: Path, spotify_track_ids: list[str]) -> dict[str, str]:
    if not spotify_track_ids:
        return {}
    placeholders = ",".join("?" for _ in spotify_track_ids)
    with connect(db_path) as conn:
        rows = conn.execute(
            f"select spotify_track_id, status from discovery_wishlist where spotify_track_id in ({placeholders})",
            spotify_track_ids,
        ).fetchall()
    return {str(row["spotify_track_id"]): str(row["status"]) for row in rows}


def list_wishlist(
    db_path: Path, *, include_removed: bool = False
) -> list[dict[str, Any]]:
    with connect(db_path) as conn:
        rows = conn.execute(
            """select w.spotify_track_id, w.status, w.source, w.created_at, w.updated_at,
                      t.title, t.spotify_uri, t.external_url, t.local_track_path,
                      coalesce(a.name, 'Unknown artist') as artist_name,
                      r.title as release_title, c.feedback
               from discovery_wishlist w
               join discovery_tracks t on t.spotify_track_id = w.spotify_track_id
               left join discovery_artists a on a.spotify_artist_id = t.primary_artist_id
               left join discovery_releases r on r.spotify_release_id = t.release_id
               left join discovery_candidates c on c.spotify_track_id = w.spotify_track_id
               where (? or w.status != 'removed')
               order by w.updated_at desc, lower(t.title), w.spotify_track_id""",
            (include_removed,),
        ).fetchall()
    return [dict(row) for row in rows]


def wishlist_events(db_path: Path, spotify_track_id: str) -> list[dict[str, Any]]:
    with connect(db_path) as conn:
        rows = conn.execute(
            """select from_status, to_status, source, note, created_at
               from discovery_wishlist_events where spotify_track_id = ? order by id desc""",
            (spotify_track_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def search_local_matches(
    db_path: Path, spotify_track_id: str, query: str
) -> list[dict[str, Any]]:
    if not query.strip():
        return []
    with connect(db_path) as conn:
        _require_active_wishlist(conn, spotify_track_id)
        needle = (
            "%"
            + query.strip()
            .casefold()
            .replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
            + "%"
        )
        rows = conn.execute(
            """select path, title, artist, album, duration_seconds, bitrate, audio_format,
                      spotify_uri, size, mtime_ns
               from tracks where lower(coalesce(title, '') || ' ' || coalesce(artist, '') || ' ' || path)
                   like ? escape '\\'
               order by lower(coalesce(artist, '')), lower(coalesce(title, '')), lower(path)
               limit 30""",
            (needle,),
        ).fetchall()
    return [dict(row) for row in rows]


def preview_local_match(
    db_path: Path, spotify_track_id: str, path: str
) -> dict[str, Any]:
    with connect(db_path) as conn:
        return _local_match_preview(conn, spotify_track_id, path)


def _require_active_wishlist(
    conn: sqlite3.Connection, spotify_track_id: str
) -> sqlite3.Row:
    row = conn.execute(
        """select w.status, t.spotify_uri, t.title as spotify_title,
                  coalesce(a.name, 'Unknown artist') as spotify_artist, t.local_track_path
           from discovery_wishlist w
           join discovery_tracks t on t.spotify_track_id = w.spotify_track_id
           left join discovery_artists a on a.spotify_artist_id = t.primary_artist_id
           where w.spotify_track_id = ?""",
        (spotify_track_id,),
    ).fetchone()
    if row is None or row["status"] == "removed":
        raise KeyError("Track is not on the active wishlist")
    return row


def _local_match_preview(
    conn: sqlite3.Connection, spotify_track_id: str, path: str
) -> dict[str, Any]:
    wanted = _require_active_wishlist(conn, spotify_track_id)
    local = conn.execute(
        """select path, title, artist, album, duration_seconds, bitrate,
                  audio_format, spotify_uri, size, mtime_ns
           from tracks where path = ?""",
        (path,),
    ).fetchone()
    if local is None:
        raise ValueError("Local file is not in the collection index")
    if local["spotify_uri"] and local["spotify_uri"] != wanted["spotify_uri"]:
        raise ValueError("Local file is already linked to another Spotify recording")
    if wanted["local_track_path"] and wanted["local_track_path"] != path:
        raise ValueError("Wanted recording is already linked to another local file")
    owner = conn.execute(
        "select spotify_track_id from discovery_tracks where local_track_path = ?",
        (path,),
    ).fetchone()
    if owner is not None and owner["spotify_track_id"] != spotify_track_id:
        raise ValueError("Local file is already linked to another wanted recording")
    if local["size"] is None or local["mtime_ns"] is None:
        raise ValueError("Local file needs a fresh collection scan before matching")
    try:
        stat = Path(path).stat()
    except OSError as error:
        raise ValueError("Local file is missing or unreadable") from error
    if stat.st_size != local["size"] or stat.st_mtime_ns != local["mtime_ns"]:
        raise ValueError("Local file changed since its last collection scan")
    details = {
        "spotify_track_id": spotify_track_id,
        "spotify_uri": wanted["spotify_uri"],
        "spotify_title": wanted["spotify_title"],
        "spotify_artist": wanted["spotify_artist"],
        "path": path,
        "title": local["title"],
        "artist": local["artist"],
        "album": local["album"],
        "duration_seconds": local["duration_seconds"],
        "bitrate": local["bitrate"],
        "audio_format": local["audio_format"],
        "size": local["size"],
        "mtime_ns": local["mtime_ns"],
    }
    details["fingerprint"] = hashlib.sha256(
        json.dumps(details, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return details


def confirm_local_match(
    db_path: Path, spotify_track_id: str, path: str, fingerprint: str, *, verified: bool
) -> bool:
    if not verified:
        raise ValueError("Confirm that this is the exact recording and mix")
    with connect(db_path) as conn:
        preview = _local_match_preview(conn, spotify_track_id, path)
        if preview["fingerprint"] != fingerprint:
            raise ValueError("Match changed since preview; review it again")
        current = conn.execute(
            "select status from discovery_wishlist where spotify_track_id = ?",
            (spotify_track_id,),
        ).fetchone()
        previous = str(current["status"])
        if previous == "needs_curation":
            return False
        now = now_iso()
        conn.execute(
            "update tracks set spotify_uri = ?, spotify_link_skipped_at = null where path = ?",
            (preview["spotify_uri"], path),
        )
        conn.execute(
            "update discovery_tracks set local_track_path = ?, updated_at = ? where spotify_track_id = ?",
            (path, now, spotify_track_id),
        )
        conn.execute(
            "update discovery_wishlist set status = 'needs_curation', updated_at = ? where spotify_track_id = ?",
            (now, spotify_track_id),
        )
        conn.execute(
            """insert into discovery_wishlist_events
               (spotify_track_id, from_status, to_status, source, note, created_at)
               values (?, ?, 'acquired', 'manual_local_match', ?, ?)""",
            (spotify_track_id, previous, path, now),
        )
        conn.execute(
            """insert into discovery_wishlist_events
               (spotify_track_id, from_status, to_status, source, created_at)
               values (?, 'acquired', 'needs_curation', 'manual_local_match', ?)""",
            (spotify_track_id, now),
        )
    return True


def playlist_import_preview(client: Any, playlist_uri: str) -> dict[str, Any]:
    if not playlist_uri.startswith("spotify:playlist:"):
        raise ValueError("Configure a Spotify to-download playlist before importing")
    payloads = _playlist_tracks(client, playlist_uri)
    tracks: list[SpotifyEntityTrack] = []
    seen: set[str] = set()
    for payload in payloads:
        track = spotify_track_from_payload(payload)
        if track is not None and track.spotify_track_id not in seen:
            tracks.append(track)
            seen.add(track.spotify_track_id)
    fingerprint = hashlib.sha256(
        json.dumps(
            [
                (payload.get("id"), payload.get("uri"), payload.get("name"))
                for payload in payloads
            ],
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    return {
        "tracks": tracks,
        "total": len(payloads),
        "skipped": len(payloads) - len(tracks),
        "fingerprint": fingerprint,
    }


def import_playlist(
    client: Any,
    db_path: Path,
    playlist_uri: str,
    *,
    fingerprint: str,
    label_aliases: dict[str, str],
) -> dict[str, int]:
    preview = playlist_import_preview(client, playlist_uri)
    if preview["fingerprint"] != fingerprint:
        raise ValueError("Playlist changed since preview; preview it again")
    tracks = cast(list[SpotifyEntityTrack], preview["tracks"])
    upsert_spotify_tracks(
        db_path,
        tracks,
        label_aliases=label_aliases,
        source="manual",
        provenance={"kind": "wishlist_playlist_import", "playlist_uri": playlist_uri},
    )
    added = 0
    with connect(db_path) as conn:
        for track in tracks:
            added += _set_wanted(
                conn,
                track.spotify_track_id,
                True,
                source="spotify_playlist_import",
                restore_removed=False,
            )
    return {
        "added": added,
        "already_present": len(tracks) - added,
        "skipped": int(preview["skipped"]),
    }
