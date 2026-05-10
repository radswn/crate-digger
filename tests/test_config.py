import textwrap

import pytest

from crate_digger.utils.config import load_config, get_settings


def test_load_config_valid(tmp_path):
    config_text = textwrap.dedent(
        """
        [spotify]
        to-listen-playlist = "pl:1"
        test-playlist = "pl:test"
        followed-labels-playlist = "pl:labels"
        to-download-playlist = "pl:download"
        acapella-playlist = "pl:acapella"
        scopes = ["a", "b"]
        """
    )
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(config_text)

    cfg = load_config(cfg_file)

    assert cfg["spotify"]["to_listen_playlist"] == "pl:1"
    assert cfg["spotify"]["test_playlist"] == "pl:test"
    assert cfg["spotify"]["followed_labels_playlist"] == "pl:labels"
    assert cfg["spotify"]["to_download_playlist"] == "pl:download"
    assert cfg["spotify"]["acapella_playlist"] == "pl:acapella"
    assert cfg["spotify"]["scopes"] == ["a", "b"]


def test_load_config_requires_sections(tmp_path):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text("[spotify]\nscopes=['x']\n")

    with pytest.raises(ValueError):
        load_config(cfg_file)


def test_load_config_requires_strings_and_lists(tmp_path):
    config_text = textwrap.dedent(
        """
        [spotify]
        to-listen-playlist = 123
        test-playlist = "pl"
        followed-labels-playlist = "pl:labels"
        to-download-playlist = "pl:download"
        acapella-playlist = "pl:acapella"
        scopes = "not-a-list"
        """
    )
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(config_text)

    with pytest.raises(ValueError):
        load_config(cfg_file)


def test_get_settings_caches_config(tmp_path):
    config_text = textwrap.dedent(
        """
        [spotify]
        to-listen-playlist = "pl:1"
        test-playlist = "pl:test"
        followed-labels-playlist = "pl:labels"
        to-download-playlist = "pl:download"
        acapella-playlist = "pl:acapella"
        scopes = ["a"]
        """
    )
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(config_text)

    # Clear cache before test
    get_settings.cache_clear()

    cfg1 = get_settings(str(cfg_file))
    cfg2 = get_settings(str(cfg_file))

    # Should return same object (cached)
    assert cfg1 is cfg2


def test_load_config_file_not_found():
    with pytest.raises(FileNotFoundError):
        load_config("/nonexistent/path/config.toml")
