from crate_digger.utils.spotify import get_spotify_client, fetch_and_add
from crate_digger.utils.config import load_config
from crate_digger.utils.telegram import construct_message, send_message


config = load_config()
sp = get_spotify_client("playlist-modify-private")

track_info_to_send = fetch_and_add(
    sp,
    config["labels"]["names"],
    config["spotify"]["test-playlist"]
)

if track_info_to_send:
    message = construct_message(track_info_to_send)
else:
    message = "No new tracks found, come back in 2 weeks :("

send_message(message)