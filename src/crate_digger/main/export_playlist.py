import argparse
from pathlib import Path

from crate_digger.utils.config import SpotifyConfig, get_settings
from crate_digger.utils.spotify import (
    fetch_playlist_track_queries,
    get_spotify_client,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export a configured Spotify playlist to a text file."
    )
    parser.add_argument(
        "playlist",
        choices=("to-download", "acapella"),
        help="Configured playlist to export.",
    )
    parser.add_argument("output", help="Output text file path.")
    return parser.parse_args()


def get_playlist_uri(spotify_config: SpotifyConfig, playlist: str) -> str:
    if playlist == "to-download":
        return spotify_config["to_download_playlist"]
    if playlist == "acapella":
        return spotify_config["acapella_playlist"]

    raise ValueError(f"Unknown playlist export: {playlist}")


def main() -> None:
    args = parse_args()

    config = get_settings()
    spotify_config = config["spotify"]
    sp = get_spotify_client(" ".join(spotify_config["scopes"]))

    playlist_uri = get_playlist_uri(spotify_config, args.playlist)
    lines = fetch_playlist_track_queries(sp, playlist_uri)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

    print(f"Wrote {len(lines)} {args.playlist} tracks to {output_path}")


if __name__ == "__main__":
    main()
