from crate_digger.utils.spotify import (
    get_spotify_client,
    fetch_new_releases,
    filter_relevant_releases
)



sp = get_spotify_client("playlist-modify-private,user-library-read")

labels = [
    "Mindshake Records",
    "Solid Grooves Records",
    "Toolroom"
]

uris_to_add = []

for label in labels:
    new_releases = fetch_new_releases(sp, label)
    relevant_releases = filter_relevant_releases(new_releases)

    for release in relevant_releases:
        album_uri = release["uri"]
        album = sp.album(album_uri)

        for track in album["tracks"]["items"]:
            uris_to_add.append(track["uri"])


sp.playlist_add_items("spotify:playlist:4RZVqEcYVkzgExsDRnmwNp", uris_to_add)
