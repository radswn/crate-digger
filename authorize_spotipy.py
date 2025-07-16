from crate_digger.utils.config import load_config
from crate_digger.utils.spotify import get_spotify_client

from pathlib import Path
from dotenv import load_dotenv

import urllib.parse
import os


load_dotenv()

cache_folder = Path(".spotipy_cache")
get_cache_path = lambda scope: cache_folder / f".cache-{scope}"

get_auth_url = lambda s: "https://accounts.spotify.com/authorize?" + \
    f"client_id={os.getenv('SPOTIPY_CLIENT_ID')}&" + \
        "response_type=code&" + \
            f"redirect_uri={urllib.parse.quote(os.getenv('SPOTIPY_REDIRECT_URI'))}&" + \
                f"scope={s.replace(',', '+')}"


grasses_uri = "spotify:track:7HODJrjN4MkIRWdrTlqjiM"
test_playlist = load_config()["playlists"]["test"]

scopes_validation = {
    "playlist-modify-private": [
        lambda client: client.playlist_add_items(test_playlist, [grasses_uri]),
        lambda client: client.playlist_remove_all_occurrences_of_items(test_playlist, [grasses_uri])
    ],
    "playlist-read-private": [
        lambda client: client.playlist(test_playlist)
    ],
    "user-library-read": [
        lambda client: client.current_user_saved_tracks()
    ]
}


if not cache_folder.exists():
    cache_folder.mkdir()

for scope, validation_functions in scopes_validation.items():
    sp = get_spotify_client(scope)

    if not get_cache_path(scope).exists():
        print(f"Cached token for scope {scope} not found, authorize using {get_auth_url(scope)}")

    for func in validation_functions:
        func(sp)

    print(f"Authorized for {scope} scope")

print("Authorized for all scopes")
