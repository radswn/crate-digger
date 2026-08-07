import tomllib
from functools import lru_cache

from typing import Dict, List, TypedDict, cast


class SpotifyConfig(TypedDict):
    """Expected structure of the `[spotify]` section."""

    to_listen_playlist: str
    test_playlist: str
    followed_labels_playlist: str
    to_download_playlist: str
    acapella_playlist: str
    scopes: List[str]


class CollectionConfig(TypedDict):
    """Expected structure of the optional `[collection]` section."""

    music_dirs: List[str]


class DiscoveryConfig(TypedDict):
    """Configuration for deterministic discovery heuristics."""

    freshness_days: int
    label_aliases: Dict[str, str]


class AppConfig(TypedDict):
    spotify: SpotifyConfig
    collection: CollectionConfig
    discovery: DiscoveryConfig


def _require_keys(section: Dict, required: List[str], section_name: str) -> None:
    """Validate that all required keys are present in a config section.

    Args:
        section: Config section dict
        required: List of required key names
        section_name: Section name for error messages

    Raises:
        ValueError: If any required keys are missing
    """
    missing = [k for k in required if k not in section]
    if missing:
        raise ValueError(f"Missing keys in [{section_name}]: {', '.join(missing)}")


def _assert_str(value: object, key: str, section_name: str) -> str:
    """Assert that a config value is a string.

    Args:
        value: Config value to check
        key: Config key name
        section_name: Section name for error messages

    Returns:
        The value cast to str

    Raises:
        ValueError: If value is not a string
    """
    if not isinstance(value, str):
        raise ValueError(f"Expected [{section_name}].{key} to be a string")
    return value


def _validate_list_of_strings(value: object, key: str, section_name: str) -> List[str]:
    """Validate that a config value is a list of strings.

    Args:
        value: Config value to check
        key: Config key name
        section_name: Section name for error messages

    Returns:
        The value cast to List[str]

    Raises:
        ValueError: If value is not a list of strings
    """
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"Expected [{section_name}].{key} to be a list of strings")
    return cast(List[str], value)


def load_config(config_path: str = "config.toml") -> AppConfig:
    """Load and validate application configuration from TOML."""

    with open(config_path, "rb") as f:
        raw = tomllib.load(f)

    if "spotify" not in raw:
        raise ValueError("Config must contain [spotify] section")

    spotify_section = raw["spotify"]

    _require_keys(
        spotify_section,
        [
            "to-listen-playlist",
            "test-playlist",
            "followed-labels-playlist",
            "to-download-playlist",
            "acapella-playlist",
            "scopes",
        ],
        "spotify",
    )

    spotify_cfg: SpotifyConfig = {
        "to_listen_playlist": _assert_str(
            spotify_section["to-listen-playlist"], "to-listen-playlist", "spotify"
        ),
        "test_playlist": _assert_str(
            spotify_section["test-playlist"], "test-playlist", "spotify"
        ),
        "followed_labels_playlist": _assert_str(
            spotify_section["followed-labels-playlist"],
            "followed-labels-playlist",
            "spotify",
        ),
        "to_download_playlist": _assert_str(
            spotify_section["to-download-playlist"], "to-download-playlist", "spotify"
        ),
        "acapella_playlist": _assert_str(
            spotify_section["acapella-playlist"], "acapella-playlist", "spotify"
        ),
        "scopes": _validate_list_of_strings(
            spotify_section["scopes"], "scopes", "spotify"
        ),
    }

    collection_section = raw.get("collection", {})
    if not isinstance(collection_section, dict):
        raise ValueError("Expected [collection] section to be a table")

    collection_cfg: CollectionConfig = {
        "music_dirs": _validate_list_of_strings(
            collection_section.get("music-dirs", []),
            "music-dirs",
            "collection",
        )
    }

    discovery_section = raw.get("discovery", {})
    if not isinstance(discovery_section, dict):
        raise ValueError("Expected [discovery] section to be a table")
    freshness_days = discovery_section.get("freshness-days", 90)
    if not isinstance(freshness_days, int) or isinstance(freshness_days, bool):
        raise ValueError("Expected [discovery].freshness-days to be an integer")
    if freshness_days < 1:
        raise ValueError("Expected [discovery].freshness-days to be positive")
    raw_aliases = discovery_section.get("label-aliases", {})
    if not isinstance(raw_aliases, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in raw_aliases.items()
    ):
        raise ValueError(
            "Expected [discovery].label-aliases to be a string-to-string table"
        )
    discovery_cfg: DiscoveryConfig = {
        "freshness_days": freshness_days,
        "label_aliases": cast(Dict[str, str], raw_aliases),
    }

    return {
        "spotify": spotify_cfg,
        "collection": collection_cfg,
        "discovery": discovery_cfg,
    }


@lru_cache(maxsize=1)
def get_settings(config_path: str = "config.toml") -> AppConfig:
    """Load and cache application settings; reuses config across calls."""
    return load_config(config_path)
