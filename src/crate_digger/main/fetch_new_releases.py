from crate_digger.utils.spotify import (
    get_spotify_client,
    fetch_new_releases,
    filter_relevant_releases,
    get_track_uris_for_album
)
from crate_digger.utils.config import load_config


config = load_config()
sp = get_spotify_client("playlist-modify-private,user-library-read")

uris_to_add = []

for label in config["labels"]["names"]:
    new_releases = fetch_new_releases(sp, label)
    relevant_releases = filter_relevant_releases(new_releases)

    for release in relevant_releases:
        uris_to_add.extend(get_track_uris_for_album(sp, release["uri"]))

sp.playlist_add_items(config["playlists"]["to-listen-test"], uris_to_add)
