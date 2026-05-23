import json

from crate_digger.utils.followed_labels import (
    DEFAULT_BACKFILLED_LABELS_PATH,
    DEFAULT_PIPELINE_STATE_DIR,
    DEFAULT_STATE_PATH,
    LEGACY_BACKFILLED_LABELS_PATH,
    LEGACY_STATE_PATH,
    compute_followed_label_changes,
    compute_labels_to_backfill,
    load_cached_backfilled_labels,
    load_followed_labels_state,
    save_cached_backfilled_labels,
    save_followed_labels_state,
    unique_preserving_order,
)


def test_unique_preserving_order_drops_blanks_and_duplicates():
    labels = unique_preserving_order([" A ", "", "B", "A", "B"])
    assert labels == ["A", "B"]


def test_compute_followed_label_changes_initializes_without_added_or_removed():
    changes = compute_followed_label_changes(["A", "B"], previous_labels=None)

    assert changes.current == ["A", "B"]
    assert changes.added == []
    assert changes.removed == []
    assert changes.initialized is True


def test_compute_followed_label_changes_detects_added_and_removed():
    changes = compute_followed_label_changes(["B", "C"], previous_labels=["A", "B"])

    assert changes.added == ["C"]
    assert changes.removed == ["A"]
    assert changes.initialized is False


def test_compute_labels_to_backfill_includes_followed_labels_missing_from_cache():
    labels = compute_labels_to_backfill(
        current_labels=["A", "B", "C"],
        added_labels=[],
        cached_backfilled_labels=["A"],
    )

    assert labels == ["B", "C"]


def test_compute_labels_to_backfill_keeps_first_run_bootstrap_quiet():
    labels = compute_labels_to_backfill(
        current_labels=["A", "B"],
        added_labels=[],
        cached_backfilled_labels=[],
        initialized=True,
    )

    assert labels == []


def test_compute_labels_to_backfill_prefers_added_order_and_dedupes_cached():
    labels = compute_labels_to_backfill(
        current_labels=["A", "B", "C"],
        added_labels=["C", "B", "B"],
        cached_backfilled_labels=["A", "C"],
    )

    assert labels == ["B"]


def test_followed_label_state_round_trips(tmp_path):
    state_path = tmp_path / "state" / "followed_labels.json"

    save_followed_labels_state(["A", "B", "A"], state_path)

    assert json.loads(state_path.read_text()) == {"labels": ["A", "B"]}
    assert load_followed_labels_state(state_path) == ["A", "B"]


def test_backfilled_label_state_round_trips(tmp_path):
    state_path = tmp_path / "state" / "backfilled_labels.json"

    save_cached_backfilled_labels(["A", "B", "A"], state_path)

    assert json.loads(state_path.read_text()) == {"labels": ["A", "B"]}
    assert load_cached_backfilled_labels(state_path) == ["A", "B"]


def test_default_followed_label_state_lives_under_fetch_pipeline():
    assert DEFAULT_PIPELINE_STATE_DIR == DEFAULT_STATE_PATH.parent
    assert DEFAULT_PIPELINE_STATE_DIR == DEFAULT_BACKFILLED_LABELS_PATH.parent
    assert DEFAULT_STATE_PATH.name == "followed_labels.json"
    assert DEFAULT_BACKFILLED_LABELS_PATH.name == "backfilled_labels.json"


def test_default_followed_label_loaders_fall_back_to_legacy_paths(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    LEGACY_STATE_PATH.parent.mkdir(parents=True)
    LEGACY_STATE_PATH.write_text('{"labels": ["A", "B", "A"]}', encoding="utf-8")
    LEGACY_BACKFILLED_LABELS_PATH.write_text(
        '{"labels": ["Done", "Done"]}',
        encoding="utf-8",
    )

    assert load_followed_labels_state() == ["A", "B"]
    assert load_cached_backfilled_labels() == ["Done"]

    save_followed_labels_state(["C"])
    save_cached_backfilled_labels(["New"])

    assert DEFAULT_STATE_PATH.exists()
    assert DEFAULT_BACKFILLED_LABELS_PATH.exists()
