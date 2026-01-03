import textwrap

import pytest

from crate_digger.utils.config import load_config


def test_load_config_valid(tmp_path):
    config_text = textwrap.dedent(
        """
        [spotify]
        to-listen-playlist = "pl:1"
        test-playlist = "pl:test"
        scopes = ["a", "b"]

        [labels]
        names = ["Label"]
        """
    )
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(config_text)

    cfg = load_config(cfg_file)

    assert cfg["spotify"]["to_listen_playlist"] == "pl:1"
    assert cfg["spotify"]["test_playlist"] == "pl:test"
    assert cfg["spotify"]["scopes"] == ["a", "b"]
    assert cfg["labels"]["names"] == ["Label"]


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
        scopes = "not-a-list"

        [labels]
        names = ["ok", 3]
        """
    )
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(config_text)

    with pytest.raises(ValueError):
        load_config(cfg_file)
