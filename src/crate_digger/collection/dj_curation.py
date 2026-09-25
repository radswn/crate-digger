"""Human-approved DJ fields kept separate from imported file metadata."""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from crate_digger.collection.genre_review import TAXONOMY
from crate_digger.collection.index import _ensure_schema
from crate_digger.collection.traktor_organization import CATEGORIES

CHARACTER_TAGS = ("rolling", "funky", "driving", "hypnotic", "percussive", "melodic")
VOCAL_PRESENCE = ("instrumental", "mixed", "vocal")


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    return conn


def get_curation(db_path: Path, track_path: str) -> dict[str, Any]:
    with _connect(db_path) as conn:
        row = conn.execute(
            """select t.path, t.title, t.artist, t.album, t.spotify_uri,
                      t.genre as embedded_genre,
                      t.artwork_mime, t.duration_seconds, t.bitrate, t.audio_format,
                      p.energy, c.approved_genre, c.tone, c.character_json,
                      c.vocal_presence, c.collection_category, c.updated_at
               from tracks t
               left join track_profiles p on p.track_path = t.path
               left join dj_curation c on c.track_path = t.path
               where t.path = ?""",
            (track_path,),
        ).fetchone()
        if row is None:
            raise KeyError(f"Indexed track not found: {track_path}")
        imported = conn.execute(
            """select source, genre from track_source_metadata
               where track_path = ? and genre is not null order by source""",
            (track_path,),
        ).fetchall()
    result = dict(row)
    result["character"] = json.loads(result.pop("character_json") or "[]")
    result["imported_genres"] = [dict(item) for item in imported]
    return result


def save_curation(
    db_path: Path,
    track_path: str,
    *,
    genre: str | None,
    energy: int | None,
    tone: int | None,
    character: list[str],
    vocal_presence: str | None,
    collection_category: str | None,
) -> bool:
    if genre is not None and genre not in TAXONOMY:
        raise ValueError("Choose a broad genre from the project taxonomy")
    if energy is not None and (isinstance(energy, bool) or energy not in range(1, 6)):
        raise ValueError("Energy must be from 1 to 5")
    if tone is not None and (isinstance(tone, bool) or tone not in range(-2, 3)):
        raise ValueError("Tone must be from -2 to +2")
    if (
        len(character) > 2
        or len(set(character)) != len(character)
        or any(tag not in CHARACTER_TAGS for tag in character)
    ):
        raise ValueError("Choose at most two distinct Character tags")
    if vocal_presence is not None and vocal_presence not in VOCAL_PRESENCE:
        raise ValueError("Choose a valid vocal presence")
    if collection_category is not None and collection_category not in CATEGORIES:
        raise ValueError("Choose a valid collection category")
    with _connect(db_path) as conn:
        row = conn.execute(
            """select p.energy, c.approved_genre, c.tone, c.character_json,
                      c.vocal_presence, c.collection_category
               from tracks t
               left join track_profiles p on p.track_path = t.path
               left join dj_curation c on c.track_path = t.path
               where t.path = ?""",
            (track_path,),
        ).fetchone()
        if row is None:
            raise KeyError(f"Indexed track not found: {track_path}")
        before = {
            "genre": row["approved_genre"],
            "energy": row["energy"],
            "tone": row["tone"],
            "character": json.loads(row["character_json"] or "[]"),
            "vocal_presence": row["vocal_presence"],
            "collection_category": row["collection_category"],
        }
        after = {
            "genre": genre,
            "energy": energy,
            "tone": tone,
            "character": character,
            "vocal_presence": vocal_presence,
            "collection_category": collection_category,
        }
        if before == after:
            return False
        now = datetime.now(timezone.utc).isoformat()
        if before["energy"] != energy:
            conn.execute(
                """insert into track_profiles (track_path, energy, updated_at)
                   values (?, ?, ?)
                   on conflict(track_path) do update set
                     energy = excluded.energy, updated_at = excluded.updated_at""",
                (track_path, energy, now),
            )
        conn.execute(
            """insert into dj_curation
               (track_path, approved_genre, tone, character_json,
                vocal_presence, collection_category, updated_at)
               values (?, ?, ?, ?, ?, ?, ?)
               on conflict(track_path) do update set
                 approved_genre = excluded.approved_genre, tone = excluded.tone,
                 character_json = excluded.character_json,
                 vocal_presence = excluded.vocal_presence,
                 collection_category = excluded.collection_category,
                 updated_at = excluded.updated_at""",
            (
                track_path,
                genre,
                tone,
                json.dumps(character),
                vocal_presence,
                collection_category,
                now,
            ),
        )
        conn.execute(
            """insert into dj_curation_events
               (track_path, before_json, after_json, source, created_at)
               values (?, ?, ?, 'dashboard_manual', ?)""",
            (
                track_path,
                json.dumps(before, sort_keys=True),
                json.dumps(after, sort_keys=True),
                now,
            ),
        )
    return True


def curation_history(db_path: Path, track_path: str) -> list[dict[str, Any]]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            """select before_json, after_json, source, created_at
               from dj_curation_events where track_path = ? order by id desc""",
            (track_path,),
        ).fetchall()
    return [
        {
            "before": json.loads(row["before_json"]),
            "after": json.loads(row["after_json"]),
            "source": row["source"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]
