from crate_digger.utils.telegram import construct_message


def test_construct_message():
    expected_message =  \
r"""❗*NEW RELEASES*❗

🎵 FOUND *4* TRACKS 🎵


🎤 GOOD LABEL

💿 Nice Single
__1\. Someone, Someone Else \- Song__
2\. Someone, Someone Else \- Song \- Extended Mix


🎤 COOL LABEL

💿 Warm EP
__1\. Somebody \- Warm__
__2\. Somebody, DJ Person \- Warm \- DJ Person Remix__
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
