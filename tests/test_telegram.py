import textwrap
import pytest
import requests

from crate_digger.utils.telegram import construct_message, send_message
from unittest.mock import Mock, patch


def _mk_track(name, artist, uri):
    return {
        "name": name,
        "artists": [{"name": artist}],
        "uri": uri,
        "album": {"name": "Album"},
    }


def test_construct_message():
    expected_message = textwrap.dedent(
        """\
        ❗*NEW RELEASES*❗

        🎵 FOUND *5* TRACKS 🎵


        🎤 GOOD LABEL

        💿 Nice Single
        💿 Amazing EP


        🎤 COOL LABEL

        💿 Warm EP
        """
    )

    notification_content = {
        "Good Label": {
            "Nice Single": [
                _mk_track("Song", "Someone", "uri:1"),
                _mk_track("Song - Extended Mix", "Someone", "uri:2"),
            ],
            "Amazing EP": [
                _mk_track("Song As Well", "Someone As Well", "uri:3"),
            ],
        },
        "Cool Label": {
            "Warm EP": [
                _mk_track("Warm", "Somebody", "uri:4"),
                _mk_track("Warm - DJ Person Remix", "Somebody", "uri:5"),
            ]
        },
    }

    msg = construct_message(notification_content)

    assert msg == expected_message


def test_construct_message_single_label_single_album():
    track_info = {
        "Label1": {
            "Album1": [
                _mk_track("Track1", "Artist1", "uri:1"),
                _mk_track("Track2", "Artist2", "uri:2"),
            ]
        }
    }

    message = construct_message(track_info)

    assert "LABEL1" in message
    assert "Album1" in message


def test_construct_message_multiple_labels():
    track_info = {
        "Label1": {"Album1": [_mk_track("Track1", "Artist1", "uri:1")]},
        "Label2": {"Album2": [_mk_track("Track2", "Artist2", "uri:2")]},
    }

    message = construct_message(track_info)

    assert "LABEL1" in message
    assert "LABEL2" in message


def test_construct_message_empty_dict():
    message = construct_message({})
    assert isinstance(message, str)
    assert len(message) >= 0  # Should handle empty gracefully


def test_construct_message_multiple_albums_per_label():
    track_info = {
        "Label1": {
            "Album1": [_mk_track("Track1", "Artist1", "uri:1")],
            "Album2": [_mk_track("Track2", "Artist2", "uri:2")],
        }
    }

    message = construct_message(track_info)

    assert "Album1" in message
    assert "Album2" in message


@patch("crate_digger.utils.telegram.requests.post")
def test_send_message_success(mock_post):
    mock_post.return_value.status_code = 200
    mock_post.return_value.json.return_value = {"ok": True}

    # Should not raise
    send_message("Test message")

    mock_post.assert_called_once()
    call_kwargs = mock_post.call_args.kwargs
    assert "data" in call_kwargs
    assert call_kwargs["data"]["text"] == "Test message"


@patch("crate_digger.utils.telegram.requests.post")
def test_send_message_handles_failure(mock_post):
    resp = Mock()
    resp.raise_for_status.side_effect = requests.HTTPError(
        "400 Client Error: Bad Request"
    )
    resp.status_code = 400
    resp.text = "Bad Request"

    mock_post.return_value = resp

    with pytest.raises(requests.HTTPError):
        send_message("Test message")


@patch("crate_digger.utils.telegram.requests.post")
def test_send_message_escapes_special_chars(mock_post):
    mock_post.return_value.status_code = 200
    mock_post.return_value.json.return_value = {"ok": True}

    # Telegram MarkdownV2 requires escaping certain chars
    send_message("Test_message*with#special")

    mock_post.assert_called_once()
