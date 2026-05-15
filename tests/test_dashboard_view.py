import sqlite3
from pathlib import Path

from crate_digger.collection.index import _ensure_schema
from crate_digger.web.app import _build_collection_view, _search_spotify_candidates


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
            artwork_checked,
            size,
            mtime_ns,
            indexed_at
        )
            values (?, ?, ?, ?, ?, ?, ?, ?, 1, 1, 1, '2026-01-01T00:00:00+00:00')
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
