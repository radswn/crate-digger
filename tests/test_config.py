from crate_digger.utils.config import load_config


def test_load_config():
    cfg = load_config()

    assert "playlists" in cfg
    assert "labels" in cfg

    assert "to-listen" in cfg["playlists"]
    assert "names" in cfg["labels"]
