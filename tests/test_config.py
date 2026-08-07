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

        [collection]
        music-dirs = ["/music", "~/Downloads/tracks"]
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
    assert cfg["collection"]["music_dirs"] == ["/music", "~/Downloads/tracks"]
    assert cfg["discovery"]["freshness_days"] == 90
    assert cfg["discovery"]["label_aliases"] == {}


def test_load_config_defaults_collection(tmp_path):
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

    assert cfg["collection"]["music_dirs"] == []


def test_load_config_reads_discovery_settings(tmp_path):
    config_text = textwrap.dedent(
        """
        [spotify]
        to-listen-playlist = "pl:1"
        test-playlist = "pl:test"
        followed-labels-playlist = "pl:labels"
        to-download-playlist = "pl:download"
        acapella-playlist = "pl:acapella"
        scopes = ["a"]

        [discovery]
        freshness-days = 60

        [discovery.label-aliases]
        "Hot-Creations" = "Hot Creations"
        "ISSUES RECORDS" = "Issues"
        """
    )
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(config_text)

    cfg = load_config(cfg_file)

    assert cfg["discovery"]["freshness_days"] == 60
    assert cfg["discovery"]["label_aliases"]["ISSUES RECORDS"] == "Issues"


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
