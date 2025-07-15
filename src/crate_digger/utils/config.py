import tomllib

from typing import Dict


def load_config(config_path="config.toml") -> Dict:
    with open(config_path, "rb") as f:
        config = tomllib.load(f)
        return config
