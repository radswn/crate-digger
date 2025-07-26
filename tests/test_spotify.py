from crate_digger.utils.spotify import (
    get_spotify_client,
    filter_relevant_releases,
    get_album_tracks,
    get_uris,
    remove_extended_versions
)


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
    berk = {
        "name": "Berk",
        "uri": "spotify:album:6HuxRAq6IqA1iBHNt1MsLD"
    }
    grasses_uri = "spotify:track:7HODJrjN4MkIRWdrTlqjiM"

    sp = get_spotify_client("user-library-read")
    album_tracks = get_album_tracks(sp, berk)
    track_uris = get_uris(album_tracks)

    assert len(track_uris) == 10
    assert grasses_uri in set(track_uris)


def test_remove_extended_versions():
    test_tracks = [
        {"name": "Song"},
        {"name": "Song - Radio Mix"},
        {"name": "Song - Nice Remix"},
        {"name": "Song Two - Extended Mix"},
        
        {"name": "Song - Extended Mix"},
        {"name": "Song (Extended Mix)"},
        {"name": "Song - Nice Extended Remix"},
    ]

    filtered_tracks = set([t["name"] for t in remove_extended_versions(test_tracks)])

    assert len(filtered_tracks) == 4
    assert "Song" in filtered_tracks
    assert "Song - Radio Mix" in filtered_tracks
    assert "Song - Nice Remix" in filtered_tracks
    assert "Song Two - Extended Mix" in filtered_tracks
