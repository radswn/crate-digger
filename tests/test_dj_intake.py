from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from crate_digger.collection.dj_curation import save_curation
from crate_digger.collection.intake import (
    intake_history,
    list_intake_checks,
    review_intake_check,
)
from crate_digger.web.app import create_app
from tests.test_dj_curation import config_file, indexed_track


def check(db: Path, path: str, key: str):
    return next(item for item in list_intake_checks(db, path) if item["key"] == key)


def test_intake_approvals_expire_when_evidence_changes(tmp_path: Path):
    db, path = indexed_track(tmp_path)
    metadata = check(db, path, "metadata")
    assert metadata["can_approve"]
    assert review_intake_check(
        db,
        path,
        "metadata",
        "approved",
        fingerprint=metadata["fingerprint"],
        confirmed=True,
    )
    assert check(db, path, "metadata")["status"] == "approved"
    assert not review_intake_check(
        db,
        path,
        "metadata",
        "approved",
        fingerprint=metadata["fingerprint"],
        confirmed=True,
    )
    import sqlite3

    with sqlite3.connect(db) as conn:
        conn.execute("update tracks set title = 'Changed' where path = ?", (path,))
    assert check(db, path, "metadata")["status"] == "stale"
    with pytest.raises(ValueError, match="evidence changed"):
        review_intake_check(
            db,
            path,
            "metadata",
            "approved",
            fingerprint=metadata["fingerprint"],
            confirmed=True,
        )
    assert len(intake_history(db, path)) == 1


def test_override_reason_and_classification_review(tmp_path: Path):
    db, path = indexed_track(tmp_path)
    identity = check(db, path, "identity")
    assert not identity["can_approve"]
    with pytest.raises(ValueError, match="Required evidence"):
        review_intake_check(
            db,
            path,
            "identity",
            "approved",
            fingerprint=identity["fingerprint"],
            confirmed=True,
        )
    with pytest.raises(ValueError, match="reason"):
        review_intake_check(
            db,
            path,
            "identity",
            "overridden",
            fingerprint=identity["fingerprint"],
            confirmed=True,
        )
    assert review_intake_check(
        db,
        path,
        "identity",
        "overridden",
        fingerprint=identity["fingerprint"],
        confirmed=True,
        note="No Spotify match exists",
    )
    assert check(db, path, "identity")["status"] == "overridden"
    save_curation(
        db,
        path,
        genre="House",
        energy=3,
        tone=0,
        character=["rolling"],
        vocal_presence=None,
        collection_category="DOWNLOAD",
    )
    classification = check(db, path, "classification")
    assert classification["can_approve"]
    assert review_intake_check(
        db,
        path,
        "classification",
        "approved",
        fingerprint=classification["fingerprint"],
        confirmed=True,
    )
    save_curation(
        db,
        path,
        genre="House",
        energy=3,
        tone=1,
        character=["rolling"],
        vocal_presence=None,
        collection_category="DOWNLOAD",
    )
    assert check(db, path, "classification")["status"] == "stale"


def test_audio_integrity_check_rejects_changed_file_and_web_saves_review(
    tmp_path: Path,
):
    db, path = indexed_track(tmp_path)
    client = TestClient(create_app(config_path=str(config_file(tmp_path)), db_path=db))
    page = client.get("/curate", params={"path": path})
    assert page.status_code == 200
    assert "Intake checklist" in page.text
    audio = check(db, path, "audio_integrity")
    response = client.post(
        "/curate/check",
        data={
            "path": path,
            "check_key": "audio_integrity",
            "decision": "approved",
            "fingerprint": audio["fingerprint"],
            "confirmed": "yes",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "Intake check saved" in response.text
    assert check(db, path, "audio_integrity")["status"] == "approved"
    Path(path).write_bytes(b"changed")
    assert check(db, path, "audio_integrity")["status"] == "stale"
