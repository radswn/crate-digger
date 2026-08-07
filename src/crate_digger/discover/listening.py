"""Durable projection of a Discovery Session onto one dedicated Spotify playlist."""

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from crate_digger.discover.repository import connect, now_iso
from crate_digger.discover.sessions import get_session, list_session_items


class PlaylistConflict(ValueError):
    """The dedicated playlist changed outside Crate Digger."""


class PlaylistGateway(Protocol):
    def snapshot(self, playlist_uri: str) -> dict[str, Any]:
        ...

    def owner_id(self) -> str:
        ...

    def replace(self, playlist_uri: str, uris: list[str]) -> None:
        ...

    def recently_played(self, *, before: int | None = None) -> dict[str, Any]:
        ...

    def unavailable_tracks(self, uris: list[str]) -> set[str]:
        ...


def _playlist_id(uri: str) -> str:
    value = uri.removeprefix("https://open.spotify.com/playlist/").split("?")[0]
    value = value.removeprefix("spotify:playlist:")
    if re.fullmatch(r"[A-Za-z0-9]+", value) is None:
        raise ValueError("Expected a Spotify playlist URI or link")
    return value


def _uri(uri: str) -> str:
    return f"spotify:playlist:{_playlist_id(uri)}"


def _protected(settings: dict[str, Any]) -> set[str]:
    spotify = settings["spotify"]
    return {
        _uri(spotify[key])
        for key in (
            "to_listen_playlist",
            "to_download_playlist",
            "test_playlist",
            "acapella_playlist",
            "followed_labels_playlist",
        )
        if spotify.get(key)
    }


class SpotifyPlaylistAdapter:
    def __init__(self, client: Any) -> None:
        self.client = client

    def snapshot(self, playlist_uri: str) -> dict[str, Any]:
        playlist = self.client.playlist(playlist_uri)
        uris: list[str] = []
        offset = 0
        while True:
            # Installed Spotipy has only the deprecated /tracks convenience methods.
            # Its authenticated request helper supports Spotify's current /items API.
            page = self.client._get(
                f"playlists/{_playlist_id(playlist_uri)}/items",
                limit=50,
                offset=offset,
            )
            for entry in page.get("items", []):
                item = entry.get("item") or entry.get("track")
                if not item or not item.get("uri"):
                    raise PlaylistConflict(
                        "Cannot inspect every item in the session playlist"
                    )
                uris.append(str(item["uri"]))
            if not page.get("next"):
                break
            offset += 50
        return {
            "uri": _uri(str(playlist["uri"])),
            "owner_id": str(playlist["owner"]["id"]),
            "snapshot_id": str(playlist["snapshot_id"]),
            "track_uris": uris,
            "public": playlist.get("public"),
        }

    def owner_id(self) -> str:
        return str(self.client.current_user()["id"])

    def replace(self, playlist_uri: str, uris: list[str]) -> None:
        # Discovery Sessions are capped at 100 items by sessions.build_session.
        if len(uris) > 100:
            raise ValueError("Session exceeds Spotify's 100-item replace limit")
        self.client._put(
            f"playlists/{_playlist_id(playlist_uri)}/items", payload={"uris": uris}
        )

    def recently_played(self, *, before: int | None = None) -> dict[str, Any]:
        return self.client.current_user_recently_played(limit=50, before=before)

    def unavailable_tracks(self, uris: list[str]) -> set[str]:
        unavailable: set[str] = set()
        for offset in range(0, len(uris), 50):
            batch = uris[offset : offset + 50]
            response = self.client.tracks(
                [uri.removeprefix("spotify:track:") for uri in batch]
            )
            details = response.get("tracks", [])
            if len(details) != len(batch):
                raise ValueError(
                    "Spotify returned an incomplete track availability response"
                )
            for uri, track in zip(batch, details, strict=True):
                if track is None or track.get("is_playable") is False:
                    unavailable.add(uri)
        return unavailable


def bind_playlist(
    db_path: Path, spotify: PlaylistGateway, settings: dict[str, Any], playlist_uri: str
) -> dict[str, Any]:
    uri = _uri(playlist_uri)
    if uri in _protected(settings):
        raise ValueError("Session playlist is a protected regular playlist")
    remote = spotify.snapshot(uri)
    if remote["uri"] != uri or remote["owner_id"] != spotify.owner_id():
        raise ValueError("Session playlist must belong to the authenticated account")
    if remote["public"] is not False:
        raise ValueError("Session playlist must be private")
    with connect(db_path) as conn:
        old = conn.execute(
            "select * from discovery_spotify_playlist where id = 1"
        ).fetchone()
        if old:
            if old["uri"] != uri:
                raise ValueError("A different session playlist is already bound")
            if old["owner_id"] != remote["owner_id"]:
                raise ValueError("Session playlist owner changed")
            if not _matches_content(remote, json.loads(old["track_uris"])):
                raise PlaylistConflict(
                    "Bound playlist changed; use accept-playlist after restoring it"
                )
            return binding(db_path)
        if remote["track_uris"]:
            raise ValueError("Session playlist must be empty when first bound")
        conn.execute(
            "insert into discovery_spotify_playlist values (1, ?, ?, ?, '[]', ?)",
            (uri, remote["owner_id"], remote["snapshot_id"], now_iso()),
        )
    return binding(db_path)


def binding(db_path: Path) -> dict[str, Any]:
    with connect(db_path) as conn:
        row = conn.execute(
            "select * from discovery_spotify_playlist where id = 1"
        ).fetchone()
    if row is None:
        raise ValueError("Bind a dedicated private playlist first")
    result = dict(row)
    result["track_uris"] = json.loads(result["track_uris"])
    return result


def _verify(
    db_path: Path, spotify: PlaylistGateway, settings: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    bound = binding(db_path)
    if bound["uri"] in _protected(settings):
        raise ValueError("Session playlist now matches a protected playlist")
    remote = spotify.snapshot(bound["uri"])
    if (
        remote["uri"] != bound["uri"]
        or remote["owner_id"] != bound["owner_id"]
        or spotify.owner_id() != bound["owner_id"]
        or remote["public"] is not False
    ):
        raise PlaylistConflict("Session playlist URI, owner, or privacy changed")
    return bound, remote


def _matches_content(remote: dict[str, Any], uris: list[str]) -> bool:
    """Spotify may change a snapshot without changing the ordered playlist items."""
    return remote["track_uris"] == uris


def _active(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "select * from discovery_listening_runs where state != 'ended' order by id desc limit 1"
    ).fetchone()


def _last_run(conn: sqlite3.Connection, session_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "select * from discovery_listening_runs where session_id = ? order by id desc limit 1",
        (session_id,),
    ).fetchone()


def _selection(
    db_path: Path, session_id: int, after_position: int = 0
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if get_session(db_path, session_id) is None:
        raise KeyError(f"Discovery session not found: {session_id}")
    selected: list[dict[str, Any]] = []
    unavailable: list[dict[str, Any]] = []
    for item in list_session_items(db_path, session_id):
        if item.position <= after_position or item.decision is not None:
            continue
        with connect(db_path) as conn:
            track = conn.execute(
                "select spotify_uri from discovery_tracks where spotify_track_id = ?",
                (item.track.spotify_track_id,),
            ).fetchone()
        track_uri = str(track["spotify_uri"]) if track else ""
        row = {
            "item_id": item.item_id,
            "position": item.position,
            "title": item.track.title,
            "track_uri": track_uri,
        }
        if track_uri == f"spotify:track:{item.track.spotify_track_id}":
            selected.append(row)
        else:
            unavailable.append({**row, "reason": "No valid Spotify track URI"})
    return selected, unavailable


def preview(
    db_path: Path,
    spotify: PlaylistGateway,
    settings: dict[str, Any],
    session_id: int,
    *,
    resume: bool = False,
) -> dict[str, Any]:
    with connect(db_path) as conn:
        active = _active(conn)
        previous = _last_run(conn, session_id)
    if active and (active["session_id"] != session_id or active["state"] == "ending"):
        raise ValueError(
            f"Run {active['id']} is still {active['state']}; end or retry it first"
        )
    if resume and (previous is None or previous["state"] != "ended"):
        raise ValueError("Resume requires a previous ended run")
    after = int(previous["confirmed_position"] or 0) if resume and previous else 0
    selected, unavailable = _selection(db_path, session_id, after)
    bound, remote = _verify(db_path, spotify, settings)
    if not _matches_content(remote, bound["track_uris"]):
        raise PlaylistConflict(
            "Spotify playlist changed outside Crate Digger; reconcile it manually"
        )
    if bound["track_uris"] and not active:
        raise PlaylistConflict(
            "Dedicated playlist is not empty; finish pending cleanup"
        )
    unavailable_uris = spotify.unavailable_tracks(
        [row["track_uri"] for row in selected]
    )
    if unavailable_uris:
        unavailable.extend(
            {**row, "reason": "Spotify track unavailable"}
            for row in selected
            if row["track_uri"] in unavailable_uris
        )
        selected = [row for row in selected if row["track_uri"] not in unavailable_uris]
    return {
        "session_id": session_id,
        "resume_after_position": after,
        "playlist_uri": bound["uri"],
        "playlist_url": f"https://open.spotify.com/playlist/{_playlist_id(bound['uri'])}",
        "items": selected,
        "unavailable": unavailable,
        "remote_mutation": False,
    }


def _store_snapshot(db_path: Path, remote: dict[str, Any]) -> None:
    with connect(db_path) as conn:
        conn.execute(
            "update discovery_spotify_playlist set snapshot_id = ?, track_uris = ?, updated_at = ? where id = 1",
            (remote["snapshot_id"], json.dumps(remote["track_uris"]), now_iso()),
        )


def accept_playlist_snapshot(
    db_path: Path, spotify: PlaylistGateway, settings: dict[str, Any]
) -> dict[str, Any]:
    """Acknowledge an exact manually restored playlist without changing Spotify."""
    _, remote = _verify(db_path, spotify, settings)
    with connect(db_path) as conn:
        active = _active(conn)
    if active is None:
        expected = []
    elif active["state"] == "ending":
        expected = json.loads(active["target_uris"])
        if remote["track_uris"] == []:
            _store_snapshot(db_path, remote)
            with connect(db_path) as conn:
                conn.execute(
                    "update discovery_listening_runs set state='ended' where id=?",
                    (active["id"],),
                )
            return {"playlist_uri": remote["uri"], "state": "ended", "track_uris": []}
    elif active["state"] in ("active", "publishing"):
        expected = json.loads(active["target_uris"])
    else:
        expected = []
    if remote["track_uris"] != expected:
        raise PlaylistConflict(
            "Restore the exact expected track order before accepting the new snapshot"
        )
    _store_snapshot(db_path, remote)
    if active and active["state"] == "publishing":
        with connect(db_path) as conn:
            conn.execute(
                "update discovery_listening_runs set state='active' where id=?",
                (active["id"],),
            )
    return {
        "playlist_uri": remote["uri"],
        "state": "active"
        if active and active["state"] == "publishing"
        else active["state"]
        if active
        else "idle",
        "track_uris": remote["track_uris"],
    }


def _finish_publish(
    db_path: Path,
    spotify: PlaylistGateway,
    settings: dict[str, Any],
    run: dict[str, Any],
) -> dict[str, Any]:
    bound, remote = _verify(db_path, spotify, settings)
    target: list[str] = json.loads(run["target_uris"])
    base: list[str] = bound["track_uris"]
    if _matches_content(remote, target):
        pass  # A prior request succeeded before its SQLite acknowledgement.
    elif _matches_content(remote, base):
        spotify.replace(bound["uri"], target)
        remote = spotify.snapshot(bound["uri"])
        if remote["track_uris"] != target:
            raise PlaylistConflict("Spotify did not publish the requested order")
    else:
        raise PlaylistConflict("Spotify playlist changed while publish was pending")
    _store_snapshot(db_path, remote)
    with connect(db_path) as conn:
        conn.execute(
            "update discovery_listening_runs set state = 'active' where id = ?",
            (run["id"],),
        )
    return run_detail(db_path, int(run["id"]))


def start(
    db_path: Path,
    spotify: PlaylistGateway,
    settings: dict[str, Any],
    session_id: int,
    *,
    resume: bool = False,
) -> dict[str, Any]:
    with connect(db_path) as conn:
        active = _active(conn)
    if active:
        if active["session_id"] == session_id and active["state"] == "publishing":
            return _finish_publish(db_path, spotify, settings, dict(active))
        if active["session_id"] == session_id and active["state"] == "active":
            _verify_active(db_path, spotify, settings, dict(active))
            return run_detail(db_path, int(active["id"]))
        raise ValueError(f"Run {active['id']} is still {active['state']}; end it first")
    plan = preview(db_path, spotify, settings, session_id, resume=resume)
    if not plan["items"]:
        raise ValueError("No publishable undecided items remain")
    bound = binding(db_path)
    with connect(db_path) as conn:
        cursor = conn.execute(
            "insert into discovery_listening_runs (session_id, playlist_uri, state, started_at, base_snapshot_id, target_uris, confirmed_position) values (?, ?, 'publishing', ?, ?, ?, ?)",
            (
                session_id,
                bound["uri"],
                now_iso(),
                bound["snapshot_id"],
                json.dumps([row["track_uri"] for row in plan["items"]]),
                plan["resume_after_position"] or None,
            ),
        )
        run_id = int(cursor.lastrowid or 0)
        conn.executemany(
            "insert into discovery_listening_run_items values (?, ?, ?, ?)",
            [
                (run_id, row["item_id"], row["position"], row["track_uri"])
                for row in plan["items"]
            ],
        )
    return _finish_publish(db_path, spotify, settings, run_detail(db_path, run_id))


def _verify_active(
    db_path: Path,
    spotify: PlaylistGateway,
    settings: dict[str, Any],
    run: dict[str, Any],
) -> None:
    bound, remote = _verify(db_path, spotify, settings)
    if not _matches_content(remote, json.loads(run["target_uris"])):
        raise PlaylistConflict("Spotify playlist changed outside Crate Digger")


def run_detail(db_path: Path, run_id: int) -> dict[str, Any]:
    with connect(db_path) as conn:
        row = conn.execute(
            "select * from discovery_listening_runs where id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"Listening run not found: {run_id}")
        items = [
            dict(r)
            for r in conn.execute(
                "select i.*, s.decision, t.title from discovery_listening_run_items i join discovery_session_items s on s.id=i.item_id join discovery_candidates c on c.id=s.candidate_id join discovery_tracks t on t.spotify_track_id=c.spotify_track_id where i.run_id=? order by i.position",
                (run_id,),
            )
        ]
        observations = [
            dict(r)
            for r in conn.execute(
                "select * from discovery_play_observations where run_id=? order by played_at",
                (run_id,),
            )
        ]
    result = dict(row)
    result["items"] = items
    result["observations"] = observations
    observed = {r["item_id"] for r in observations}
    confirmed = int(row["confirmed_position"] or 0)
    for item in items:
        item["progress"] = (
            "confirmed heard"
            if item["position"] <= confirmed
            else "observed heard"
            if item["item_id"] in observed
            else "unheard"
        )
    result[
        "playlist_url"
    ] = f"https://open.spotify.com/playlist/{_playlist_id(row['playlist_uri'])}"
    return result


def list_runs(db_path: Path, session_id: int) -> list[dict[str, Any]]:
    with connect(db_path) as conn:
        ids = [
            r["id"]
            for r in conn.execute(
                "select id from discovery_listening_runs where session_id=? order by id desc",
                (session_id,),
            )
        ]
    return [run_detail(db_path, int(run_id)) for run_id in ids]


def confirm_progress(db_path: Path, run_id: int, item_id: int | None) -> dict[str, Any]:
    detail = run_detail(db_path, run_id)
    if item_id is not None:
        item = next((r for r in detail["items"] if r["item_id"] == item_id), None)
        if item is None:
            raise ValueError("Item was not published in this run")
        position = item["position"]
    else:
        position = None
    with connect(db_path) as conn:
        conn.execute(
            "update discovery_listening_runs set confirmed_position=? where id=?",
            (position, run_id),
        )
    return run_detail(db_path, run_id)


def reconcile(db_path: Path, spotify: PlaylistGateway, run_id: int) -> dict[str, Any]:
    detail = run_detail(db_path, run_id)
    end = detail["ended_at"] or now_iso()
    start_dt = datetime.fromisoformat(detail["started_at"].replace("Z", "+00:00"))
    end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
    track_items: dict[str, list[int]] = {}
    for item in detail["items"]:
        track_items.setdefault(item["track_uri"], []).append(item["item_id"])
    before: int | None = int(end_dt.timestamp() * 1000) + 1
    status = "complete"
    any_entries = False
    history_error: str | None = None
    seen_cursors: set[int] = set()
    try:
        for _ in range(20):
            page = spotify.recently_played(before=before)
            entries = page.get("items", [])
            if not entries:
                break
            any_entries = True
            oldest = end_dt
            for entry in entries:
                played_at = str(entry["played_at"])
                played = datetime.fromisoformat(played_at.replace("Z", "+00:00"))
                oldest = min(oldest, played)
                if not start_dt <= played <= end_dt:
                    continue
                uri = str((entry.get("track") or {}).get("uri") or "")
                with connect(db_path) as conn:
                    for item_id in track_items.get(uri, []):
                        conn.execute(
                            "insert or ignore into discovery_play_observations values (?, ?, ?, 'spotify_recently_played')",
                            (run_id, item_id, played_at),
                        )
            if oldest < start_dt or not page.get("next"):
                break
            next_before = int(oldest.timestamp() * 1000)
            if next_before in seen_cursors:
                status = "partial"
                break
            seen_cursors.add(next_before)
            before = next_before
        else:
            status = "partial"
    except Exception as error:
        status = "partial" if any_entries else "unavailable"
        history_error = str(error)
    if status == "complete" and not any_entries:
        status = "empty"
    with connect(db_path) as conn:
        conn.execute(
            "update discovery_listening_runs set history_status=?, history_error=? where id=?",
            (status, history_error, run_id),
        )
    return run_detail(db_path, run_id)


def end(
    db_path: Path, spotify: PlaylistGateway, settings: dict[str, Any], run_id: int
) -> dict[str, Any]:
    detail = run_detail(db_path, run_id)
    if detail["state"] == "ended":
        return detail
    if detail["state"] == "publishing":
        detail = _finish_publish(db_path, spotify, settings, detail)
    with connect(db_path) as conn:
        active = _active(conn)
        if active is None or active["id"] != run_id:
            raise ValueError("Run is not the active dedicated-playlist run")
        conn.execute(
            "update discovery_listening_runs set state='ending', ended_at=coalesce(ended_at, ?) where id=?",
            (now_iso(), run_id),
        )
    reconcile(db_path, spotify, run_id)
    bound, remote = _verify(db_path, spotify, settings)
    if not remote["track_uris"]:
        pass  # Cleanup succeeded before SQLite acknowledgement.
    elif _matches_content(remote, json.loads(detail["target_uris"])):
        spotify.replace(bound["uri"], [])
        remote = spotify.snapshot(bound["uri"])
        if remote["track_uris"]:
            raise PlaylistConflict("Spotify did not clear the session playlist")
    else:
        raise PlaylistConflict(
            "Spotify playlist changed outside Crate Digger; cleanup paused"
        )
    _store_snapshot(db_path, remote)
    with connect(db_path) as conn:
        conn.execute(
            "update discovery_listening_runs set state='ended' where id=?", (run_id,)
        )
    return run_detail(db_path, run_id)
