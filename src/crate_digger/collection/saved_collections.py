"""Saved rules over reviewed DJ metadata; no Traktor write occurs here."""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from crate_digger.collection.dj_curation import CHARACTER_TAGS
from crate_digger.collection.genre_review import TAXONOMY
from crate_digger.collection.index import _ensure_schema
from crate_digger.collection.traktor_organization import CATEGORIES, PLAYLISTS


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    return conn


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    if not db_path.is_file():
        raise ValueError("Collection database does not exist")
    conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _bound(value: object, lower: int, upper: int, label: str) -> int | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not lower <= value <= upper
    ):
        raise ValueError(f"{label} must be from {lower} to {upper}")
    return value


def validate_rule(raw: dict[str, object]) -> dict[str, Any]:
    allowed = {
        "min_energy",
        "max_energy",
        "min_tone",
        "max_tone",
        "character_tags",
        "genre",
        "collection_category",
    }
    if set(raw) - allowed:
        raise ValueError("Unknown collection rule field")
    rule: dict[str, Any] = {
        "min_energy": _bound(raw.get("min_energy"), 1, 5, "Minimum Energy"),
        "max_energy": _bound(raw.get("max_energy"), 1, 5, "Maximum Energy"),
        "min_tone": _bound(raw.get("min_tone"), -2, 2, "Minimum Tone"),
        "max_tone": _bound(raw.get("max_tone"), -2, 2, "Maximum Tone"),
    }
    for field in ("energy", "tone"):
        minimum, maximum = rule[f"min_{field}"], rule[f"max_{field}"]
        if minimum is not None and maximum is not None and minimum > maximum:
            raise ValueError(f"Minimum {field.title()} exceeds maximum")
    characters = raw.get("character_tags", [])
    if (
        not isinstance(characters, list)
        or any(
            not isinstance(tag, str) or tag not in CHARACTER_TAGS for tag in characters
        )
        or len(characters) > 2
        or len(set(characters)) != len(characters)
    ):
        raise ValueError("Choose up to two distinct Character tags")
    rule["character_tags"] = characters
    genre = raw.get("genre")
    if genre is not None and genre not in TAXONOMY:
        raise ValueError("Choose a broad genre from the project taxonomy")
    rule["genre"] = genre
    category = raw.get("collection_category")
    if category is not None and category not in CATEGORIES:
        raise ValueError("Choose a valid collection category")
    rule["collection_category"] = category
    if (
        all(rule[key] is None for key in allowed - {"character_tags"})
        and not characters
    ):
        raise ValueError("Choose at least one collection rule")
    return rule


def list_saved_collections(db_path: Path) -> list[dict[str, Any]]:
    if not db_path.is_file():
        return []
    with _connect_readonly(db_path) as conn:
        if (
            conn.execute(
                "select 1 from sqlite_master where name = 'dj_saved_collections'"
            ).fetchone()
            is None
        ):
            return []
        rows = conn.execute(
            "select * from dj_saved_collections order by lower(name), id"
        ).fetchall()
    return [{**dict(row), "rule": json.loads(row["rule_json"])} for row in rows]


def get_saved_collection(db_path: Path, collection_id: int) -> dict[str, Any]:
    with _connect_readonly(db_path) as conn:
        if (
            conn.execute(
                "select 1 from sqlite_master where name = 'dj_saved_collections'"
            ).fetchone()
            is None
        ):
            raise KeyError(f"Saved collection not found: {collection_id}")
        row = conn.execute(
            "select * from dj_saved_collections where id = ?", (collection_id,)
        ).fetchone()
    if row is None:
        raise KeyError(f"Saved collection not found: {collection_id}")
    return {**dict(row), "rule": json.loads(row["rule_json"])}


def save_collection(
    db_path: Path,
    *,
    name: str,
    description: str,
    rule: dict[str, object],
    collection_id: int | None = None,
) -> int:
    name = " ".join(name.split())
    description = description.strip()
    if not name or len(name) > 80:
        raise ValueError("Collection name must be 1 to 80 characters")
    if any(ord(character) < 32 for character in name):
        raise ValueError("Collection name cannot contain control characters")
    if name.casefold() in {item[1].casefold() for item in PLAYLISTS}:
        raise ValueError("Collection name conflicts with a managed Traktor playlist")
    if len(description) > 500:
        raise ValueError("Collection description is too long")
    normalized = validate_rule(rule)
    now = datetime.now(timezone.utc).isoformat()
    after = {"name": name, "description": description, "rule": normalized}
    with _connect(db_path) as conn:
        previous = None
        if collection_id is not None:
            row = conn.execute(
                "select * from dj_saved_collections where id = ?", (collection_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"Saved collection not found: {collection_id}")
            previous = {
                "name": row["name"],
                "description": row["description"],
                "rule": json.loads(row["rule_json"]),
            }
            if previous == after:
                return collection_id
        try:
            if collection_id is None:
                cursor = conn.execute(
                    """insert into dj_saved_collections
                       (name, description, rule_json, created_at, updated_at)
                       values (?, ?, ?, ?, ?)""",
                    (
                        name,
                        description,
                        json.dumps(normalized, sort_keys=True),
                        now,
                        now,
                    ),
                )
                assert cursor.lastrowid is not None
                collection_id = int(cursor.lastrowid)
            else:
                conn.execute(
                    """update dj_saved_collections
                       set name = ?, description = ?, rule_json = ?, updated_at = ?
                       where id = ?""",
                    (
                        name,
                        description,
                        json.dumps(normalized, sort_keys=True),
                        now,
                        collection_id,
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise ValueError("A saved collection already has that name") from error
        conn.execute(
            """insert into dj_saved_collection_events
               (collection_id, before_json, after_json, created_at) values (?, ?, ?, ?)""",
            (
                collection_id,
                json.dumps(previous, sort_keys=True) if previous else None,
                json.dumps(after, sort_keys=True),
                now,
            ),
        )
    return collection_id


def preview_collection(db_path: Path, collection_id: int) -> list[dict[str, Any]]:
    collection = get_saved_collection(db_path, collection_id)
    rule = collection["rule"]
    clauses = ["1 = 1"]
    params: list[object] = []
    for key, column, operator in (
        ("min_energy", "p.energy", ">="),
        ("max_energy", "p.energy", "<="),
        ("min_tone", "c.tone", ">="),
        ("max_tone", "c.tone", "<="),
    ):
        if rule[key] is not None:
            clauses.append(f"{column} {operator} ?")
            params.append(rule[key])
    if rule["genre"] is not None:
        clauses.append("c.approved_genre = ?")
        params.append(rule["genre"])
    if rule["collection_category"] is not None:
        clauses.append("c.collection_category = ?")
        params.append(rule["collection_category"])
    for tag in rule["character_tags"]:
        clauses.append(
            "exists (select 1 from json_each(c.character_json) where value = ?)"
        )
        params.append(tag)
    with _connect_readonly(db_path) as conn:
        rows = conn.execute(
            f"""select t.path, t.title, t.artist, t.audio_format,
                       p.energy, c.tone, c.approved_genre, c.character_json,
                       c.collection_category
                from dj_curation c
                join tracks t on t.path = c.track_path
                left join track_profiles p on p.track_path = t.path
                where {" and ".join(clauses)}
                order by p.energy, c.tone, lower(coalesce(t.artist, '')),
                         lower(coalesce(t.title, '')), lower(t.path)""",
            params,
        ).fetchall()
    return [
        {**dict(row), "character": json.loads(row["character_json"])} for row in rows
    ]


def collection_history(db_path: Path, collection_id: int) -> list[dict[str, Any]]:
    with _connect_readonly(db_path) as conn:
        rows = conn.execute(
            """select before_json, after_json, created_at
               from dj_saved_collection_events where collection_id = ? order by id desc""",
            (collection_id,),
        ).fetchall()
    return [
        {
            "before": json.loads(row["before_json"]) if row["before_json"] else None,
            "after": json.loads(row["after_json"]),
            "created_at": row["created_at"],
        }
        for row in rows
    ]
