"""Database-led, narrow Traktor organization changes.

Only INFO.RATING and the root Crate Digger playlist folder are writable here.
"""

import copy
import csv
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5
from xml.etree import ElementTree as ET

from mutagen import File, MutagenError

from crate_digger.collection.importers.traktor import _location_path, comment2_value
from crate_digger.collection.index import _ensure_schema
from crate_digger.collection.matching import PathMap

CATEGORIES = (
    "DOWNLOAD",
    "ACAPELLAS",
    "LISTENING",
    "STEMS",
    "DEMOS",
    "FX",
    "PRODUCTION",
    "EDITS",
    "TEST",
    "SETS",
    "REVIEW",
)
RULE_VERSION = "organize-traktor-v1"
PLAYLISTS = (
    ("DOWNLOAD", "00 Main Tracks", None),
    ("DOWNLOAD", "01 DJ Downloads", None),
    ("ACAPELLAS", "02 Acapellas", None),
    ("FX", "FX and Samples", "Utilities"),
    ("SETS", "Recorded Sets", "Utilities"),
    ("EDITS", "Personal Edits and Mashups", "Utilities"),
    ("REVIEW", "Short Recordings - Review", "Utilities"),
    ("STEMS", "Generated Stems", "Utilities"),
    ("DEMOS", "Factory Demos", "Utilities"),
    ("PRODUCTION", "Production Assets", "Utilities"),
    ("TEST", "Test Files", "Utilities"),
)
TOKEN_RE = re.compile(r"\[CD_[A-Z]+\]")


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _parse_xml(data: bytes) -> ET.Element:
    return ET.fromstring(
        data,
        parser=ET.XMLParser(
            target=ET.TreeBuilder(insert_comments=True, insert_pis=True)
        ),
    )


def _signature(element: ET.Element) -> tuple:
    return (
        element.tag,
        sorted(element.attrib.items()),
        element.text,
        element.tail,
        tuple(_signature(child) for child in element),
    )


def _read_nml(path: Path) -> tuple[bytes, ET.Element, list[ET.Element]]:
    data = path.read_bytes()
    try:
        root = _parse_xml(data)
    except ET.ParseError as error:
        raise ValueError(f"Invalid Traktor NML: {error}") from error
    collection = root.find("COLLECTION")
    if root.tag != "NML" or collection is None:
        raise ValueError("Expected an NML with a COLLECTION")
    return data, root, [item for item in collection if item.tag == "ENTRY"]


def _collection(root: ET.Element) -> ET.Element:
    collection = root.find("COLLECTION")
    if collection is None:
        raise ValueError("Missing Traktor collection")
    return collection


def location_key(entry: ET.Element) -> str:
    location = entry.find("LOCATION")
    if location is None or not location.get("FILE"):
        raise ValueError(f"Entry has no LOCATION: {entry.get('TITLE', '')}")
    return (
        location.get("VOLUME", "") + location.get("DIR", "") + location.get("FILE", "")
    )


def _audio_id(entry: ET.Element) -> str | None:
    for name in ("AUDIO_ID", "TRACKID", "UNIQUE_ID", "ID"):
        if entry.get(name):
            return entry.get(name)
    element = entry.find("AUDIO_ID")
    return (element.get("VALUE") or element.get("ID")) if element is not None else None


def _local_path(entry: ET.Element, maps: tuple[PathMap, ...]) -> Path | None:
    location = entry.find("LOCATION")
    if location is None:
        return None
    source = _location_path(location)
    if source is None:
        return None
    for mapping in maps:
        prefix = mapping.source.rstrip("/\\")
        if source.casefold() == prefix.casefold() or source.casefold().startswith(
            (prefix + "/").casefold()
        ):
            return Path(mapping.destination) / source[len(prefix) :].lstrip("/\\")
    if (
        re.match(r"^[A-Za-z]:/", source)
        and source[0].upper() == "C"
        and Path("/mnt/c").is_dir()
    ):
        return Path("/mnt/c") / source[3:]
    if source.startswith("/"):
        return Path(source)
    return None


def classify(
    entry: ET.Element, local: Path | None
) -> tuple[str | None, str, float | None]:
    loc = entry.find("LOCATION")
    assert loc is not None
    directory = loc.get("DIR", "").replace("/:", "/").lower()
    filename = loc.get("FILE", "").lower()
    if "/traktor/stems/" in directory or filename.endswith(".stem.mp4"):
        return "STEMS", "Generated stem file path", None
    if "/factory sounds/" in directory or "/pioneerdj/demo tracks/" in directory:
        return "DEMOS", "Factory sound or demo directory", None
    if "/sampler/" in directory:
        return "FX", "Sampler preset or captured sample directory", None
    if "/ableton projects/" in directory:
        return "PRODUCTION", "Ableton project source asset", None
    if "/edits/" in directory:
        if filename == "vocal adlib ee.wav":
            return "FX", "Vocal adlib sample in edits folder (0.6 seconds)", 0.6
        return "EDITS", "Personal edits directory", None
    if "/test/" in directory:
        return "TEST", "Test directory", None
    if "/recording/" in directory or "/recordings/" in directory:
        if local is None or not local.is_file():
            return (
                "REVIEW",
                "Recording duration unavailable; map or restore the file",
                None,
            )
        try:
            media = File(local)
            if media is None or media.info is None:
                raise ValueError("No audio duration")
            seconds = round(media.info.length, 3)
        except (OSError, ValueError, AttributeError, MutagenError):
            return "REVIEW", "Recording duration unreadable", None
        album = entry.find("ALBUM")
        if album is not None and album.get("TITLE", "").lower() in {"butlegs", "mash"}:
            return "EDITS", "Personal recording album is butlegs or mash", seconds
        if seconds >= 1200:
            return (
                "SETS",
                "Recording directory and duration at least 20 minutes",
                seconds,
            )
        return "REVIEW", "Short personal recording; purpose needs review", seconds
    for fragment, category, reason in (
        ("/acapellas/", "ACAPELLAS", "Dedicated acapella directory"),
        ("/download/", "DOWNLOAD", "Downloaded music directory"),
        ("/spoti local/", "LISTENING", "Personal listening directory"),
    ):
        if fragment in directory:
            return category, reason, None
    return None, "Unknown folder; choose a category manually", None


def _schema(conn: sqlite3.Connection) -> None:
    _ensure_schema(conn)
    conn.execute(
        """create table if not exists traktor_entries (
        id integer primary key,
        location_key text not null unique,
        audio_id text,
        source_path text,
        local_path text,
        track_path text references tracks(path) on delete set null,
        title text, artist text, genre text, comment2 text,
        category text check (category in ('DOWNLOAD','ACAPELLAS','LISTENING','STEMS',
          'DEMOS','FX','PRODUCTION','EDITS','TEST','SETS','REVIEW')),
        category_source text check (category_source in ('rule','manual')),
        rule_version text,
        rule_reason text,
        duration_seconds real,
        review_reason text,
        review_state text not null default 'clear',
        relink_from_id integer,
        relink_evidence text,
        imported_fingerprint text not null,
        active integer not null default 1,
        updated_at text not null
    )"""
    )
    columns = {row[1] for row in conn.execute("pragma table_info(traktor_entries)")}
    if "relink_from_id" not in columns:
        conn.execute("alter table traktor_entries add column relink_from_id integer")
    if "relink_evidence" not in columns:
        conn.execute("alter table traktor_entries add column relink_evidence text")
    conn.execute(
        """create table if not exists traktor_organization_source (
        id integer primary key check (id = 1),
        source_file text not null, fingerprint text not null,
        imported_at text not null
    )"""
    )
    conn.execute(
        "create index if not exists idx_traktor_entries_audio_id on traktor_entries(audio_id)"
    )


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    if not db_path.is_file():
        raise ValueError("Import an NML before reviewing or previewing")
    conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    if (
        conn.execute(
            "select 1 from sqlite_master where name='traktor_entries'"
        ).fetchone()
        is None
    ):
        conn.close()
        raise ValueError("Import an NML before reviewing or previewing")
    return conn


def import_nml(source: Path, db_path: Path, maps: tuple[PathMap, ...] = ()) -> dict:
    data, _root, entries = _read_nml(source)
    keys = [location_key(entry) for entry in entries]
    if len(set(keys)) != len(keys):
        raise ValueError("Duplicate NML locations; resolve them before import")
    ids = Counter(_audio_id(entry) for entry in entries if _audio_id(entry))
    db_path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if db_path.is_file():
        backup = (
            db_path.parent / f"{db_path.name}.{datetime.now():%Y%m%d-%H%M%S-%f}.bak"
        )
        with sqlite3.connect(db_path) as existing, sqlite3.connect(backup) as copy_to:
            existing.backup(copy_to)
    fingerprint = _digest(data)
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("pragma foreign_keys=on")
        conn.execute("begin immediate")
        _schema(conn)
        previous = list(conn.execute("select * from traktor_entries"))
        by_key = {row["location_key"]: row for row in previous}
        by_id: dict[str, list[sqlite3.Row]] = {}
        for row in previous:
            if row["audio_id"]:
                by_id.setdefault(row["audio_id"], []).append(row)
        indexed = {row[0]: row[0] for row in conn.execute("select path from tracks")}
        lower_indexed: dict[str, list[str]] = {}
        for path in indexed:
            lower_indexed.setdefault(path.casefold(), []).append(path)
        conn.execute("update traktor_entries set active=0")
        for entry, key in zip(entries, keys, strict=True):
            audio_id = _audio_id(entry)
            old = by_key.get(key)
            candidates = (
                by_id.get(audio_id, []) if audio_id and ids[audio_id] == 1 else []
            )
            if (
                old is None
                and len(candidates) == 1
                and candidates[0]["location_key"] not in keys
            ):
                old = candidates[0]
            local = _local_path(entry, maps)
            path = str(local) if local else None
            exact = indexed.get(path) if path else None
            near = lower_indexed.get(path.casefold(), []) if path and not exact else []
            track_path = exact or (near[0] if len(near) == 1 else None)
            category, rule_reason, seconds = classify(entry, local)
            issues = []
            if local is None:
                issues.append("Unmapped media path")
            elif not local.is_file():
                issues.append("Missing media file")
            if track_path is None:
                issues.append("No scanned track link")
            identity_conflict = bool(
                old and old["audio_id"] and audio_id and old["audio_id"] != audio_id
            )
            if (
                len(near) > 1
                or (audio_id and ids[audio_id] > 1)
                or len(candidates) > 1
                or identity_conflict
            ):
                issues.append("Ambiguous identity")
                category = None
            if category is None:
                issues.append(rule_reason)
            if (
                old is not None
                and old["category_source"] == "manual"
                and not identity_conflict
            ):
                category = old["category"]
                category_source = "manual"
            else:
                category_source = "rule" if category else None
            info = entry.find("INFO")
            genre = info.get("GENRE") if info is not None else None
            values = (
                key,
                audio_id,
                _location_path(entry.find("LOCATION")),
                path,
                track_path,
                entry.get("TITLE"),
                entry.get("ARTIST"),
                genre,
                comment2_value(info),
                category,
                category_source,
                RULE_VERSION,
                rule_reason,
                seconds,
                "; ".join(issues) or None,
                "review" if issues or category == "REVIEW" else "clear",
                fingerprint,
                now,
            )
            if old is None:
                conn.execute(
                    """insert into traktor_entries (
                    location_key,audio_id,source_path,local_path,track_path,title,artist,genre,comment2,
                    category,category_source,rule_version,rule_reason,duration_seconds,
                    review_reason,review_state,imported_fingerprint,updated_at)
                    values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    values,
                )
            else:
                conn.execute(
                    """update traktor_entries set
                    location_key=?,audio_id=?,source_path=?,local_path=?,track_path=?,title=?,artist=?,genre=?,comment2=?,
                    category=?,category_source=?,rule_version=?,rule_reason=?,duration_seconds=?,
                    review_reason=?,review_state=?,imported_fingerprint=?,updated_at=?,active=1
                    where id=?""",
                    (*values, old["id"]),
                )
            if track_path and genre:
                conn.execute(
                    """insert into canonical_track_metadata
                    (track_path,genre,updated_at) values (?,?,?)
                    on conflict(track_path) do update set
                    genre=case when canonical_track_metadata.genre is null or
                      canonical_track_metadata.genre='' then excluded.genre
                      else canonical_track_metadata.genre end""",
                    (track_path, genre, now),
                )
        conn.execute(
            """insert into traktor_organization_source (id,source_file,fingerprint,imported_at)
            values (1,?,?,?) on conflict(id) do update set source_file=excluded.source_file,
            fingerprint=excluded.fingerprint,imported_at=excluded.imported_at""",
            (str(source.resolve()), fingerprint, now),
        )
        rows = list(conn.execute("select * from traktor_entries where active=1"))
    return {
        "source": str(source.resolve()),
        "source_sha256": fingerprint,
        "total": len(entries),
        "linked": sum(row["track_path"] is not None for row in rows),
        "unlinked": sum(row["track_path"] is None for row in rows),
        "missing_media": sum(
            "Missing media file" in (row["review_reason"] or "") for row in rows
        ),
        "review": sum(row["review_state"] == "review" for row in rows),
        "unclassified": sum(row["category"] is None for row in rows),
        "database_backup": str(backup) if backup else None,
    }


def _rows_and_fingerprint(conn: sqlite3.Connection) -> tuple[list[sqlite3.Row], str]:
    rows = list(
        conn.execute("select * from traktor_entries where active=1 order by id")
    )
    state = [
        (
            row["id"],
            row["location_key"],
            row["category"],
            row["category_source"],
            row["review_reason"],
            row["relink_from_id"],
            row["relink_evidence"],
        )
        for row in rows
    ]
    return rows, _digest(json.dumps(state, ensure_ascii=False).encode())


def review(db_path: Path) -> list[dict]:
    with _connect_readonly(db_path) as conn:
        rows, _ = _rows_and_fingerprint(conn)
        return [
            {
                "id": row["id"],
                "location_key": row["location_key"],
                "title": row["title"],
                "artist": row["artist"],
                "category": row["category"],
                "category_source": row["category_source"],
                "rule_reason": row["rule_reason"],
                "review_reason": row["review_reason"],
                "review_state": row["review_state"],
                "track_path": row["track_path"],
                "relink_from_id": row["relink_from_id"],
                "relink_evidence": row["relink_evidence"],
                "comment2_before": row["comment2"],
                "comment2_after": _comment2_after(row["comment2"], row["category"])
                if row["category"]
                else None,
                "comment2_tokens": _tokens(row["category"])
                if row["category"]
                else None,
                "playlists": [
                    name
                    for category, name, _ in PLAYLISTS
                    if category == row["category"]
                ],
            }
            for row in rows
        ]


def set_category(db_path: Path, entry_id: int, category: str) -> None:
    if category not in CATEGORIES:
        raise ValueError(f"Unknown category: {category}")
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "select id from traktor_entries where id=? and active=1", (entry_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"No active Traktor entry {entry_id}")
        conn.execute(
            """update traktor_entries set category=?,category_source='manual',
            updated_at=? where id=?""",
            (category, datetime.now(timezone.utc).isoformat(), entry_id),
        )


def relink(db_path: Path, old_id: int, new_id: int, evidence: str) -> None:
    if not evidence.strip():
        raise ValueError("Relink requires an evidence note")
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        old = conn.execute(
            "select * from traktor_entries where id=? and active=0", (old_id,)
        ).fetchone()
        new = conn.execute(
            "select * from traktor_entries where id=? and active=1", (new_id,)
        ).fetchone()
        if old is None or new is None or old["category_source"] != "manual":
            raise ValueError(
                "Relink needs an inactive manual entry and an active new entry"
            )
        if old["audio_id"] or new["audio_id"]:
            raise ValueError("Relink is reserved for entries without a stable audio ID")
        if (old["title"], old["artist"]) != (new["title"], new["artist"]):
            raise ValueError(
                "Title and artist differ; identity evidence is insufficient"
            )
        conn.execute(
            """update traktor_entries set category=?,category_source='manual',
            relink_from_id=?,relink_evidence=?,updated_at=? where id=?""",
            (
                old["category"],
                old_id,
                evidence.strip(),
                datetime.now(timezone.utc).isoformat(),
                new_id,
            ),
        )


def _tokens(category: str) -> str:
    return f"[CD_{'MAIN' if category == 'DOWNLOAD' else 'HIDE'}] [CD_{category}]"


def _comment2_after(before: str | None, category: str) -> str:
    base = TOKEN_RE.sub("", before or "").strip()
    return f"{base} {_tokens(category)}".strip()


def _playlist(
    parent: ET.Element, name: str, selected: list[ET.Element], old_uuids: dict[str, str]
) -> int:
    selected.sort(
        key=lambda e: (
            e.get("ARTIST", "").casefold(),
            e.get("TITLE", "").casefold(),
            location_key(e),
        )
    )
    node = ET.SubElement(parent, "NODE", TYPE="PLAYLIST", NAME=name)
    body = ET.SubElement(
        node,
        "PLAYLIST",
        ENTRIES=str(len(selected)),
        TYPE="LIST",
        UUID=old_uuids.get(name)
        or uuid5(NAMESPACE_URL, "crate-digger-cleanup/" + name).hex,
    )
    for entry in selected:
        member = ET.SubElement(body, "ENTRY")
        ET.SubElement(member, "PRIMARYKEY", TYPE="TRACK", KEY=location_key(entry))
    parent.set("COUNT", str(len(parent)))
    return len(selected)


def _render(
    original: ET.Element, entries: list[ET.Element], rows: list[sqlite3.Row]
) -> tuple[bytes, list[dict], dict]:
    categories = {row["location_key"]: row["category"] for row in rows}
    if len(categories) != len(entries) or set(categories) != {
        location_key(e) for e in entries
    }:
        raise ValueError(
            "NML entries differ from imported registry; reimport and preview"
        )
    if any(value is None for value in categories.values()):
        raise ValueError("Unclassified entries block apply; review and set categories")
    if any(
        row["review_reason"] and "Ambiguous identity" in row["review_reason"]
        for row in rows
    ):
        raise ValueError("Ambiguous identities block apply")
    root = copy.deepcopy(original)
    output_entries = [e for e in _collection(root) if e.tag == "ENTRY"]
    groups: dict[str, list[ET.Element]] = {}
    changes = []
    for entry in output_entries:
        key = location_key(entry)
        category = categories[key]
        info = entry.find("INFO")
        if info is None:
            raise ValueError(f"Missing INFO for {key}")
        before = comment2_value(info)
        after = _comment2_after(before, category)
        info.set("RATING", after)
        groups.setdefault(category, []).append(entry)
        changes.append(
            {
                "location_key": key,
                "title": entry.get("TITLE"),
                "artist": entry.get("ARTIST"),
                "category": category,
                "comment2_before": before,
                "comment2_after": after,
                "playlists": [name for item, name, _ in PLAYLISTS if item == category],
            }
        )
    nodes = root.find("PLAYLISTS/NODE/SUBNODES")
    if nodes is None:
        raise ValueError("Missing Traktor playlist root")
    folders = [n for n in nodes if n.get("NAME") == "Crate Digger"]
    if len(folders) > 1 or (folders and folders[0].get("TYPE") != "FOLDER"):
        raise ValueError("Ambiguous Crate Digger folder")
    old_uuids = {}
    if folders:
        for node in folders[0].findall(".//NODE"):
            body = node.find("PLAYLIST")
            if body is not None and node.get("NAME") and body.get("UUID"):
                old_uuids[node.get("NAME")] = body.get("UUID")
        nodes.remove(folders[0])
    folder = ET.Element("NODE", TYPE="FOLDER", NAME="Crate Digger")
    children = ET.SubElement(folder, "SUBNODES", COUNT="0")
    nodes.insert(0, folder)
    nodes.set("COUNT", str(len(nodes)))
    counts = {}
    for category, name, utility in PLAYLISTS[:3]:
        counts[name] = _playlist(
            children, name, groups.get(category, []).copy(), old_uuids
        )
    utilities = ET.SubElement(children, "NODE", TYPE="FOLDER", NAME="Utilities")
    utility_nodes = ET.SubElement(utilities, "SUBNODES", COUNT="0")
    children.set("COUNT", str(len(children)))
    for category, name, _ in PLAYLISTS[3:]:
        counts[name] = _playlist(
            utility_nodes, name, groups.get(category, []).copy(), old_uuids
        )
    output = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    _validate(original, _parse_xml(output), categories)
    return output, changes, counts


def _validate(
    original: ET.Element, rendered: ET.Element, categories: dict[str, str]
) -> None:
    restored = copy.deepcopy(rendered)
    original_entries = [e for e in _collection(original) if e.tag == "ENTRY"]
    restored_entries = [e for e in _collection(restored) if e.tag == "ENTRY"]
    if len(original_entries) != len(restored_entries):
        raise ValueError("Collection entry count changed")
    for before, after in zip(original_entries, restored_entries, strict=True):
        left, right = before.find("INFO"), after.find("INFO")
        if left is None or right is None:
            raise ValueError("Missing INFO")
        key = location_key(before)
        if key != location_key(after) or right.get("RATING") != _comment2_after(
            comment2_value(left), categories[key]
        ):
            raise ValueError("Unexpected Comment2 change")
        if "RATING" in left.attrib:
            right.set("RATING", left.get("RATING"))
        else:
            right.attrib.pop("RATING", None)
    a = original.find("PLAYLISTS/NODE/SUBNODES")
    b = restored.find("PLAYLISTS/NODE/SUBNODES")
    if a is None or b is None:
        raise ValueError("Missing playlist root")
    previous = next((n for n in a if n.get("NAME") == "Crate Digger"), None)
    generated = next((n for n in b if n.get("NAME") == "Crate Digger"), None)
    if generated is None:
        raise ValueError("Generated folder missing")
    b.remove(generated)
    if previous is not None:
        b.insert(list(a).index(previous), copy.deepcopy(previous))
    b.attrib = dict(a.attrib)
    if _signature(restored) != _signature(original):
        raise ValueError(
            "Unexpected XML change outside Comment2 and Crate Digger folder"
        )
    for subnodes in rendered.findall(".//SUBNODES"):
        if int(subnodes.get("COUNT", "-1")) != len(subnodes):
            raise ValueError("Invalid playlist folder count")
    keys = set(categories)
    folder = generated
    generated_nodes = {
        node.get("NAME"): node.find("PLAYLIST")
        for node in folder.findall(".//NODE")
        if node.find("PLAYLIST") is not None
    }
    if len(folder.findall(".//PLAYLIST")) != len(PLAYLISTS) or set(generated_nodes) != {
        name for _, name, _ in PLAYLISTS
    }:
        raise ValueError("Generated playlist layout differs from expected layout")
    for category, name, _ in PLAYLISTS:
        body = generated_nodes[name]
        members = [e.find("PRIMARYKEY").get("KEY") for e in body]
        expected_entries = sorted(
            (e for e in original_entries if categories[location_key(e)] == category),
            key=lambda e: (
                e.get("ARTIST", "").casefold(),
                e.get("TITLE", "").casefold(),
                location_key(e),
            ),
        )
        expected = [location_key(e) for e in expected_entries]
        if (
            int(body.get("ENTRIES", "-1")) != len(members)
            or members != expected
            or not set(members) <= keys
        ):
            raise ValueError("Invalid generated playlist membership")
    uuids = [
        node.get("UUID")
        for node in rendered.findall(".//PLAYLIST") + rendered.findall(".//SMARTLIST")
    ]
    if len(uuids) != len(set(uuids)):
        raise ValueError("Duplicate playlist UUIDs")


def preview(source: Path, db_path: Path, output_dir: Path) -> dict:
    data, root, entries = _read_nml(source)
    if len({location_key(e) for e in entries}) != len(entries):
        raise ValueError("Duplicate NML locations block preview")
    with _connect_readonly(db_path) as conn:
        rows, db_fingerprint = _rows_and_fingerprint(conn)
        imported = conn.execute(
            "select * from traktor_organization_source where id=1"
        ).fetchone()
    if (
        imported is None
        or imported["fingerprint"] != _digest(data)
        or imported["source_file"] != str(source.resolve())
    ):
        raise ValueError("NML differs from imported source; import it again")
    output, changes, counts = _render(root, entries, rows)
    if source.read_bytes() != data:
        raise ValueError("Source changed during preview")
    output_dir.mkdir(parents=True, exist_ok=True)
    snapshot = output_dir / "traktor-organized.nml"
    report_path = output_dir / "organization-preview.json"
    if source.resolve() in {snapshot.resolve(), report_path.resolve()}:
        raise ValueError("Preview outputs must differ from the source NML")
    snapshot.write_bytes(output)
    report = {
        "source": str(source.resolve()),
        "database": str(db_path.resolve()),
        "source_sha256": _digest(data),
        "database_sha256": db_fingerprint,
        "snapshot": str(snapshot.resolve()),
        "snapshot_sha256": _digest(output),
        "total": len(entries),
        "categories": dict(Counter(row["category"] for row in rows)),
        "playlist_counts": counts,
        "changed_comment2": sum(
            c["comment2_before"] != c["comment2_after"] for c in changes
        ),
        "entries": changes,
        "validation": "Only managed Comment2 tokens and Crate Digger folder changed; XML tree and playlists validated",
        "applied": False,
    }
    report_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return {**report, "report": str(report_path.resolve())}


def _traktor_executable(value: str) -> bool:
    name = value.replace("\\", "/").rsplit("/", 1)[-1].casefold()
    return (
        re.fullmatch(r"traktor(?: pro)?(?: [0-9]+(?:\.[0-9]+)*)?(?:\.exe)?", name)
        is not None
    )


def _traktor_process(name: str, argv: list[str]) -> bool:
    if _traktor_executable(name) or (argv and _traktor_executable(argv[0])):
        return True
    if len(argv) > 1:
        launcher = argv[0].replace("\\", "/").rsplit("/", 1)[-1].casefold()
        if launcher.startswith("wine") and _traktor_executable(argv[1]):
            return True
    return False


def _traktor_running() -> bool:
    proc = Path("/proc")
    if proc.is_dir():
        for item in proc.iterdir():
            if not item.name.isdigit():
                continue
            try:
                name = (item / "comm").read_text().strip().lower()
                argv = [
                    part.decode(errors="ignore")
                    for part in (item / "cmdline").read_bytes().split(b"\0")
                    if part
                ]
            except (OSError, UnicodeError):
                continue
            if _traktor_process(name, argv):
                return True
    else:
        try:
            result = subprocess.run(
                ["pgrep", "-ix", r"traktor( pro)?( [0-9]+(\.[0-9]+)*)?(\.exe)?"],
                capture_output=True,
                check=False,
            )
        except OSError:
            result = None
        if result is not None and result.returncode == 0:
            return True
    if shutil.which("tasklist.exe"):
        try:
            result = subprocess.run(
                ["tasklist.exe", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ValueError(
                f"Could not check Windows Traktor processes: {error}"
            ) from error
        if result.returncode != 0:
            raise ValueError("Could not check Windows Traktor processes")
        return any(
            _traktor_executable(fields[0])
            for fields in csv.reader(result.stdout.splitlines())
            if fields
        )
    return False


def apply(preview_report: Path, backup_dir: Path | None = None) -> dict:
    report = json.loads(preview_report.read_text(encoding="utf-8"))
    source, db_path, snapshot = (
        Path(report[name]) for name in ("source", "database", "snapshot")
    )
    if _traktor_running():
        raise ValueError("Traktor is running; close it before apply")
    live = source.read_bytes()
    proposed = snapshot.read_bytes()
    if _digest(proposed) != report["snapshot_sha256"]:
        raise ValueError("Proposed NML changed after preview")
    with _connect_readonly(db_path) as conn:
        rows, db_fingerprint = _rows_and_fingerprint(conn)
    if db_fingerprint != report["database_sha256"]:
        raise ValueError("Categories changed after preview; make a fresh preview")
    if live == proposed:
        return {"changed": False, "backup": None, "source": str(source)}
    if _digest(live) != report["source_sha256"]:
        raise ValueError("NML changed after preview; make a fresh preview")
    root = _parse_xml(live)
    entries = [e for e in _collection(root) if e.tag == "ENTRY"]
    regenerated, _, _ = _render(root, entries, rows)
    if regenerated != proposed:
        raise ValueError("Preview snapshot does not match current database")
    destination = backup_dir or source.parent / ".crate-digger-backups"
    destination.mkdir(parents=True, exist_ok=True)
    backup = destination / f"{source.name}.{datetime.now():%Y%m%d-%H%M%S-%f}.bak"
    source_mode = stat.S_IMODE(source.stat().st_mode)
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
    temporary.chmod(source_mode)
    replaced = False
    try:
        if _traktor_running() or source.read_bytes() != live:
            raise ValueError("Source changed or Traktor opened during apply")
        os.replace(temporary, source)
        replaced = True
        written = _parse_xml(source.read_bytes())
        _validate(root, written, {row["location_key"]: row["category"] for row in rows})
        if _digest(source.read_bytes()) != report["snapshot_sha256"]:
            raise ValueError("Written NML failed fingerprint validation")
    except Exception as error:
        if replaced and source.read_bytes() == proposed:
            with tempfile.NamedTemporaryFile(
                dir=source.parent, delete=False
            ) as restore_handle:
                restore_handle.write(live)
                restore_handle.flush()
                os.fsync(restore_handle.fileno())
                restore_path = Path(restore_handle.name)
            restore_path.chmod(source_mode)
            os.replace(restore_path, source)
        elif replaced:
            raise ValueError(
                f"NML changed after replacement; inspect the file and backup at {backup}"
            ) from error
        raise
    finally:
        temporary.unlink(missing_ok=True)
    result = {
        "changed": True,
        "backup": str(backup),
        "source": str(source),
        "preview_report": str(preview_report.resolve()),
    }
    (preview_report.parent / "organization-apply.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8"
    )
    return result
