import argparse
import os
from pathlib import Path

from dotenv import load_dotenv
from spotipy.oauth2 import SpotifyOAuth

from crate_digger.utils.spotify import SpotifyCacheFileHandler, normalize_spotify_scope


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Initiate and cache a Spotify OAuth token for a client ID and scope."
    )
    parser.add_argument(
        "scope",
        help='OAuth scope string, for example "playlist-modify-private"',
    )
    parser.add_argument(
        "--client-id",
        default=None,
        help="Spotify app client ID. Defaults to SPOTIPY_CLIENT_ID.",
    )
    parser.add_argument(
        "--client-secret",
        default=None,
        help="Spotify app client secret. Defaults to SPOTIPY_CLIENT_SECRET.",
    )
    parser.add_argument(
        "--redirect-uri",
        default=None,
        help="OAuth redirect URI. Defaults to SPOTIPY_REDIRECT_URI.",
    )
    parser.add_argument(
        "--cache-path",
        type=Path,
        default=None,
        help="Token cache path. Defaults to .spotipy_cache/.cache-<scope>.",
    )
    return parser.parse_args()


def main() -> int:
    load_dotenv()
    args = parse_args()

    scope = normalize_spotify_scope(args.scope)
    client_id = args.client_id or os.environ.get("SPOTIPY_CLIENT_ID")
    client_secret = args.client_secret or os.environ.get("SPOTIPY_CLIENT_SECRET")
    redirect_uri = args.redirect_uri or os.environ.get("SPOTIPY_REDIRECT_URI")

    missing = [
        name
        for name, value in (
            ("SPOTIPY_CLIENT_ID", client_id),
            ("SPOTIPY_CLIENT_SECRET", client_secret),
            ("SPOTIPY_REDIRECT_URI", redirect_uri),
        )
        if not value
    ]
    if missing:
        raise SystemExit(f"Missing required value(s): {', '.join(missing)}")

    project_root = Path(__file__).resolve().parents[3]
    cache_path = args.cache_path or project_root / f".cache-{scope}"

    auth = SpotifyOAuth(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        scope=scope,
        cache_handler=SpotifyCacheFileHandler(cache_path),
        open_browser=True,
    )
    token_info = auth.get_access_token(as_dict=True)

    cached_scope = token_info.get("scope", scope)
    print(f"Cached Spotify token for scope {cached_scope!r} at {cache_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
