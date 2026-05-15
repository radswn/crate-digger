import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from crate_digger.collection.index import _ensure_schema
from crate_digger.collection.models import LocalTrack
from crate_digger.web.app import (
    SpotifyCandidate,
    _build_collection_view,
    create_app,
    _download_spotify_artwork,
    _render_spotify_candidate,
    _render_cover,
    _render_spotify_action,
    _safe_return_to,
    _search_spotify_candidates,
    _with_art_refresh,
)


def seed_track(
    db_path: Path,
    filename: str,
    *,
    title: str | None,
    artist: str | None,
    album: str | None,
    bitrate: int | None,
    audio_format: str,
    duration_seconds: float | None = None,
    spotify_uri: str | None = None,
    spotify_link_skipped_at: str | None = None,
) -> None:
    path = Path("/music") / filename
    with sqlite3.connect(db_path) as conn:
        _ensure_schema(conn)
        conn.execute(
            """
            insert into tracks (
                path,
                stem,
                title,
                artist,
                album,
                duration_seconds,
                bitrate,
                audio_format,
                spotify_uri,
                spotify_link_skipped_at,
                artwork_checked,
                size,
                mtime_ns,
                indexed_at
            )
            values (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1, 1,
                '2026-01-01T00:00:00+00:00'
            )
            """,
            (
                str(path),
                path.stem,
                title,
                artist,
                album,
                duration_seconds,
                bitrate,
                audio_format,
                spotify_uri,
                spotify_link_skipped_at,
            ),
        )


def test_collection_view_searches_across_track_fields(tmp_path):
    db_path = tmp_path / "collection.sqlite3"
    seed_track(
        db_path,
        "deep.flac",
        title="Deep Burn",
        artist="Ada",
        album="Night Work",
        bitrate=900000,
        audio_format="FLAC",
    )
    seed_track(
        db_path,
        "bright.mp3",
        title="Bright Cut",
        artist="Bea",
        album="Day Work",
        bitrate=320000,
        audio_format="MP3",
    )

    view = _build_collection_view(
        db_path,
        q="night",
        audio_format="",
        metadata="all",
        spotify="all",
        sort="title",
        direction="asc",
        page=1,
        page_size=50,
    )

    assert view.filtered_count == 1
    assert view.tracks[0].title == "Deep Burn"


def test_collection_view_filters_by_format_and_missing_metadata(tmp_path):
    db_path = tmp_path / "collection.sqlite3"
    seed_track(
        db_path,
        "tagged.flac",
        title="Tagged",
        artist="Ada",
        album="Album",
        bitrate=900000,
        audio_format="FLAC",
    )
    seed_track(
        db_path,
        "missing.flac",
        title=None,
        artist="Bea",
        album="Album",
        bitrate=850000,
        audio_format="FLAC",
    )
    seed_track(
        db_path,
        "other.mp3",
        title=None,
        artist=None,
        album=None,
        bitrate=320000,
        audio_format="MP3",
    )

    view = _build_collection_view(
        db_path,
        q="",
        audio_format="flac",
        metadata="missing",
        spotify="all",
        sort="title",
        direction="asc",
        page=1,
        page_size=50,
    )

    assert view.filtered_count == 1
    assert view.tracks[0].path.name == "missing.flac"


def test_collection_view_sorts_and_paginates(tmp_path):
    db_path = tmp_path / "collection.sqlite3"
    for bitrate in range(26):
        seed_track(
            db_path,
            f"track-{bitrate}.mp3",
            title=f"Track {bitrate}",
            artist="Ada",
            album="Album",
            bitrate=bitrate,
            audio_format="MP3",
        )

    view = _build_collection_view(
        db_path,
        q="",
        audio_format="",
        metadata="all",
        spotify="all",
        sort="bitrate",
        direction="desc",
        page=2,
        page_size=25,
    )

    assert view.filtered_count == 26
    assert view.total_pages == 3
    assert view.query.page == 2
    assert len(view.tracks) == 10
    assert view.tracks[0].path.name == "track-15.mp3"
    assert view.query.page_size == 10


def test_collection_view_filters_by_spotify_status(tmp_path):
    db_path = tmp_path / "collection.sqlite3"
    seed_track(
        db_path,
        "unlinked.mp3",
        title="Unlinked",
        artist="Ada",
        album="Album",
        bitrate=320000,
        audio_format="MP3",
    )
    seed_track(
        db_path,
        "linked.mp3",
        title="Linked",
        artist="Ada",
        album="Album",
        bitrate=320000,
        audio_format="MP3",
        spotify_uri="spotify:track:linked",
    )
    seed_track(
        db_path,
        "skipped.mp3",
        title="Skipped",
        artist="Ada",
        album="Album",
        bitrate=320000,
        audio_format="MP3",
        spotify_link_skipped_at="2026-01-01T00:00:00+00:00",
    )

    view = _build_collection_view(
        db_path,
        q="",
        audio_format="",
        metadata="all",
        spotify="skipped",
        sort="title",
        direction="asc",
        page=1,
        page_size=50,
    )

    assert view.filtered_count == 1
    assert view.query.spotify == "skipped"
    assert view.tracks[0].path.name == "skipped.mp3"
    assert view.tracks[0].spotify_link_skipped_at == "2026-01-01T00:00:00+00:00"


def test_spotify_link_actions_preserve_return_to():
    track = seedless_track("/music/unlinked.mp3")
    action = _render_spotify_action(
        track,
        return_to="/?q=night&spotify=unlinked&page=2",
    )
    candidate = _render_spotify_candidate(
        track,
        SpotifyCandidate(
            uri="spotify:track:1",
            name="Night Track",
            artists="Ada",
            album="Album",
            image_url="https://i.scdn.co/image/manual-cover",
            external_url=None,
        ),
        "/?q=night&spotify=unlinked&page=2",
    )

    assert "return_to=%2F%3Fq%3Dnight%26spotify%3Dunlinked%26page%3D2" in action
    assert 'action="/spotify-link/quick-link"' in action
    assert ">Link</button>" in action
    assert ">Find</a>" in action
    assert (
        'name="return_to" value="/?q=night&amp;spotify=unlinked&amp;page=2"'
        in candidate
    )
    assert 'name="image_url" value="https://i.scdn.co/image/manual-cover"' in candidate


def test_linked_spotify_action_keeps_manual_find_available():
    track = seedless_track("/music/linked.mp3", spotify_uri="spotify:track:linked")
    action = _render_spotify_action(track, return_to="/?spotify=linked")

    assert "https://open.spotify.com/track/linked" in action
    assert ">Linked</a>" in action
    assert ">Find</a>" in action
    assert 'action="/spotify-link/quick-link"' not in action


def test_safe_return_to_allows_only_local_paths():
    assert _safe_return_to("/?q=night") == "/?q=night"
    assert _safe_return_to("https://example.com/") == "/"
    assert _safe_return_to("//example.com/") == "/"
    assert _safe_return_to("collection") == "/"


def test_art_refresh_url_preserves_current_view():
    assert (
        _with_art_refresh("/?q=night&spotify=linked&page=2")
        == "/?q=night&spotify=linked&page=2&art_refresh=1"
    )
    assert _with_art_refresh("/") == "/?art_refresh=1"


def test_render_cover_cache_busts_with_index_timestamp():
    track = seedless_track("/music/covered.mp3")
    track = LocalTrack(
        path=track.path,
        title=track.title,
        artist=track.artist,
        album=track.album,
        duration_seconds=track.duration_seconds,
        bitrate=track.bitrate,
        audio_format=track.audio_format,
        artwork_mime="image/jpeg",
        indexed_at="2026-05-15T10:00:00+00:00",
    )

    rendered = _render_cover(track)

    assert "path=%2Fmusic%2Fcovered.mp3" in rendered
    assert "v=2026-05-15T10%3A00%3A00%2B00%3A00" in rendered


def test_spotify_link_posts_redirect_to_return_to(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[spotify]
to-listen-playlist = "listen"
test-playlist = "test"
followed-labels-playlist = "labels"
to-download-playlist = "download"
acapella-playlist = "acapella"
scopes = []

[collection]
music-dirs = []
""",
        encoding="utf-8",
    )
    client = TestClient(
        create_app(
            config_path=str(config_path), db_path=tmp_path / "collection.sqlite3"
        )
    )

    response = client.post(
        "/spotify-link/link",
        data={
            "path": "/music/unlinked.mp3",
            "spotify_uri": "spotify:track:1",
            "return_to": "/?q=night&spotify=unlinked&page=2",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/?q=night&spotify=unlinked&page=2"


def test_spotify_link_replaces_art_before_redirect(tmp_path, monkeypatch):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[spotify]
to-listen-playlist = "listen"
test-playlist = "test"
followed-labels-playlist = "labels"
to-download-playlist = "download"
acapella-playlist = "acapella"
scopes = []

[collection]
music-dirs = []
""",
        encoding="utf-8",
    )
    db_path = tmp_path / "collection.sqlite3"
    calls = []

    def fake_replace_track_artwork_from_url(db_path, *, path, image_url):
        calls.append((str(db_path), path, image_url))
        return True

    monkeypatch.setattr(
        "crate_digger.web.app._replace_track_artwork_from_url",
        fake_replace_track_artwork_from_url,
    )
    client = TestClient(create_app(config_path=str(config_path), db_path=db_path))

    response = client.post(
        "/spotify-link/link",
        data={
            "path": "/music/unlinked.mp3",
            "spotify_uri": "spotify:track:1",
            "image_url": "https://i.scdn.co/image/manual-cover",
            "return_to": "/?q=night&spotify=unlinked&page=2",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == (
        "/?q=night&spotify=unlinked&page=2&art_refresh=1"
    )
    assert calls == [
        (
            str(db_path),
            "/music/unlinked.mp3",
            "https://i.scdn.co/image/manual-cover",
        )
    ]


def test_spotify_find_modal_reports_lookup_timeout(tmp_path, monkeypatch):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[spotify]
to-listen-playlist = "listen"
test-playlist = "test"
followed-labels-playlist = "labels"
to-download-playlist = "download"
acapella-playlist = "acapella"
scopes = []

[collection]
music-dirs = []
""",
        encoding="utf-8",
    )
    db_path = tmp_path / "collection.sqlite3"
    seed_track(
        db_path,
        "linked.mp3",
        title="After Coma",
        artist="Omon Breaker",
        album="Standard Deviation 2",
        bitrate=320000,
        audio_format="MP3",
        spotify_uri="spotify:track:linked",
    )

    def fake_search_with_timeout(*_args, **_kwargs):
        raise TimeoutError("Spotify lookup exceeded 12s")

    monkeypatch.setattr(
        "crate_digger.web.app._search_spotify_candidates_from_config_with_timeout",
        fake_search_with_timeout,
    )
    client = TestClient(create_app(config_path=str(config_path), db_path=db_path))

    response = client.get(
        "/spotify-link",
        params={
            "path": "/music/linked.mp3",
            "partial": 1,
            "return_to": "/?spotify=linked",
        },
    )

    assert response.status_code == 200
    assert "Spotify lookup timed out. Try again in a moment." in response.text
    assert "After Coma" in response.text


def test_art_endpoint_disables_browser_cache(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[spotify]
to-listen-playlist = "listen"
test-playlist = "test"
followed-labels-playlist = "labels"
to-download-playlist = "download"
acapella-playlist = "acapella"
scopes = []

[collection]
music-dirs = []
""",
        encoding="utf-8",
    )
    db_path = tmp_path / "collection.sqlite3"
    with sqlite3.connect(db_path) as conn:
        _ensure_schema(conn)
        conn.execute(
            """
            insert into tracks (
                path,
                stem,
                title,
                audio_format,
                artwork_mime,
                artwork_data,
                artwork_checked,
                size,
                mtime_ns,
                indexed_at
            )
            values (?, 'covered', 'Covered', 'MP3', 'image/jpeg', ?, 1, 1, 1, '2026-01-01T00:00:00+00:00')
            """,
            ("/music/covered.mp3", b"image-bytes"),
        )
    client = TestClient(create_app(config_path=str(config_path), db_path=db_path))

    response = client.get("/art", params={"path": "/music/covered.mp3"})

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store, max-age=0"


def test_quick_link_assigns_first_spotify_result(tmp_path, monkeypatch):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[spotify]
to-listen-playlist = "listen"
test-playlist = "test"
followed-labels-playlist = "labels"
to-download-playlist = "download"
acapella-playlist = "acapella"
scopes = []

[collection]
music-dirs = []
""",
        encoding="utf-8",
    )
    db_path = tmp_path / "collection.sqlite3"
    seed_track(
        db_path,
        "unlinked.mp3",
        title="After Coma",
        artist="Omon Breaker",
        album="Standard Deviation 2",
        bitrate=900000,
        audio_format="FLAC",
    )

    class Client:
        def search(self, *, q, type, limit, offset):
            assert q == "Omon Breaker - After Coma"
            assert type == "track"
            assert limit == 1
            assert offset == 0
            return {
                "tracks": {
                    "items": [
                        {
                            "uri": "spotify:track:first",
                            "name": "After Coma",
                            "artists": [{"name": "Omon Breaker"}],
                            "album": {
                                "name": "Standard Deviation 2",
                                "images": [
                                    {"url": "https://i.scdn.co/image/first-cover"}
                                ],
                            },
                        }
                    ]
                }
            }

    def fake_get_spotify_client(_scopes):
        return Client()

    monkeypatch.setattr(
        "crate_digger.web.app.get_spotify_client",
        fake_get_spotify_client,
    )
    artwork_calls = []

    def fake_download_spotify_artwork(url):
        artwork_calls.append(("download", url))
        return ("image/jpeg", b"cover")

    def fake_overwrite_embedded_artwork(path, *, mime, data):
        artwork_calls.append(("overwrite", str(path), mime, data))
        return True

    def fake_refresh_track_metadata(db_path, *, path):
        artwork_calls.append(("refresh", str(db_path), path))
        return True

    monkeypatch.setattr(
        "crate_digger.web.app._download_spotify_artwork",
        fake_download_spotify_artwork,
    )
    monkeypatch.setattr(
        "crate_digger.web.app.overwrite_embedded_artwork",
        fake_overwrite_embedded_artwork,
    )
    monkeypatch.setattr(
        "crate_digger.web.app.refresh_track_metadata",
        fake_refresh_track_metadata,
    )

    client = TestClient(create_app(config_path=str(config_path), db_path=db_path))
    response = client.post(
        "/spotify-link/quick-link",
        data={
            "path": "/music/unlinked.mp3",
            "return_to": "/?spotify=unlinked&page=2",
        },
        follow_redirects=False,
    )

    with sqlite3.connect(db_path) as conn:
        spotify_uri = conn.execute(
            "select spotify_uri from tracks where path = ?",
            ("/music/unlinked.mp3",),
        ).fetchone()[0]

    assert response.status_code == 303
    assert response.headers["location"] == "/?spotify=unlinked&page=2&art_refresh=1"
    assert spotify_uri == "spotify:track:first"
    assert artwork_calls == [
        ("download", "https://i.scdn.co/image/first-cover"),
        ("overwrite", "/music/unlinked.mp3", "image/jpeg", b"cover"),
        ("refresh", str(db_path), "/music/unlinked.mp3"),
    ]


def test_download_spotify_artwork_rejects_non_image_url():
    assert _download_spotify_artwork("file:///tmp/cover.jpg") is None


def test_search_spotify_candidates_formats_results():
    class Client:
        def search(self, *, q, type, limit, offset):
            assert q == "Ada - Deep Burn"
            assert type == "track"
            assert limit == 5
            assert offset == 10
            return {
                "tracks": {
                    "items": [
                        {
                            "uri": "spotify:track:1",
                            "name": "Deep Burn",
                            "artists": [{"name": "Ada"}],
                            "album": {
                                "name": "Night Work",
                                "images": [{"url": "https://i.scdn.co/image/cover"}],
                            },
                            "external_urls": {
                                "spotify": "https://open.spotify.com/track/1"
                            },
                        }
                    ]
                }
            }

    candidates = _search_spotify_candidates(
        Client(),
        "Ada - Deep Burn",
        offset=10,
        limit=5,
    )

    assert len(candidates) == 1
    assert candidates[0].uri == "spotify:track:1"
    assert candidates[0].artists == "Ada"
    assert candidates[0].album == "Night Work"
    assert candidates[0].image_url == "https://i.scdn.co/image/cover"


def seedless_track(path: str, *, spotify_uri: str | None = None) -> LocalTrack:
    return LocalTrack(
        path=Path(path),
        title="Night Track",
        artist="Ada",
        album="Album",
        duration_seconds=None,
        bitrate=None,
        audio_format="MP3",
        spotify_uri=spotify_uri,
    )
