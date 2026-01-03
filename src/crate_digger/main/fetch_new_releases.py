from crate_digger.utils.spotify import get_spotify_client, fetch_and_add
from crate_digger.utils.config import get_settings
from crate_digger.utils.telegram import construct_message, send_message


config = get_settings()
sp = get_spotify_client("playlist-modify-private")

track_info_to_send = fetch_and_add(
    sp,
    config["labels"]["names"],
    config["spotify"]["to_listen_playlist"]
)

if track_info_to_send:
    message = construct_message(track_info_to_send)
    send_message(message)
