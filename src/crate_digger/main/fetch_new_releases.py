from crate_digger.utils.spotify import get_spotify_client, add_new_releases_to_playlist
from crate_digger.utils.config import load_config


config = load_config()
sp = get_spotify_client("playlist-modify-private")

added_uris, snapshot = add_new_releases_to_playlist(
    sp,
    config["labels"]["names"],
    config["playlists"]["test"]
)
