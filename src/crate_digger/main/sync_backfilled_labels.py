import argparse
from pathlib import Path

from crate_digger.utils.config import get_settings
from crate_digger.utils.followed_labels import (
    DEFAULT_BACKFILLED_LABELS_PATH,
    load_cached_backfilled_labels,
    save_cached_backfilled_labels,
    unique_preserving_order,
)
from crate_digger.utils.spotify import (
    fetch_followed_labels_from_playlist,
    fetch_user_playlist_names,
    get_spotify_client,
    playlist_name_matches_label,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Sync the backfilled-label state from existing Spotify playlist names."
        )
    )
    parser.add_argument(
        "--config",
        default="config.toml",
        help="Path to the app config file. Default: config.toml",
    )
    parser.add_argument(
        "--state-path",
        type=Path,
        default=DEFAULT_BACKFILLED_LABELS_PATH,
        help=(
            f"Path to backfilled_labels.json. Default: {DEFAULT_BACKFILLED_LABELS_PATH}"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print matches without writing backfilled_labels.json.",
    )
    args = parser.parse_args()

    config = get_settings(args.config)
    spotify_config = config["spotify"]
    client = get_spotify_client(" ".join(spotify_config["scopes"]))

    followed_labels = fetch_followed_labels_from_playlist(
        client,
        spotify_config["followed_labels_playlist"],
    )
    playlist_names = fetch_user_playlist_names(client)

    matched: dict[str, list[str]] = {}
    for label in followed_labels:
        matches = [
            playlist_name
            for playlist_name in playlist_names
            if playlist_name_matches_label(playlist_name, label)
        ]
        if matches:
            matched[label] = matches

    cached_labels = load_cached_backfilled_labels(args.state_path)
    cached_label_set = set(cached_labels)
    matched_labels = list(matched)
    labels_to_add = [label for label in matched_labels if label not in cached_label_set]
    updated_labels = unique_preserving_order([*cached_labels, *labels_to_add])

    print(
        f"Fetched {len(followed_labels)} followed labels and "
        f"{len(playlist_names)} playlists."
    )
    print(f"Matched {len(matched_labels)} labels to existing playlist names.")

    for label, matches in matched.items():
        marker = "new" if label in labels_to_add else "cached"
        print(f"- {label} ({marker})")
        for playlist_name in matches:
            print(f"  - {playlist_name}")

    if args.dry_run:
        print(f"Dry run: would add {len(labels_to_add)} labels to {args.state_path}.")
        return

    save_cached_backfilled_labels(updated_labels, args.state_path)
    print(f"Added {len(labels_to_add)} labels to {args.state_path}.")


if __name__ == "__main__":
    main()
