import json

from crate_digger.utils.followed_labels import (
    compute_followed_label_changes,
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
