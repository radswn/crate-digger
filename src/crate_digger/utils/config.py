from __future__ import annotations

import tomllib
from functools import lru_cache

from typing import Dict, List, TypedDict


class SpotifyConfig(TypedDict):
    """Expected structure of the `[spotify]` section."""

    to_listen_playlist: str
    test_playlist: str
    scopes: List[str]


class LabelsConfig(TypedDict):
    """Expected structure of the `[labels]` section."""

    names: List[str]


class AppConfig(TypedDict):
    spotify: SpotifyConfig
    labels: LabelsConfig


def _require_keys(section: Dict, required: List[str], section_name: str) -> None:
    missing = [k for k in required if k not in section]
    if missing:
        raise ValueError(f"Missing keys in [{section_name}]: {', '.join(missing)}")


def _assert_str(value: object, key: str, section_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"Expected [{section_name}].{key} to be a string")
    return value


def _validate_list_of_strings(value: object, key: str, section_name: str) -> List[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"Expected [{section_name}].{key} to be a list of strings")
    return value


def load_config(config_path: str = "config.toml") -> AppConfig:
    """Load and validate application configuration from TOML."""

    with open(config_path, "rb") as f:
        raw = tomllib.load(f)

    if "spotify" not in raw or "labels" not in raw:
        raise ValueError("Config must contain [spotify] and [labels] sections")

    spotify_section = raw["spotify"]
    labels_section = raw["labels"]

    _require_keys(spotify_section, ["to-listen-playlist", "test-playlist", "scopes"], "spotify")
    _require_keys(labels_section, ["names"], "labels")

    spotify_cfg: SpotifyConfig = {
        "to_listen_playlist": _assert_str(spotify_section["to-listen-playlist"], "to-listen-playlist", "spotify"),
        "test_playlist": _assert_str(spotify_section["test-playlist"], "test-playlist", "spotify"),
        "scopes": _validate_list_of_strings(spotify_section["scopes"], "scopes", "spotify"),
    }

    labels_cfg: LabelsConfig = {
        "names": _validate_list_of_strings(labels_section["names"], "names", "labels"),
    }

    return {"spotify": spotify_cfg, "labels": labels_cfg}


@lru_cache(maxsize=1)
def get_settings(config_path: str = "config.toml") -> AppConfig:
    """Load and cache application settings; reuses config across calls."""
    return load_config(config_path)
