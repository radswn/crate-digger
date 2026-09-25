import shutil
import subprocess
import wave
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from mutagen import File
from mutagen.id3 import COMM, ID3, TCON, TIT2, TPE1
from mutagen.wave import WAVE

from crate_digger.collection.dj_curation import save_curation
from crate_digger.collection.index import refresh_track_metadata
from crate_digger.collection.tag_publish import apply_tags, preview_tags
from crate_digger.web.app import create_app


def prepared_track(tmp_path: Path) -> tuple[Path, Path]:
    path = tmp_path / "test.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(b"\x00\x00" * 800)
    audio = WAVE(path)
    audio.add_tags()
    assert isinstance(audio.tags, ID3)
    audio.tags.add(TIT2(encoding=3, text=["Example"]))
    audio.tags.add(TPE1(encoding=3, text=["DJ"]))
    audio.tags.add(TCON(encoding=3, text=["Old genre"]))
    audio.tags.add(COMM(encoding=3, lang="eng", desc="", text=["Original note"]))
    audio.tags.add(COMM(encoding=3, lang="eng", desc="source", text=["Other comment"]))
    audio.save()
    db = tmp_path / "collection.sqlite3"
    assert refresh_track_metadata(db, path=str(path))
    assert save_curation(
        db,
        str(path),
        genre="House",
        energy=3,
        tone=1,
        character=["funky"],
        vocal_presence=None,
        collection_category="DOWNLOAD",
    )
    return db, path


def test_publish_wav_tags_preserves_other_comments_and_exact_backup(tmp_path: Path):
    db, path = prepared_track(tmp_path)
    original = path.read_bytes()
    preview = preview_tags(db, str(path))
    assert preview["before"]["genre"] == "Old genre"
    assert preview["change_needed"]
    assert "Original note" in preview["after"]["comment"]
    assert '"energy":3' in preview["after"]["comment"]

    result = apply_tags(db, str(path), preview["fingerprint"])
    assert result["changed"]
    assert Path(result["backup"]).read_bytes() == original
    audio = WAVE(path)
    assert isinstance(audio.tags, ID3)
    assert audio.tags.get("TCON").text == ["House"]
    assert audio.tags.get("COMM:source:eng").text == ["Other comment"]
    assert audio.tags.get("COMM::eng").text[0] == preview["after"]["comment"]
    assert not preview_tags(db, str(path))["change_needed"]


def test_publish_rejects_stale_file_and_review(tmp_path: Path):
    db, path = prepared_track(tmp_path)
    preview = preview_tags(db, str(path))
    save_curation(
        db,
        str(path),
        genre="Techno",
        energy=3,
        tone=1,
        character=["funky"],
        vocal_presence=None,
        collection_category="DOWNLOAD",
    )
    with pytest.raises(ValueError, match="changed"):
        apply_tags(db, str(path), preview["fingerprint"])
    assert not list(tmp_path.glob("*.crate-digger-backup-*"))
    current = preview_tags(db, str(path))
    path.write_bytes(path.read_bytes() + b"extra")
    with pytest.raises(ValueError, match="changed"):
        apply_tags(db, str(path), current["fingerprint"])


def test_preview_rejects_malformed_managed_comment(tmp_path: Path):
    db, path = prepared_track(tmp_path)
    audio = WAVE(path)
    assert isinstance(audio.tags, ID3)
    audio.tags.get("COMM::eng").text = ["Note [[CRATE_DIGGER_DJ:broken]]"]
    audio.save()
    with pytest.raises(ValueError, match="Malformed"):
        preview_tags(db, str(path))


def test_web_tag_preview_does_not_write_without_confirmation(tmp_path: Path):
    db, path = prepared_track(tmp_path)
    config = tmp_path / "config.toml"
    config.write_text("[collection]\nmusic-dirs = []\n", encoding="utf-8")
    client = TestClient(create_app(config_path=str(config), db_path=db))
    original = path.read_bytes()
    page = client.get("/curate/tags", params={"path": str(path)})
    assert page.status_code == 200
    assert "Original note" in page.text
    assert path.read_bytes() == original
    fingerprint = preview_tags(db, str(path))["fingerprint"]
    denied = client.post(
        "/curate/tags",
        data={"path": str(path), "fingerprint": fingerprint},
        follow_redirects=True,
    )
    assert "Confirm the file tag changes" in denied.text
    assert path.read_bytes() == original


@pytest.mark.parametrize("suffix", [".mp3", ".flac", ".m4a", ".ogg", ".opus"])
def test_publish_round_trip_on_generated_audio_copies(tmp_path: Path, suffix: str):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg unavailable")
    _, source = prepared_track(tmp_path)
    path = tmp_path / f"generated{suffix}"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-ar",
            "48000" if suffix == ".opus" else "44100",
            "-metadata",
            "title=Generated",
            "-metadata",
            "artist=DJ",
            "-metadata",
            "genre=Old genre",
            "-metadata",
            "comment=Unrelated note",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    tagged = File(path, easy=False)
    assert tagged is not None and tagged.tags is not None
    if suffix == ".mp3":
        tagged.tags.add(COMM(encoding=3, lang="eng", desc="", text=["Unrelated note"]))
    elif suffix in {".flac", ".ogg", ".opus"}:
        tagged.tags["comment"] = ["Unrelated note"]
    tagged.save()
    db = tmp_path / f"{suffix[1:]}.sqlite3"
    assert refresh_track_metadata(db, path=str(path))
    assert save_curation(
        db,
        str(path),
        genre="House",
        energy=3,
        tone=1,
        character=["funky"],
        vocal_presence=None,
        collection_category="DOWNLOAD",
    )
    preview = preview_tags(db, str(path))
    assert preview["before"]["genre"] == "Old genre"
    assert "Unrelated note" in preview["after"]["comment"]
    original = path.read_bytes()
    result = apply_tags(db, str(path), preview["fingerprint"])
    assert Path(result["backup"]).read_bytes() == original
    after = preview_tags(db, str(path))
    assert not after["change_needed"]
    assert after["before"]["genre"] == "House"
