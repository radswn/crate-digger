from pathlib import Path

import crate_digger.collection.title_suffix as title_suffix
from crate_digger.collection.title_suffix import (
    ACAPELLA_TAIL_RE,
    INSTRUMENTAL_TAIL_RE,
    TitleSuffixRule,
    ensure_path_title_suffix,
    ensure_title_suffix,
    ensure_title_suffixes,
)


def test_ensure_title_suffix_appends_instrumental_marker():
    assert (
        ensure_title_suffix(
            "Can't Decide",
            suffix="Instrumental",
            tail_pattern=INSTRUMENTAL_TAIL_RE,
        )
        == "Can't Decide (Instrumental)"
    )


def test_ensure_title_suffix_keeps_existing_canonical_marker():
    assert (
        ensure_title_suffix(
            "Can't Decide (Instrumental)",
            suffix="Instrumental",
            tail_pattern=INSTRUMENTAL_TAIL_RE,
        )
        == "Can't Decide (Instrumental)"
    )


def test_ensure_title_suffix_normalizes_trailing_marker_variants():
    assert (
        ensure_title_suffix(
            "Can't Decide - instrumental",
            suffix="Instrumental",
            tail_pattern=INSTRUMENTAL_TAIL_RE,
        )
        == "Can't Decide (Instrumental)"
    )
    assert (
        ensure_title_suffix(
            "Can't Decide (Vocals)",
            suffix="Acapella",
            tail_pattern=ACAPELLA_TAIL_RE,
        )
        == "Can't Decide (Acapella)"
    )


def test_ensure_path_title_suffix_uses_stem_when_title_missing(monkeypatch):
    written = []

    monkeypatch.setattr(title_suffix, "read_title", lambda _path: None)
    monkeypatch.setattr(
        title_suffix,
        "write_title",
        lambda path, title: written.append((path, title)),
    )

    path = Path("/music/Can't Decide.mp3")
    result = ensure_path_title_suffix(
        path,
        suffix="Instrumental",
        tail_pattern=INSTRUMENTAL_TAIL_RE,
        apply=True,
    )

    assert result.changed is True
    assert result.before_title is None
    assert result.after_title == "Can't Decide (Instrumental)"
    assert written == [(path, "Can't Decide (Instrumental)")]


def test_ensure_path_title_suffix_dry_run_does_not_write(monkeypatch):
    written = []

    monkeypatch.setattr(title_suffix, "read_title", lambda _path: "Can't Decide")
    monkeypatch.setattr(
        title_suffix,
        "write_title",
        lambda path, title: written.append((path, title)),
    )

    result = ensure_path_title_suffix(
        Path("/music/Can't Decide.mp3"),
        suffix="Instrumental",
        tail_pattern=INSTRUMENTAL_TAIL_RE,
        apply=False,
    )

    assert result.changed is True
    assert result.after_title == "Can't Decide (Instrumental)"
    assert written == []


def test_ensure_title_suffixes_applies_rules_and_filters(monkeypatch):
    files = {
        Path("/instrumentals"): [
            Path("/instrumentals/Keep Me.mp3"),
            Path("/instrumentals/Skip Me.mp3"),
        ],
        Path("/acapellas"): [Path("/acapellas/Keep Me.flac")],
    }
    titles = {
        Path("/instrumentals/Keep Me.mp3"): "Keep Me",
        Path("/instrumentals/Skip Me.mp3"): "Skip Me",
        Path("/acapellas/Keep Me.flac"): "Keep Me",
    }
    written = []

    monkeypatch.setattr(
        title_suffix,
        "discover_audio_files",
        lambda paths: files[Path(next(iter(paths)))],
    )
    monkeypatch.setattr(title_suffix, "read_title", lambda path: titles[path])
    monkeypatch.setattr(
        title_suffix,
        "write_title",
        lambda path, title: written.append((path, title)),
    )

    results = ensure_title_suffixes(
        [
            TitleSuffixRule(
                path=Path("/instrumentals"),
                suffix="Instrumental",
                tail_pattern=INSTRUMENTAL_TAIL_RE,
            ),
            TitleSuffixRule(
                path=Path("/acapellas"),
                suffix="Acapella",
                tail_pattern=ACAPELLA_TAIL_RE,
            ),
        ],
        apply=True,
        filters=["keep"],
    )

    assert [result.path for result in results] == [
        Path("/instrumentals/Keep Me.mp3"),
        Path("/acapellas/Keep Me.flac"),
    ]
    assert written == [
        (Path("/instrumentals/Keep Me.mp3"), "Keep Me (Instrumental)"),
        (Path("/acapellas/Keep Me.flac"), "Keep Me (Acapella)"),
    ]
