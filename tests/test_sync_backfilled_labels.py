import json

from crate_digger.main import sync_backfilled_labels


def test_sync_backfilled_labels_updates_state_from_matching_playlists(
    tmp_path, monkeypatch, capsys
):
    config_path = tmp_path / "config.toml"
    state_path = tmp_path / "backfilled_labels.json"
    config_path.write_text(
        """
[spotify]
to-listen-playlist = "listen"
test-playlist = "test"
followed-labels-playlist = "labels"
to-download-playlist = "download"
acapella-playlist = "acapella"
scopes = ["playlist-read-private"]

[collection]
music-dirs = []
""",
        encoding="utf-8",
    )
    state_path.write_text('{"labels": ["Already Done"]}\n', encoding="utf-8")

    monkeypatch.setattr(
        sync_backfilled_labels,
        "get_spotify_client",
        lambda scope: object(),
    )
    monkeypatch.setattr(
        sync_backfilled_labels,
        "fetch_followed_labels_from_playlist",
        lambda client, playlist: ["Already Done", "Cecille Records", "COCO"],
    )
    monkeypatch.setattr(
        sync_backfilled_labels,
        "fetch_user_playlist_names",
        lambda client: [
            "Already Done 001",
            "Cecille Records1",
            "Coconut Cuts",
        ],
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "sync_backfilled_labels",
            "--config",
            str(config_path),
            "--state-path",
            str(state_path),
        ],
    )

    sync_backfilled_labels.main()

    assert json.loads(state_path.read_text(encoding="utf-8")) == {
        "labels": ["Already Done", "Cecille Records"]
    }
    output = capsys.readouterr().out
    assert "Cecille Records (new)" in output
    assert "COCO" not in output


def test_sync_backfilled_labels_dry_run_does_not_write(tmp_path, monkeypatch, capsys):
    config_path = tmp_path / "config.toml"
    state_path = tmp_path / "backfilled_labels.json"
    config_path.write_text(
        """
[spotify]
to-listen-playlist = "listen"
test-playlist = "test"
followed-labels-playlist = "labels"
to-download-playlist = "download"
acapella-playlist = "acapella"
scopes = ["playlist-read-private"]

[collection]
music-dirs = []
""",
        encoding="utf-8",
    )
    state_path.write_text('{"labels": []}\n', encoding="utf-8")

    monkeypatch.setattr(
        sync_backfilled_labels, "get_spotify_client", lambda scope: object()
    )
    monkeypatch.setattr(
        sync_backfilled_labels,
        "fetch_followed_labels_from_playlist",
        lambda client, playlist: ["Cecille Records"],
    )
    monkeypatch.setattr(
        sync_backfilled_labels,
        "fetch_user_playlist_names",
        lambda client: ["Cecille Records 001"],
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "sync_backfilled_labels",
            "--config",
            str(config_path),
            "--state-path",
            str(state_path),
            "--dry-run",
        ],
    )

    sync_backfilled_labels.main()

    assert json.loads(state_path.read_text(encoding="utf-8")) == {"labels": []}
    assert "Dry run: would add 1 labels" in capsys.readouterr().out
