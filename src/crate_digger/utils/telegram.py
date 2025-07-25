import os
import requests

from typing import Dict, List

from crate_digger.utils.markdownv2 import bold, underline
from crate_digger.utils.logging import get_logger


logger = get_logger(__name__)


def send_message(message: str) -> None:
    url = f"https://api.telegram.org/bot{os.getenv('TELEGRAM_BOT_TOKEN')}/sendMessage"
    data = {
        "chat_id": os.getenv("TELEGRAM_CHAT_ID"),
        "text": message,
        "parse_mode": "MarkdownV2"
    }

    try:
        resp = requests.post(url, data=data)
        resp.raise_for_status()
    except Exception as e:
        logger.error(f"Failed to send Telegram message: {e}")


def construct_message(releases_info: Dict[str, Dict[str, List[Dict]]]) -> str:
    n_tracks_found = sum([len(tracks) for release in releases_info.values() for tracks in release.values()])

    message_text = "❗" + bold("NEW RELEASES") + "❗" + "\n\n"
    message_text += "🎵 FOUND " + bold(str(n_tracks_found)) + " TRACKS 🎵" + "\n"

    for label, release_titles in releases_info.items():
        message_text += "\n\n" + "🎤 " + escape_markdown_v2(label).upper() + "\n"

        for release, tracks in release_titles.items():
            message_text += "\n" + "💿 " +  escape_markdown_v2(release) + "\n"

            for i, track in enumerate(tracks):
                joined_artists = ", ".join([artist["name"] for artist in track["artists"]])
                title = track["name"]

                track_line = escape_markdown_v2(f"{i + 1}. {joined_artists} - {title}")
                if track["is_added"]:
                    track_line = underline(track_line)
                message_text += track_line + "\n"

    return message_text


def escape_markdown_v2(text: str) -> str:
    to_escape = r"_*[]()~`>#+-=|{}.!"
    return ''.join(f"\\{c}" if c in to_escape else c for c in text)
