from crate_digger.utils.spotify import get_spotify_client, add_new_releases_to_playlist
from crate_digger.utils.config import load_config
from crate_digger.utils.telegram import construct_message, send_message


config = load_config()
sp = get_spotify_client("playlist-modify-private")

track_info_to_send, snapshot = add_new_releases_to_playlist(
    sp,
    config["labels"]["names"],
    config["spotify"]["test-playlist"]
)

if track_info_to_send:
    message = construct_message(track_info_to_send)
    send_message(message)
