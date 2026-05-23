import subprocess

from crate_digger.collection.normalization import measure_track_loudness


def test_measure_track_loudness_parses_ffmpeg_loudnorm_json(tmp_path, monkeypatch):
    track = tmp_path / "track.mp3"
    track.write_bytes(b"not real audio")

    def fake_which(name):
        return "/usr/bin/ffmpeg" if name == "ffmpeg" else None

    def fake_run(command, *, capture_output, check, text, timeout):
        assert command[0] == "/usr/bin/ffmpeg"
        assert str(track) in command
        assert capture_output is True
        assert check is False
        assert text is True
        assert timeout == 240
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="",
            stderr="""
            [Parsed_loudnorm_0]
            {
                "input_i" : "-18.50",
                "input_tp" : "-1.00"
            }
            """,
        )

    monkeypatch.setattr(
        "crate_digger.collection.normalization.shutil.which", fake_which
    )
    monkeypatch.setattr(
        "crate_digger.collection.normalization.subprocess.run", fake_run
    )

    measurement = measure_track_loudness(track, target_lufs=-14.0)

    assert measurement is not None
    assert measurement.gain_db == 4.5
    assert round(measurement.peak or 0, 6) == 0.891251


def test_measure_track_loudness_returns_none_without_ffmpeg(tmp_path, monkeypatch):
    track = tmp_path / "track.mp3"
    track.write_bytes(b"not real audio")
    monkeypatch.setattr(
        "crate_digger.collection.normalization.shutil.which",
        lambda _name: None,
    )

    assert measure_track_loudness(track) is None
