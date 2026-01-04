import os
import requests

from typing import Dict, List

from crate_digger.utils.markdownv2 import bold, escape_markdown_v2
from crate_digger.utils.logging import get_logger
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


def construct_message(releases_info: Dict[str, Dict[str, List[SpotifyTrack]]]) -> str:
    """Construct a formatted Telegram message from release information.

    Args:
        releases_info: Dict mapping labels to their releases and tracks

    Returns:
        MarkdownV2-formatted message string
    """
    n_tracks_found = sum(
        [
            len(tracks)
            for release in releases_info.values()
            for tracks in release.values()
        ]
    )

    message_text = "❗" + bold("NEW RELEASES") + "❗" + "\n\n"
    message_text += "🎵 FOUND " + bold(str(n_tracks_found)) + " TRACKS 🎵" + "\n"

    for label, release_titles in releases_info.items():
        message_text += "\n\n" + "🎤 " + escape_markdown_v2(label).upper() + "\n\n"

        for release in release_titles.keys():
            message_text += "💿 " + escape_markdown_v2(release) + "\n"

    return message_text
