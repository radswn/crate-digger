from crate_digger.utils.config import load_config


def test_load_config():
    cfg = load_config()

    assert "spotify" in cfg
    assert "labels" in cfg

    assert "to-listen-playlist" in cfg["spotify"]
    assert "test-playlist" in cfg["spotify"]
    assert "scopes" in cfg["spotify"]

    assert "names" in cfg["labels"]
