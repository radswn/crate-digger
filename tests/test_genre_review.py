import json
import sqlite3
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from crate_digger.collection.genre_review import (
    apply,
    decide,
    import_audit,
    list_reviews,
    preview,
)
from crate_digger.collection.index import _ensure_schema
from crate_digger.collection.traktor_organization import _schema as organization_schema
from crate_digger.web.genres import create_genres_router


def _setup(tmp_path: Path) -> tuple[Path, Path, Path, list[dict]]:
    nml = tmp_path / "collection.nml"
    nml.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<NML VERSION="19" CUSTOM="keep"><COLLECTION ENTRIES="2">
<ENTRY TITLE="One" ARTIST="Ada"><LOCATION VOLUME="C:" DIR="/:Music/:" FILE="one.mp3" />
<INFO GENRE="House" RATING="Comment2" RANKING="255" /><TEMPO BPM="125" />
<CUE_V2 TYPE="4" START="4" /><CUE_V2 TYPE="0" START="8" /><UNKNOWN EXTRA="yes" /><!--keep--></ENTRY>
<ENTRY TITLE="Two" ARTIST="Bea"><LOCATION VOLUME="C:" DIR="/:Music/:" FILE="two.mp3" />
<INFO GENRE="Pop" RATING="Other" /></ENTRY></COLLECTION>
<PLAYLISTS><NODE NAME="My playlist"><ENTRY KEY="one" /></NODE></PLAYLISTS></NML>""",
        encoding="utf-8",
    )
    db = tmp_path / "collection.sqlite3"
    records = []
    with sqlite3.connect(db) as conn:
        _ensure_schema(conn)
        organization_schema(conn)
        for name, genre, artist, review in (
            ("one", "House", "Ada", True),
            ("two", "Pop", "Bea", False),
        ):
            path = tmp_path / f"{name}.mp3"
            path.write_bytes(b"media")
            conn.execute(
                """insert into tracks (path,stem,size,mtime_ns,indexed_at)
                values (?,?,?,?,?)""",
                (str(path), name, 5, 1, "now"),
            )
            conn.execute(
                """insert into canonical_track_metadata
                (track_path,title,artist,genre,updated_at) values (?,?,?,?,?)""",
                (str(path), name.title(), artist, genre, "now"),
            )
            conn.execute(
                """insert into traktor_entries
                (location_key,track_path,genre,imported_fingerprint,updated_at)
                values (?,?,?,?,?)""",
                (f"C:/:Music/:{name}.mp3", str(path), genre, "hash", "now"),
            )
            records.append(
                {
                    "path": str(path),
                    "final_genre": genre,
                    "final_method": "manual audit",
                    "review": review,
                    "review_note": "Check source" if review else "",
                    "artist_evidence": [{"name": artist, "uri": "spotify:artist:abc"}],
                }
            )
    audit = tmp_path / "genre-final.json"
    audit.write_text(json.dumps(records), encoding="utf-8")
    return nml, db, audit, records


def test_import_review_reimport_preview_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nml, db, audit, records = _setup(tmp_path)
    assert import_audit(audit, nml, db)["imported"] == 2
    assert len(list_reviews(db)) == 1
    assert import_audit(audit, nml, db)["imported"] == 0
    before = nml.read_bytes()
    path = records[0]["path"]
    decide(db, path, "correct", "Radek", "Techno", "Checked recording")
    assert import_audit(audit, nml, db)["conflicts"] == []
    records[0]["review_note"] = "New evidence version"
    audit.write_text(json.dumps(records), encoding="utf-8")
    assert import_audit(audit, nml, db)["imported"] == 0
    with sqlite3.connect(db) as conn:
        assert (
            conn.execute(
                "select genre from canonical_track_metadata where track_path=?", (path,)
            ).fetchone()[0]
            == "Techno"
        )
        assert (
            conn.execute("select count(*) from genre_review_events").fetchone()[0] == 1
        )
        assert (
            conn.execute("select count(*) from genre_review_sources").fetchone()[0] == 4
        )
    assert len(list_reviews(db, "approved")) == 1
    assert len(list_reviews(db, "baseline")) == 1
    assert (
        list_reviews(db, "approved")[0]["newer_evidence"]["review_note"]
        == "New evidence version"
    )
    app = FastAPI()
    app.include_router(create_genres_router(db))
    assert (
        "Newer audit evidence (decision retained)"
        in TestClient(app).get("/genres?status=approved").text
    )
    database_before = db.read_bytes()
    report = preview(nml, db, tmp_path / "preview")
    assert nml.read_bytes() == before
    assert db.read_bytes() == database_before
    assert report["changed"] == 1 and report["conflicts"] == []
    assert len(report["tracks"]) == 2
    assert {track["path"]: track["outcome"] for track in report["tracks"]} == {
        records[0]["path"]: "change",
        records[1]["path"]: "baseline",
    }
    proposed = ET.parse(report["snapshot"]).getroot()
    original = ET.fromstring(before)
    info = proposed.find("COLLECTION/ENTRY/INFO")
    assert info is not None
    info.set("GENRE", "House")
    assert ET.tostring(proposed) == ET.tostring(original)
    monkeypatch.setattr(
        "crate_digger.collection.genre_review._traktor_running", lambda: False
    )
    result = apply(Path(report["report"]), tmp_path / "backups")
    assert result["count"] == 1
    assert Path(result["backup"]).read_bytes() == before
    assert apply(Path(report["report"]))["changed"] is False
    assert preview(nml, db, tmp_path / "next")["changed"] == 0


def test_import_conflicts_and_stale_preview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nml, db, audit, records = _setup(tmp_path)
    records[0]["path"] = str(tmp_path / "missing.mp3")
    audit.write_text(json.dumps(records), encoding="utf-8")
    result = import_audit(audit, nml, db)
    assert result["conflicts"]
    assert list_reviews(db, "all") == []
    records[0]["path"] = records[1]["path"]
    audit.write_text(json.dumps(records), encoding="utf-8")
    assert any(
        "duplicate" in conflict["reason"].lower()
        for conflict in import_audit(audit, nml, db)["conflicts"]
    )
    records[0]["path"] = str(tmp_path / "one.mp3")
    records[0]["final_genre"] = "Pop"
    audit.write_text(json.dumps(records), encoding="utf-8")
    assert import_audit(audit, nml, db)["conflicts"]
    records[0]["final_genre"] = "House"
    audit.write_text(json.dumps(records), encoding="utf-8")
    assert import_audit(audit, nml, db)["conflicts"] == []
    decide(db, records[0]["path"], "correct", "Radek", "Techno")
    nml.write_text(
        nml.read_text().replace('GENRE="House"', 'GENRE="Dance"'), encoding="utf-8"
    )
    report = preview(nml, db, tmp_path / "conflict")
    assert report["changed"] == 0 and len(report["conflicts"]) == 1
    assert (
        next(
            track for track in report["tracks"] if track["path"] == records[0]["path"]
        )["outcome"]
        == "conflict"
    )
    with pytest.raises(ValueError, match="conflicts"):
        apply(Path(report["report"]))
    nml.write_text(
        nml.read_text().replace('GENRE="Dance"', 'GENRE="House"'), encoding="utf-8"
    )
    report = preview(nml, db, tmp_path / "clean")
    monkeypatch.setattr(
        "crate_digger.collection.genre_review._traktor_running", lambda: False
    )
    nml.write_text(
        nml.read_text().replace('RANKING="255"', 'RANKING="200"'), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="changed after preview"):
        apply(Path(report["report"]))
    nml.write_text(
        nml.read_text().replace('RANKING="200"', 'RANKING="255"'), encoding="utf-8"
    )
    decide(db, records[1]["path"], "defer", "Radek")
    with pytest.raises(ValueError, match="decisions changed"):
        apply(Path(report["report"]))


def test_dashboard_review_escapes_evidence_and_saves_decision(tmp_path: Path) -> None:
    nml, db, audit, records = _setup(tmp_path)
    records[0]["review_note"] = "<script>alert(1)</script>"
    records[0]["source_url"] = "javascript:alert(1)"
    audit.write_text(json.dumps(records), encoding="utf-8")
    import_audit(audit, nml, db)
    app = FastAPI()
    app.include_router(create_genres_router(db))
    client = TestClient(app)
    response = client.get("/genres")
    assert response.status_code == 200
    assert "Pending (1)" in response.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in response.text
    assert 'href="javascript:' not in response.text
    response = client.post(
        "/genres/decide",
        data={
            "path": records[0]["path"],
            "status": "pending",
            "action": "correct",
            "genre": "Techno",
            "actor": "Radek",
            "note": "Checked",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "Pending (0)" in response.text
    reviewed = client.get("/api/genres?status=approved").json()
    assert reviewed["count"] == 1
    assert client.get("/api/genres?status=baseline").json()["count"] == 1
    assert (
        next(
            row for row in reviewed["tracks"] if row["track_path"] == records[0]["path"]
        )["current_genre"]
        == "Techno"
    )


def test_approval_and_deferral_survive_reimport(tmp_path: Path) -> None:
    nml, db, audit, records = _setup(tmp_path)
    import_audit(audit, nml, db)
    path = records[0]["path"]
    decide(db, path, "defer", "Radek", note="Need another listen")
    assert len(list_reviews(db, "deferred")) == 1
    assert import_audit(audit, nml, db)["pending"] == 0
    decide(db, path, "approve", "Radek", note="Confirmed")
    assert import_audit(audit, nml, db)["pending"] == 0
    row = next(row for row in list_reviews(db, "approved") if row["track_path"] == path)
    assert row["current_genre"] == "House"
    assert row["decision_by"] == "Radek"
    assert preview(nml, db, tmp_path / "preview")["changed"] == 0


def test_new_audit_evidence_updates_pending_review_without_changing_genre(
    tmp_path: Path,
) -> None:
    nml, db, audit, records = _setup(tmp_path)
    assert import_audit(audit, nml, db)["imported"] == 2
    first_audit = audit.read_bytes()
    path = records[0]["path"]
    records[0].update(
        final_genre="Techno",
        final_method="new source",
        review_note="New recording says Techno",
        source_url="https://music.apple.com/new",
    )
    audit.write_text(json.dumps(records), encoding="utf-8")
    result = import_audit(audit, nml, db)
    assert result["conflicts"] == []
    assert result["updated"] == 1
    pending = list_reviews(db, "pending")
    assert len(pending) == 1
    assert pending[0]["proposed_genre"] == "Techno"
    assert pending[0]["method"] == "new source"
    assert pending[0]["evidence"]["source_url"] == "https://music.apple.com/new"
    assert pending[0]["current_genre"] == "House"
    assert import_audit(audit, nml, db)["updated"] == 0
    audit.write_bytes(first_audit)
    assert any(
        "older imported version" in conflict["reason"]
        for conflict in import_audit(audit, nml, db)["conflicts"]
    )
    assert list_reviews(db, "pending")[0]["proposed_genre"] == "Techno"
    audit.write_text(json.dumps(records), encoding="utf-8")
    report = preview(nml, db, tmp_path / "preview")
    assert report["changed"] == 0
    assert (
        next(track for track in report["tracks"] if track["path"] == path)["outcome"]
        == "pending"
    )
    decide(db, path, "approve", "Radek")
    assert preview(nml, db, tmp_path / "approved")["changed"] == 1


def test_changed_baseline_proposal_becomes_pending(tmp_path: Path) -> None:
    nml, db, audit, records = _setup(tmp_path)
    import_audit(audit, nml, db)
    records[1]["final_genre"] = "Techno"
    audit.write_text(json.dumps(records), encoding="utf-8")
    result = import_audit(audit, nml, db)
    assert result["updated"] == 1
    assert len(list_reviews(db, "baseline")) == 0
    pending = next(
        row
        for row in list_reviews(db, "pending")
        if row["track_path"] == records[1]["path"]
    )
    assert pending["current_genre"] == "Pop"
    assert pending["proposed_genre"] == "Techno"
    assert preview(nml, db, tmp_path / "preview")["changed"] == 0


def test_existing_audit_has_67_review_flags() -> None:
    audit = Path(__file__).resolve().parents[1] / "exports/genres/genre-final.json"
    records = json.loads(audit.read_text(encoding="utf-8"))
    assert len(records) == 909
    assert sum(record["review"] is True for record in records) == 67
