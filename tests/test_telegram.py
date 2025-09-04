from crate_digger.utils.telegram import construct_message


def test_construct_message():
    expected_message =  \
r"""❗*NEW RELEASES*❗

🎵 FOUND *5* TRACKS 🎵


🎤 GOOD LABEL

💿 Nice Single
💿 Amazing EP


🎤 COOL LABEL

💿 Warm EP
"""

    notification_content = {
        "Good Label": {
            "Nice Single": [
                {
                    "artists": [
                        {"name": "Someone"},
                        {"name": "Someone Else"}
                    ],
                    "name": "Song",
                    "is_added": True
                },
                {
                    "artists": [
                        {"name": "Someone"},
                        {"name": "Someone Else"}
                    ],
                    "name": "Song - Extended Mix",
                    "is_added": False
                }
            ],
            "Amazing EP": [
                {
                    "artists": [
                        {"name": "Someone As Well"}
                    ],
                    "name": "Song As Well",
                    "is_added": True
                }
            ]
        },
        "Cool Label": {
            "Warm EP": [
                {
                    "artists": [
                        {"name": "Somebody"},
                    ],
                    "name": "Warm",
                    "is_added": True
                },
                {
                    "artists": [
                        {"name": "Somebody"},
                        {"name": "DJ Person"},
                    ],
                    "name": "Warm - DJ Person Remix",
                    "is_added": True
                }
            ]
        }
    }

    msg = construct_message(notification_content)

    assert msg == expected_message
