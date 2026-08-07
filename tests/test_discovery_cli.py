import textwrap

from crate_digger.cli import main
from crate_digger.discover.models import SpotifyEntityTrack
from crate_digger.discover.repository import upsert_spotify_tracks


def test_discover_cli_rebuild_build_show_explain_feedback_and_stats(tmp_path, capsys):
    db_path = tmp_path / "collection.sqlite3"
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        textwrap.dedent(
            """
            [spotify]
            to-listen-playlist = "listen"
            test-playlist = "test"
            followed-labels-playlist = "labels"
            to-download-playlist = "download"
            acapella-playlist = "acapella"
            scopes = ["playlist-read-private"]

            [discovery]
            freshness-days = 90
            """
        ),
        encoding="utf-8",
    )
    upsert_spotify_tracks(
        db_path,
        [
            SpotifyEntityTrack(
                spotify_track_id="cli-track",
                spotify_uri="spotify:track:cli-track",
                title="CLI Track",
                artists=(("cli-artist", "CLI Artist"),),
                spotify_release_id="cli-release",
                release_title="CLI Release",
                release_date="2020-01-01",
                raw_label_name="CLI Label",
            )
        ],
        label_aliases={},
        source="manual",
    )

    assert (
        main(
            [
                "discover",
                "rebuild-taste",
                "--offline",
                "--db-path",
                str(db_path),
                "--config",
                str(config_path),
            ]
        )
        == 0
    )
    assert "Neutral Catalogue Tracks: 1" in capsys.readouterr().out
    assert (
        main(
            [
                "discover",
                "build",
                "--mode",
                "deep-dig",
                "--size",
                "1",
                "--db-path",
                str(db_path),
                "--config",
                str(config_path),
            ]
        )
        == 0
    )
    build_output = capsys.readouterr().out
    assert "Session ID: 1" in build_output
    assert "Actual size: 1" in build_output

    assert main(["discover", "show", "1", "--db-path", str(db_path)]) == 0
    show_output = capsys.readouterr().out
    assert "CLI Artist — CLI Track" in show_output
    item_id = int(show_output.splitlines()[1].split()[0])
    assert (
        main(
            [
                "discover",
                "explain",
                "1",
                str(item_id),
                "--db-path",
                str(db_path),
            ]
        )
        == 0
    )
    assert "Score at selection" in capsys.readouterr().out
    assert (
        main(
            [
                "discover",
                "feedback",
                "1",
                str(item_id),
                "keep",
                "--db-path",
                str(db_path),
            ]
        )
        == 0
    )
    assert "Recorded keep" in capsys.readouterr().out
    assert main(["discover", "stats", "--db-path", str(db_path)]) == 0
    assert "Kept: 1" in capsys.readouterr().out
