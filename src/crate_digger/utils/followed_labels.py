import json

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


DEFAULT_PIPELINE_STATE_DIR = Path(".crate_digger_state") / "fetch_pipeline"
DEFAULT_STATE_PATH = DEFAULT_PIPELINE_STATE_DIR / "followed_labels.json"
DEFAULT_BACKFILLED_LABELS_PATH = DEFAULT_PIPELINE_STATE_DIR / "backfilled_labels.json"
LEGACY_STATE_PATH = Path(".crate_digger_state") / "followed_labels.json"
LEGACY_BACKFILLED_LABELS_PATH = Path(".crate_digger_state") / "backfilled_labels.json"


@dataclass(frozen=True)
class FollowedLabelChanges:
    current: list[str]
    added: list[str]
    removed: list[str]
    initialized: bool = False


def unique_preserving_order(values: Sequence[str]) -> list[str]:
    """Return non-empty unique values, preserving first-seen order."""

    unique = []
    seen = set()

    for value in values:
        normalized = value.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        unique.append(normalized)

    return unique


def load_followed_labels_state(
    state_path: Path = DEFAULT_STATE_PATH,
) -> list[str] | None:
    """Load the previously followed labels, if state exists."""

    state_path = _with_legacy_fallback(state_path, LEGACY_STATE_PATH)
    if not state_path.exists():
        return None

    with state_path.open("r", encoding="utf-8") as f:
        payload = json.load(f)

    labels = payload.get("labels")
    if not isinstance(labels, list) or not all(
        isinstance(label, str) for label in labels
    ):
        raise ValueError(f"Invalid followed label state in {state_path}")

    return unique_preserving_order(labels)


def save_followed_labels_state(
    labels: Sequence[str], state_path: Path = DEFAULT_STATE_PATH
) -> None:
    """Persist the current followed labels for change detection next run."""

    state_path.parent.mkdir(parents=True, exist_ok=True)

    payload = {"labels": unique_preserving_order(labels)}
    with state_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")


def load_cached_backfilled_labels(
    state_path: Path = DEFAULT_BACKFILLED_LABELS_PATH,
) -> list[str]:
    """Load labels this app has backfilled itself."""

    state_path = _with_legacy_fallback(state_path, LEGACY_BACKFILLED_LABELS_PATH)
    if not state_path.exists():
        return []

    with state_path.open("r", encoding="utf-8") as f:
        payload = json.load(f)

    labels = payload.get("labels")
    if not isinstance(labels, list) or not all(
        isinstance(label, str) for label in labels
    ):
        raise ValueError(f"Invalid backfilled label state in {state_path}")

    return unique_preserving_order(labels)


def save_cached_backfilled_labels(
    labels: Sequence[str], state_path: Path = DEFAULT_BACKFILLED_LABELS_PATH
) -> None:
    """Persist labels this app has backfilled itself."""

    state_path.parent.mkdir(parents=True, exist_ok=True)

    payload = {"labels": unique_preserving_order(labels)}
    with state_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")


def compute_followed_label_changes(
    current_labels: Sequence[str], previous_labels: Sequence[str] | None
) -> FollowedLabelChanges:
    """Compare current playlist-derived labels with the previous state."""

    current = unique_preserving_order(current_labels)

    if previous_labels is None:
        return FollowedLabelChanges(
            current=current, added=[], removed=[], initialized=True
        )

    previous = unique_preserving_order(previous_labels)
    current_set = set(current)
    previous_set = set(previous)

    added = [label for label in current if label not in previous_set]
    removed = [label for label in previous if label not in current_set]

    return FollowedLabelChanges(
        current=current, added=added, removed=removed, initialized=False
    )


def _with_legacy_fallback(state_path: Path, legacy_path: Path) -> Path:
    if state_path == DEFAULT_STATE_PATH and not state_path.exists():
        return legacy_path
    if state_path == DEFAULT_BACKFILLED_LABELS_PATH and not state_path.exists():
        return legacy_path
    return state_path
