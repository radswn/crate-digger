"""Durable broad-genre decisions and a narrow Traktor GENRE publisher."""

import hashlib
import json
import os
import sqlite3
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

from crate_digger.collection.index import _ensure_schema
from crate_digger.collection.traktor_organization import (
    _parse_xml,
    _read_nml,
    _signature,
    _traktor_running,
    location_key,
)

TAXONOMY = (
    "Breakbeat",
    "Dance",
    "Disco",
    "Electro",
    "Electronic",
    "Hard Dance",
    "Hip-Hop",
    "House",
    "Latin",
    "Pop",
    "R&B / Soul",
    "Reggae / Dancehall",
    "Rock",
    "Techno",
    "Trance",
    "UK Garage",
)
AUDIT_VERSION = "genre-final-v1"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _schema(conn: sqlite3.Connection) -> None:
    _ensure_schema(conn)
    conn.execute(
        """create table if not exists genre_reviews (
        track_path text primary key references tracks(path) on delete cascade,
        location_key text not null unique,
        audit_genre text not null,
        traktor_baseline_genre text not null,
        proposed_genre text not null,
        method text not null,
        review_flag integer not null check (review_flag in (0,1)),
        status text not null check (status in ('pending','approved','deferred')),
        note text not null,
        evidence_json text not null,
        import_source text not null,
        import_version text not null,
        import_sha256 text not null,
        imported_at text not null,
        decision_by text,
        decision_at text,
        updated_at text not null
    )"""
    )
    conn.execute(
        """create table if not exists genre_review_events (
        id integer primary key,
        track_path text not null references tracks(path) on delete cascade,
        action text not null,
        before_genre text not null,
        after_genre text not null,
        actor text not null,
        note text not null,
        occurred_at text not null
    )"""
    )
    conn.execute(
        """create table if not exists genre_review_sources (
        track_path text not null references tracks(path) on delete cascade,
        source_sha256 text not null,
        source_version text not null,
        evidence_json text not null,
        imported_at text not null,
        primary key (track_path, source_sha256)
    )"""
    )


def _rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return list(
        conn.execute(
            """select g.*, c.genre as current_genre,
        coalesce(c.artist,t.artist) as artist, coalesce(c.title,t.title) as title,
        t.spotify_uri, t.soundcloud_url, s.evidence_json as latest_evidence_json
        from genre_reviews g join canonical_track_metadata c on c.track_path=g.track_path
        join tracks t on t.path=g.track_path
        left join genre_review_sources s on s.rowid = (
            select max(newer.rowid) from genre_review_sources newer
            where newer.track_path=g.track_path)
        order by lower(coalesce(c.artist,t.artist,'')),
        lower(coalesce(c.title,t.title,'')), g.track_path"""
        )
    )


def list_reviews(db_path: Path, status: str = "pending") -> list[dict]:
    if status not in {"pending", "approved", "deferred", "baseline", "all"}:
        raise ValueError("Invalid genre review status")
    with sqlite3.connect(db_path) as conn:
        _schema(conn)
        rows = _rows(conn)
    result = []
    for row in rows:
        review_state = (
            "baseline"
            if row["status"] == "approved" and row["decision_by"] is None
            else row["status"]
        )
        if status != "all" and review_state != status:
            continue
        latest = row["latest_evidence_json"]
        result.append(
            {
                **dict(row),
                "review_state": review_state,
                "evidence": json.loads(row["evidence_json"]),
                "newer_evidence": (
                    json.loads(latest)
                    if latest and latest != row["evidence_json"]
                    else None
                ),
            }
        )
    return result


def import_audit(audit: Path, nml: Path, db_path: Path) -> dict:
    raw = audit.read_bytes()
    source_sha = _sha(raw)
    records = json.loads(raw)
    if not isinstance(records, list):
        raise ValueError("Genre audit must be a JSON array")
    nml_data, _, entries = _read_nml(nml)
    by_key: dict[str, list] = {}
    for entry in entries:
        by_key.setdefault(location_key(entry), []).append(entry)
    conflicts: list[dict[str, str]] = []
    seen: set[str] = set()
    prepared: list[tuple] = []
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        _schema(conn)
        links: dict[str, list[sqlite3.Row]] = {}
        for row in conn.execute(
            """select e.location_key,e.track_path,c.genre
            from traktor_entries e join canonical_track_metadata c on c.track_path=e.track_path
            where e.active=1 and e.track_path is not null"""
        ):
            links.setdefault(row["track_path"], []).append(row)
        existing = {
            row["track_path"]: row
            for row in conn.execute("select * from genre_reviews")
        }
        for record in records:
            path = record.get("path") if isinstance(record, dict) else None
            if not isinstance(path, str) or not path or path in seen:
                conflicts.append(
                    {"path": str(path), "reason": "Missing or duplicate audit path"}
                )
                continue
            seen.add(path)
            matching = links.get(path, [])
            if len(matching) != 1:
                conflicts.append(
                    {
                        "path": path,
                        "reason": "Expected one active canonical Traktor link",
                    }
                )
                continue
            linked = matching[0]
            current = linked["genre"] or ""
            final = record.get("final_genre")
            old = existing.get(path)
            expected_current = (
                old["proposed_genre"]
                if old and old["decision_by"]
                else old["audit_genre"]
                if old
                else final
            )
            if final not in TAXONOMY or current != expected_current:
                conflicts.append(
                    {
                        "path": path,
                        "reason": f"Canonical genre differs from expected: {current!r} vs {expected_current!r}",
                    }
                )
                continue
            if old is not None and old["decision_by"] is None:
                known = conn.execute(
                    """select 1 from genre_review_sources where
                    track_path=? and source_sha256=?""",
                    (path, source_sha),
                ).fetchone()
                latest = conn.execute(
                    """select source_sha256 from genre_review_sources
                    where track_path=? order by rowid desc limit 1""",
                    (path,),
                ).fetchone()
                if known and latest and latest[0] != source_sha:
                    conflicts.append(
                        {"path": path, "reason": "Audit is an older imported version"}
                    )
                    continue
            entry_matches = by_key.get(linked["location_key"], [])
            if len(entry_matches) != 1:
                conflicts.append(
                    {"path": path, "reason": "Missing or duplicate live NML entry"}
                )
                continue
            info = entry_matches[0].find("INFO")
            live = info.get("GENRE", "") if info is not None else ""
            expected_live = old["traktor_baseline_genre"] if old else final
            if live != expected_live:
                conflicts.append(
                    {
                        "path": path,
                        "reason": f"Live Traktor genre differs from stored baseline: {live!r} vs {expected_live!r}",
                    }
                )
                continue
            if old is not None and old["location_key"] != linked["location_key"]:
                conflicts.append(
                    {"path": path, "reason": "Existing audit identity differs"}
                )
                continue
            prepared.append((path, linked["location_key"], final, record))
        if len(seen) != len(links):
            for path in links.keys() - seen:
                conflicts.append(
                    {
                        "path": path,
                        "reason": "Linked canonical track missing from audit",
                    }
                )
        if conflicts:
            return {"imported": 0, "total": len(records), "conflicts": conflicts}
        if nml.read_bytes() != nml_data:
            raise ValueError("Traktor NML changed during audit import")
        now = _now()
        updated = 0
        for path, key, final, record in prepared:
            evidence = {k: v for k, v in record.items() if k not in {"index", "path"}}
            evidence_json = json.dumps(evidence, ensure_ascii=False)
            conn.execute(
                """insert or ignore into genre_review_sources
                (track_path,source_sha256,source_version,evidence_json,imported_at)
                values (?,?,?,?,?)""",
                (path, source_sha, AUDIT_VERSION, evidence_json, now),
            )
            old = existing.get(path)
            if old is not None:
                if old["decision_by"] is None:
                    flag = bool(record.get("review"))
                    review_status = (
                        "pending"
                        if old["status"] == "pending"
                        or flag
                        or final != old["audit_genre"]
                        else "approved"
                    )
                    values = (
                        final,
                        record.get("final_method", ""),
                        int(flag),
                        review_status,
                        record.get("review_note", ""),
                        evidence_json,
                        str(audit.resolve()),
                        AUDIT_VERSION,
                        source_sha,
                    )
                    previous = tuple(
                        old[column]
                        for column in (
                            "proposed_genre",
                            "method",
                            "review_flag",
                            "status",
                            "note",
                            "evidence_json",
                            "import_source",
                            "import_version",
                            "import_sha256",
                        )
                    )
                    if values[:6] != previous[:6]:
                        conn.execute(
                            """update genre_reviews set proposed_genre=?,method=?,
                            review_flag=?,status=?,note=?,evidence_json=?,import_source=?,
                            import_version=?,import_sha256=?,imported_at=?,updated_at=?
                            where track_path=?""",
                            (*values, now, now, path),
                        )
                        updated += 1
                continue
            flag = bool(record.get("review"))
            conn.execute(
                """insert into genre_reviews (
                track_path,location_key,audit_genre,traktor_baseline_genre,proposed_genre,
                method,review_flag,status,note,evidence_json,import_source,import_version,
                import_sha256,imported_at,updated_at)
                values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    path,
                    key,
                    final,
                    final,
                    final,
                    record.get("final_method", ""),
                    int(flag),
                    "pending" if flag else "approved",
                    record.get("review_note", ""),
                    evidence_json,
                    str(audit.resolve()),
                    AUDIT_VERSION,
                    source_sha,
                    now,
                    now,
                ),
            )
        pending = int(
            conn.execute(
                "select count(*) from genre_reviews where status='pending'"
            ).fetchone()[0]
        )
    return {
        "imported": len(prepared) - len(existing.keys() & seen),
        "updated": updated,
        "total": len(records),
        "pending": pending,
        "conflicts": [],
    }


def decide(
    db_path: Path,
    path: str,
    action: str,
    actor: str,
    genre: str | None = None,
    note: str = "",
) -> dict:
    if action not in {"approve", "correct", "defer"}:
        raise ValueError("Invalid genre review action")
    if not actor.strip():
        raise ValueError("Decision maker is required")
    if action == "correct" and genre not in TAXONOMY:
        raise ValueError("Choose a genre from the project taxonomy")
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        _schema(conn)
        conn.execute("begin immediate")
        row = conn.execute(
            """select g.*,c.genre as current_genre from genre_reviews g
            join canonical_track_metadata c on c.track_path=g.track_path where g.track_path=?""",
            (path,),
        ).fetchone()
        if row is None:
            raise ValueError("Track has no genre audit")
        before = row["current_genre"] or ""
        expected = row["proposed_genre"] if row["decision_by"] else row["audit_genre"]
        if before != expected:
            raise ValueError(
                "Canonical genre changed since this review; reload the track"
            )
        target = (
            genre
            if action == "correct"
            else (row["proposed_genre"] if action == "approve" else before)
        )
        if target not in TAXONOMY:
            raise ValueError("Choose a genre from the project taxonomy")
        now = _now()
        status = "deferred" if action == "defer" else "approved"
        if action != "defer":
            conn.execute(
                """update canonical_track_metadata set genre=?,updated_at=?
                where track_path=?""",
                (target, now, path),
            )
        conn.execute(
            """update genre_reviews set proposed_genre=?,status=?,decision_by=?,
            decision_at=?,note=?,updated_at=? where track_path=?""",
            (target, status, actor.strip(), now, note or row["note"], now, path),
        )
        conn.execute(
            """insert into genre_review_events
            (track_path,action,before_genre,after_genre,actor,note,occurred_at)
            values (?,?,?,?,?,?,?)""",
            (path, action, before, target, actor.strip(), note, now),
        )
    return {
        "path": path,
        "genre": target,
        "status": status,
        "decision_by": actor.strip(),
    }


def _decision_fingerprint(
    rows: list[sqlite3.Row], baseline_overrides: dict[str, str] | None = None
) -> str:
    overrides = baseline_overrides or {}
    values = [
        (
            r["track_path"],
            r["location_key"],
            overrides.get(r["track_path"], r["traktor_baseline_genre"]),
            r["current_genre"],
            r["status"],
            r["updated_at"],
        )
        for r in rows
    ]
    return _sha(json.dumps(sorted(values), ensure_ascii=False).encode())


def _render(
    data: bytes, rows: list[sqlite3.Row]
) -> tuple[bytes, list[dict], list[dict], list[dict]]:
    original = _parse_xml(data)
    root = _parse_xml(data)
    collection = root.find("COLLECTION")
    if collection is None:
        raise ValueError("NML has no collection")
    keys: dict[str, list] = {}
    for entry in collection:
        if entry.tag == "ENTRY":
            keys.setdefault(location_key(entry), []).append(entry)
    changes: list[dict] = []
    conflicts: list[dict] = []
    tracks: list[dict] = []
    for row in rows:
        key = row["location_key"]
        track = {
            "path": row["track_path"],
            "location_key": key,
            "baseline_genre": row["traktor_baseline_genre"],
            "effective_genre": row["current_genre"],
            "review_state": (
                "baseline"
                if row["status"] == "approved" and row["decision_by"] is None
                else row["status"]
            ),
        }
        tracks.append(track)
        found = keys.get(key, [])
        if len(found) != 1:
            reason = "Missing or duplicate NML entry"
            track.update({"outcome": "conflict", "reason": reason, "live_genre": None})
            conflicts.append({"path": row["track_path"], "reason": reason})
            continue
        entry = found[0]
        info = entry.find("INFO")
        live = info.get("GENRE", "") if info is not None else ""
        track["live_genre"] = live
        baseline = row["traktor_baseline_genre"]
        if live != baseline:
            reason = f"Traktor genre changed since baseline: {live!r} vs {baseline!r}"
            track.update({"outcome": "conflict", "reason": reason})
            conflicts.append({"path": row["track_path"], "reason": reason})
            continue
        if (
            row["status"] != "approved"
            or row["decision_by"] is None
            or live == row["current_genre"]
        ):
            track["outcome"] = (
                track["review_state"]
                if track["review_state"] in {"pending", "deferred", "baseline"}
                else "unchanged"
            )
            continue
        target = row["current_genre"] or ""
        if target not in TAXONOMY or info is None:
            reason = "Invalid approved genre or missing INFO"
            track.update({"outcome": "conflict", "reason": reason})
            conflicts.append({"path": row["track_path"], "reason": reason})
            continue
        info.set("GENRE", target)
        track["outcome"] = "change"
        changes.append(
            {
                "path": row["track_path"],
                "location_key": key,
                "before": live,
                "after": target,
            }
        )
    output = _parse_xml(ET.tostring(root, encoding="utf-8", xml_declaration=True))
    output_collection = output.find("COLLECTION")
    if output_collection is None:
        raise ValueError("NML has no collection")
    for change in changes:
        entry = next(
            e
            for e in output_collection
            if e.tag == "ENTRY" and location_key(e) == change["location_key"]
        )
        info = entry.find("INFO")
        if info is not None:
            info.set("GENRE", change["before"])
    if _signature(original) != _signature(output):
        raise ValueError(
            "Genre snapshot changed XML outside intended INFO.GENRE fields"
        )
    return (
        ET.tostring(root, encoding="utf-8", xml_declaration=True),
        changes,
        conflicts,
        tracks,
    )


def preview(nml: Path, db_path: Path, output_dir: Path) -> dict:
    data, _, _ = _read_nml(nml)
    with sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        rows = _rows(conn)
    if not rows:
        raise ValueError("Import the genre audit before preview")
    snapshot_bytes, changes, conflicts, tracks = _render(data, rows)
    if nml.read_bytes() != data:
        raise ValueError("Traktor NML changed during preview")
    output_dir.mkdir(parents=True, exist_ok=True)
    snapshot = output_dir / "traktor-genre-preview.nml"
    report_path = output_dir / "genre-preview.json"
    if nml.resolve() in {snapshot.resolve(), report_path.resolve()}:
        raise ValueError("Preview output must differ from live NML")
    snapshot.write_bytes(snapshot_bytes)
    report = {
        "source": str(nml.resolve()),
        "database": str(db_path.resolve()),
        "source_sha256": _sha(data),
        "decision_sha256": _decision_fingerprint(rows),
        "post_apply_decision_sha256": _decision_fingerprint(
            rows, {change["path"]: change["after"] for change in changes}
        ),
        "snapshot": str(snapshot.resolve()),
        "snapshot_sha256": _sha(snapshot_bytes),
        "total": len(rows),
        "pending": sum(r["status"] == "pending" for r in rows),
        "deferred": sum(r["status"] == "deferred" for r in rows),
        "changes": changes,
        "changed": len(changes),
        "conflicts": conflicts,
        "tracks": tracks,
        "applicable": not conflicts,
        "applied": False,
    }
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return {**report, "report": str(report_path.resolve())}


def apply(report_path: Path, backup_dir: Path | None = None) -> dict:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if report["conflicts"] or not report["applicable"]:
        raise ValueError("Preview has genre conflicts; resolve them and preview again")
    source = Path(report["source"])
    snapshot = Path(report["snapshot"])
    proposed = snapshot.read_bytes()
    if _sha(proposed) != report["snapshot_sha256"]:
        raise ValueError("Genre snapshot changed after preview")
    if _traktor_running():
        raise ValueError("Traktor is running; close it before apply")
    live = source.read_bytes()
    db_path = Path(report["database"])
    if _sha(live) == report["snapshot_sha256"]:
        with sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            if (
                _decision_fingerprint(_rows(conn))
                == report["post_apply_decision_sha256"]
            ):
                return {"changed": False, "backup": None, "source": str(source)}
        raise ValueError("Genre decisions changed after preview; make a new preview")
    if _sha(live) != report["source_sha256"]:
        raise ValueError("Traktor NML changed after preview; make a new preview")
    with sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        rows = _rows(conn)
    if _decision_fingerprint(rows) != report["decision_sha256"]:
        raise ValueError("Genre decisions changed after preview; make a new preview")
    regenerated, changes, conflicts, tracks = _render(live, rows)
    if (
        conflicts
        or regenerated != proposed
        or changes != report["changes"]
        or tracks != report["tracks"]
    ):
        raise ValueError("Genre snapshot validation failed")
    if not changes:
        return {"changed": False, "backup": None, "source": str(source)}
    destination = backup_dir or source.parent / ".crate-digger-backups"
    destination.mkdir(parents=True, exist_ok=True)
    backup = destination / f"{source.name}.{datetime.now():%Y%m%d-%H%M%S-%f}.genre.bak"
    mode = stat.S_IMODE(source.stat().st_mode)
    with tempfile.NamedTemporaryFile(dir=destination, delete=False) as handle:
        handle.write(live)
        handle.flush()
        os.fsync(handle.fileno())
        temporary_backup = Path(handle.name)
    os.replace(temporary_backup, backup)
    with tempfile.NamedTemporaryFile(dir=source.parent, delete=False) as handle:
        handle.write(proposed)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    temporary.chmod(mode)
    guard = sqlite3.connect(db_path)
    guard.row_factory = sqlite3.Row
    try:
        guard.execute("begin immediate")
        if _decision_fingerprint(_rows(guard)) != report["decision_sha256"]:
            raise ValueError("Genre decisions changed during apply; make a new preview")
        if _traktor_running() or source.read_bytes() != live:
            raise ValueError("Traktor opened or NML changed during apply")
        os.replace(temporary, source)
        if _sha(source.read_bytes()) != report["snapshot_sha256"]:
            raise ValueError("Written NML failed validation")
        for change in changes:
            updated = guard.execute(
                """update genre_reviews set traktor_baseline_genre=?
                where track_path=? and traktor_baseline_genre=?""",
                (change["after"], change["path"], change["before"]),
            )
            if updated.rowcount != 1:
                raise ValueError("Genre baseline changed during apply")
        guard.commit()
    except Exception:
        guard.rollback()
        if source.read_bytes() == proposed:
            with tempfile.NamedTemporaryFile(dir=source.parent, delete=False) as handle:
                handle.write(live)
                handle.flush()
                os.fsync(handle.fileno())
                restore = Path(handle.name)
            restore.chmod(mode)
            os.replace(restore, source)
        raise
    finally:
        guard.close()
        temporary.unlink(missing_ok=True)
    return {
        "changed": True,
        "count": len(changes),
        "backup": str(backup),
        "source": str(source),
    }
