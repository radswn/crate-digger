import os
import requests

from typing import Dict, List, Sequence

from crate_digger.utils.markdownv2 import bold, escape_markdown_v2
from crate_digger.utils.logging import get_logger, pluralize
from crate_digger.utils.types import SpotifyTrack


logger = get_logger(__name__)


def send_message(message: str) -> None:
    """Send a message via Telegram Bot API with MarkdownV2 formatting.

    Args:
        message: Message text with MarkdownV2 formatting

    Raises:
        requests.RequestException: If Telegram API request fails
    """
    url = f"https://api.telegram.org/bot{os.getenv('TELEGRAM_BOT_TOKEN')}/sendMessage"
    data = {
        "chat_id": os.getenv("TELEGRAM_CHAT_ID"),
        "text": message,
        "parse_mode": "MarkdownV2",
    }

    try:
        resp = requests.post(url, data=data)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.error(f"Telegram request failed: {e}")
        raise


def construct_message(
    releases_info: Dict[str, Dict[str, List[SpotifyTrack]]],
    labels_added: Sequence[str] | None = None,
    labels_removed: Sequence[str] | None = None,
    labels_backfilled: Sequence[str] | None = None,
) -> str:
    """Construct a compact Telegram summary.

    Args:
        releases_info: Dict mapping labels to their releases and tracks
        labels_added: Labels newly present in the followed-label playlist
        labels_removed: Labels no longer present in the followed-label playlist
        labels_backfilled: Added labels whose history was backfilled

    Returns:
        MarkdownV2-formatted message string
    """
    labels_added = labels_added or []
    labels_removed = labels_removed or []
    labels_backfilled = labels_backfilled or []

    n_releases_found = sum(len(releases) for releases in releases_info.values())
    lines = [
        bold(f"{n_releases_found} new {pluralize(n_releases_found, 'release')} found")
    ]

    if labels_added or labels_removed:
        change_parts = []
        if labels_added:
            change_parts.append(
                f"{len(labels_added)} {pluralize(len(labels_added), 'label')} added"
            )
        if labels_removed:
            change_parts.append(
                f"{len(labels_removed)} {pluralize(len(labels_removed), 'label')} removed"
            )

        lines.append(bold("Followed labels updated: " + ", ".join(change_parts)))

    if labels_added:
        lines.append("Added: " + escape_markdown_v2(", ".join(labels_added)))

    if labels_removed:
        lines.append("Removed: " + escape_markdown_v2(", ".join(labels_removed)))

    if labels_backfilled:
        lines.append(
            "Backfilled history for " + escape_markdown_v2(", ".join(labels_backfilled))
        )

    return "\n".join(lines)
