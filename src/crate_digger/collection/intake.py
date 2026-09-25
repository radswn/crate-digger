"""Auditable intake checks whose approvals expire when their evidence changes."""

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from crate_digger.collection.index import _ensure_schema

CHECKS = (
    (
        "identity",
        "Recording identity",
        "Confirm the local file is the selected recording and mix.",
    ),
    (
        "audio_integrity",
        "Audio integrity",
        "Listen and check that the indexed file plays cleanly.",
    ),
    ("artwork", "Artwork", "Check the embedded cover, or explain why none is needed."),
    ("metadata", "File metadata", "Check the title and artist against the recording."),
    (
        "classification",
        "DJ classification",
        "Review Genre, Energy, Tone, Character, and category.",
    ),
)
CheckKey = Literal[
    "identity", "audio_integrity", "artwork", "metadata", "classification"
]


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    return conn


def _evidence(conn: sqlite3.Connection, track_path: str, key: str) -> dict[str, Any]:
    if key not in {item[0] for item in CHECKS}:
        raise ValueError("Unknown intake check")
    row = conn.execute(
        """select t.path, t.spotify_uri, t.title, t.artist, t.album,
                  t.size, t.mtime_ns, t.artwork_mime, t.artwork_data,
                  p.energy, c.approved_genre, c.tone, c.character_json,
                  c.collection_category
           from tracks t
           left join track_profiles p on p.track_path = t.path
           left join dj_curation c on c.track_path = t.path
           where t.path = ?""",
        (track_path,),
    ).fetchone()
    if row is None:
        raise KeyError(f"Indexed track not found: {track_path}")
    if key == "identity":
        payload = {"spotify_uri": row["spotify_uri"]}
        can_approve = bool(row["spotify_uri"])
        explanation = "Spotify recording linked" if can_approve else "No recording link"
    elif key == "audio_integrity":
        try:
            stat = Path(track_path).stat()
            current = (stat.st_size, stat.st_mtime_ns)
        except OSError:
            current = None
        indexed = (row["size"], row["mtime_ns"])
        payload = {"indexed": indexed, "current": current}
        can_approve = current is not None and current == indexed
        explanation = (
            "File matches the collection scan; manual playback check required"
            if can_approve
            else "File is missing or changed since the collection scan"
        )
    elif key == "artwork":
        art = row["artwork_data"]
        payload = {
            "mime": row["artwork_mime"],
            "sha256": hashlib.sha256(art).hexdigest() if art is not None else None,
        }
        can_approve = bool(row["artwork_mime"] and art)
        explanation = (
            "Embedded artwork present" if can_approve else "No embedded artwork"
        )
    elif key == "metadata":
        payload = {
            "title": row["title"],
            "artist": row["artist"],
            "album": row["album"],
        }
        can_approve = bool(row["title"] and row["artist"])
        explanation = (
            "Title and artist present" if can_approve else "Title or artist missing"
        )
    else:
        character = json.loads(row["character_json"] or "[]")
        payload = {
            "genre": row["approved_genre"],
            "energy": row["energy"],
            "tone": row["tone"],
            "character": character,
            "category": row["collection_category"],
        }
        can_approve = bool(
            row["approved_genre"]
            and row["energy"]
            and row["tone"] is not None
            and 1 <= len(character) <= 2
            and row["collection_category"]
        )
        explanation = (
            "Reviewed Genre, Energy, Tone, Character, and category present"
            if can_approve
            else "DJ classification is incomplete"
        )
    fingerprint = hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return {
        "fingerprint": fingerprint,
        "can_approve": can_approve,
        "evidence": explanation,
    }


def list_intake_checks(db_path: Path, track_path: str) -> list[dict[str, Any]]:
    with _connect(db_path) as conn:
        stored = {
            row["check_key"]: row
            for row in conn.execute(
                "select * from dj_intake_checks where track_path = ?", (track_path,)
            )
        }
        result = []
        for key, label, instruction in CHECKS:
            evidence = _evidence(conn, track_path, key)
            decision = stored.get(key)
            status = "pending"
            if decision is not None:
                status = (
                    str(decision["decision"])
                    if decision["evidence_fingerprint"] == evidence["fingerprint"]
                    and (
                        decision["decision"] == "overridden" or evidence["can_approve"]
                    )
                    else "stale"
                )
            result.append(
                {
                    "key": key,
                    "label": label,
                    "instruction": instruction,
                    "status": status,
                    "note": decision["note"] if decision else "",
                    "checked_at": decision["checked_at"] if decision else None,
                    **evidence,
                }
            )
    return result


def review_intake_check(
    db_path: Path,
    track_path: str,
    key: CheckKey,
    decision: str,
    *,
    fingerprint: str,
    confirmed: bool,
    note: str = "",
) -> bool:
    if not confirmed:
        raise ValueError("Confirm that you reviewed this check")
    if decision not in {"approved", "overridden"}:
        raise ValueError("Choose Approve or Override")
    note = note.strip()
    if decision == "overridden" and not note:
        raise ValueError("An override needs a reason")
    with _connect(db_path) as conn:
        evidence = _evidence(conn, track_path, key)
        if evidence["fingerprint"] != fingerprint:
            raise ValueError("Intake evidence changed; review it again")
        if decision == "approved" and not evidence["can_approve"]:
            raise ValueError("Required evidence is missing; use an explained override")
        previous = conn.execute(
            "select decision, note, evidence_fingerprint from dj_intake_checks where track_path = ? and check_key = ?",
            (track_path, key),
        ).fetchone()
        if previous and (
            previous["decision"] == decision
            and previous["note"] == note
            and previous["evidence_fingerprint"] == fingerprint
        ):
            return False
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """insert into dj_intake_checks
               (track_path, check_key, decision, evidence_fingerprint, note, checked_at)
               values (?, ?, ?, ?, ?, ?)
               on conflict(track_path, check_key) do update set
                 decision = excluded.decision,
                 evidence_fingerprint = excluded.evidence_fingerprint,
                 note = excluded.note, checked_at = excluded.checked_at""",
            (track_path, key, decision, fingerprint, note, now),
        )
        conn.execute(
            """insert into dj_intake_check_events
               (track_path, check_key, from_decision, to_decision,
                evidence_fingerprint, note, created_at)
               values (?, ?, ?, ?, ?, ?, ?)""",
            (
                track_path,
                key,
                previous["decision"] if previous else None,
                decision,
                fingerprint,
                note,
                now,
            ),
        )
    return True


def intake_history(db_path: Path, track_path: str) -> list[dict[str, Any]]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            """select check_key, from_decision, to_decision, note, created_at
               from dj_intake_check_events where track_path = ? order by id desc""",
            (track_path,),
        ).fetchall()
    return [dict(row) for row in rows]
