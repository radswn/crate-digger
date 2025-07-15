import spotipy

from typing import List, Dict
from dotenv import load_dotenv

from spotipy.oauth2 import SpotifyOAuth



def get_spotify_client(scope: str) -> spotipy.Spotify:
    load_dotenv()
    auth = SpotifyOAuth(scope=scope)
    sp = spotipy.Spotify(auth_manager=auth)
    return sp


def fetch_new_releases(client: spotipy.Spotify, label: str) -> List[Dict]:
    new_releases = client.search(f"label:{label} tag:new", type="album")["albums"]["items"]
    return new_releases


def filter_relevant_releases(releases: List[Dict]) -> List[Dict]:
    eps_and_singles = [release for release in releases if release["album_type"].lower() in ("ep", "single")]
    return eps_and_singles


def get_track_uris_for_album(client: spotipy.Spotify, album_uri: str) -> List[str]:
    album_tracks = client.album_tracks(album_uri)["items"]
    track_uris = [track["uri"] for track in album_tracks]
    return track_uris
