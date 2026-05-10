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


class AppConfig(TypedDict):
    spotify: SpotifyConfig


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

    return {"spotify": spotify_cfg}


@lru_cache(maxsize=1)
def get_settings(config_path: str = "config.toml") -> AppConfig:
    """Load and cache application settings; reuses config across calls."""
    return load_config(config_path)
