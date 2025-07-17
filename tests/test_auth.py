from crate_digger.utils.config import load_config

from pathlib import Path


def test_cache_present_for_each_scope():
    config_scopes = load_config()["spotify"]["scopes"]

    for scope in config_scopes:
        assert Path(f".spotipy_cache/.cache-{scope}").exists()
