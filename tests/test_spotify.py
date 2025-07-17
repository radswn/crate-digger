from crate_digger.utils.spotify import get_spotify_client, filter_relevant_releases, get_track_uris_for_album


def test_filter_relevant_releases():
    unfiltered = [
        {"album_type": "ep"},
        {"album_type": "ep"},
        {"album_type": "compilation"},
        {"album_type": "single"},
    ]

    filtered = filter_relevant_releases(unfiltered)

    assert len(filtered) == 3
    assert {"album_type": "compilation"} not in filtered


def test_get_track_uris_for_album():
    berk_uri = "spotify:album:6HuxRAq6IqA1iBHNt1MsLD"
    grasses_uri = "spotify:track:7HODJrjN4MkIRWdrTlqjiM"

    sp = get_spotify_client("user-library-read")
    track_uris = get_track_uris_for_album(sp, berk_uri)

    assert len(track_uris) == 10
    assert grasses_uri in set(track_uris)
