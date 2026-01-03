import sys

from crate_digger.utils.spotify import (
    get_spotify_client,
    fetch_all_release_uris,
    collect_tracks_from_albums,
    create_playlists
)


if len(sys.argv) < 2:
    print("Usage: python backfill_label_history.py '<label name>'")
    sys.exit(1)

label = " ".join(sys.argv[1:])

sp = get_spotify_client("playlist-modify-private")

release_uris = fetch_all_release_uris(sp, label)

uris_to_add = collect_tracks_from_albums(sp, release_uris, label)

create_playlists(sp, label, uris_to_add)
