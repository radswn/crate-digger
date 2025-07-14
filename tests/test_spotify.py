from pathlib import Path
from crate_digger.utils.spotify import get_spotify_client, filter_relevant_releases



def test_spotify_client_can_access_profile():
    root_path = Path(".")
    cached_token_path = root_path / ".cache"

    assert cached_token_path.exists()

    sp = get_spotify_client("playlist-modify-private")
    me = sp.current_user()

    assert "id" in me
    assert "display_name" in me


def test_release_filtering():
    unfiltered = [
        {"album_type": "ep"},
        {"album_type": "ep"},
        {"album_type": "compilation"},
        {"album_type": "single"},
    ]

    filtered = filter_relevant_releases(unfiltered)

    assert len(filtered) == 3
    assert {"album_type": "compilation"} not in filtered
