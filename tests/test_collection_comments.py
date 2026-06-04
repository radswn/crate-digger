from pathlib import Path

from mutagen.id3 import COMM

import crate_digger.collection.comments as comments
from crate_digger.collection.comments import clean_rekordbox_comment_tags


def test_clean_rekordbox_comment_tags_keeps_only_rekordbox_blocks():
    assert (
        clean_rekordbox_comment_tags(
            "promo pool junk 6A /* Tech / House / master */ old note"
        )
        == "/* Tech / House / master */"
    )


def test_clean_rekordbox_comment_tags_normalizes_multiple_blocks():
    assert (
        clean_rekordbox_comment_tags("foo /*  Tech/House */ bar /* peak / late */")
        == "/* Tech / House */ /* peak / late */"
    )


def test_clean_rekordbox_comment_tags_clears_comments_without_rekordbox_tags():
    assert clean_rekordbox_comment_tags("promo pool junk") is None


def test_read_comment_falls_back_to_raw_id3_comment(monkeypatch):
    class Audio:
        def __init__(self, tags):
            self.tags = tags

    def fake_file(_path, *, easy):
        if easy:
            return Audio({})
        return Audio({"COMM::eng": COMM(encoding=3, lang="eng", text=["raw note"])})

    monkeypatch.setattr(comments, "File", fake_file)

    assert comments.read_comment(Path("/music/raw.mp3")) == "raw note"


def test_clear_comment_removes_existing_comment(monkeypatch):
    written = []

    monkeypatch.setattr(comments, "read_comment", lambda _path: "old note")
    monkeypatch.setattr(
        comments,
        "write_comment",
        lambda path, comment: written.append((str(path), comment)) or True,
    )

    result = comments.clear_comment(Path("/music/commented.mp3"))

    assert written == [("/music/commented.mp3", None)]
    assert result.cleaned is True
    assert result.before_comment == "old note"
    assert result.after_comment is None


def test_clear_comment_skips_empty_comment(monkeypatch):
    written = []

    monkeypatch.setattr(comments, "read_comment", lambda _path: None)
    monkeypatch.setattr(
        comments,
        "write_comment",
        lambda path, comment: written.append((str(path), comment)) or True,
    )

    result = comments.clear_comment(Path("/music/empty.mp3"))

    assert written == []
    assert result.cleaned is False
    assert result.before_comment is None
    assert result.after_comment is None
