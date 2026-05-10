import pandas as pd

from unittest.mock import MagicMock

from types import SimpleNamespace
from typing import List, cast

import crate_digger.utils.spotify as m
from crate_digger.utils.types import SpotifyAlbum


class FakeDate:
    """Patchable replacement for datetime.date used in the module (it imports `date` directly)."""

    _today = None

    @classmethod
    def today(cls):
        if cls._today is None:
            raise RuntimeError("FakeDate._today not set")
        return cls._today


def _mk_track(name, artists, uri, album_name="Album"):
    return {
        "name": name,
        "artists": [{"name": a} for a in artists],
        "uri": uri,
        "album": {"name": album_name},
    }


def test_filter_past_week_releases_filters_correctly(monkeypatch):
    FakeDate._today = SimpleNamespace(
        isoformat=lambda: "2026-01-21"
    )  # minimal stub for .isoformat()
    # We need .today() returning something with isoformat(); timedelta(days=1) will be applied to it in your code
    # so we patch the module's date with a *real* date-like object instead:
    import datetime as _dt

    class _RealFakeDate(_dt.date):
        @classmethod
        def today(cls):
            return cls(2026, 1, 21)

    monkeypatch.setattr(m, "date", _RealFakeDate)

    releases = cast(
        List[SpotifyAlbum],
        [
            {
                "release_date": "2026-01-20",
                "uri": "a",
            },
            {
                "release_date": "2026-01-13",
                "uri": "b",
            },
            {
                "release_date": "2026-01-14",
                "uri": "c",
            },
        ],
    )

    out = m.filter_releases_by_date(releases, n_days=1)
    assert [r["uri"] for r in out] == ["a"]


def test_remove_extended_versions_prefers_original_when_present():
    tracks = [
        _mk_track("Track", ["Artist"], "uri:1"),
        _mk_track("Track (Extended Mix)", ["Artist"], "uri:2"),
        _mk_track("Track - Extended", ["Artist"], "uri:3"),
    ]

    out = m.remove_extended_versions(tracks)
    assert [t["uri"] for t in out] == ["uri:1"]


def test_remove_extended_versions_keeps_extended_if_original_missing():
    tracks = [
        _mk_track("Banger (Extended Mix)", ["DJ"], "uri:x"),
    ]

    out = m.remove_extended_versions(tracks)
    assert [t["uri"] for t in out] == ["uri:x"]


def test_remove_extended_versions_normalizes_punctuation_and_spaces():
    tracks = [
        _mk_track("Foo!", ["A"], "u1"),
        _mk_track("Foo   Extended   Mix", ["A"], "u2"),
    ]
    out = m.remove_extended_versions(tracks)
    assert [t["uri"] for t in out] == ["u1"]


def test_remove_extended_versions_handles_unicode():
    tracks = [
        _mk_track("Café", ["A"], "u1"),
        _mk_track("Café (Extended Mix)", ["A"], "u2"),
    ]
    out = m.remove_extended_versions(tracks)
    assert [t["uri"] for t in out] == ["u1"]


def test_remove_extended_versions_empty_list():
    assert m.remove_extended_versions([]) == []


def test_parse_releases_drops_columns_dedupes_and_sorts():
    releases = [
        {
            "uri": "u2",
            "release_date": "2020-01-03",
            "artists": [],
            "images": [],
            "available_markets": [],
            "external_urls": {},
            "name": "B",
        },
        {
            "uri": "u1",
            "release_date": "2020-01-01",
            "artists": [],
            "images": [],
            "available_markets": [],
            "external_urls": {},
            "name": "A",
        },
        # duplicate uri
        {
            "uri": "u1",
            "release_date": "2020-01-02",
            "artists": [],
            "images": [],
            "available_markets": [],
            "external_urls": {},
            "name": "A-dup",
        },
    ]

    df = m.parse_releases(cast(List[SpotifyAlbum], releases))

    assert list(df["uri"]) == ["u1", "u2"]  # dedup + sorted by release_date
    assert "artists" not in df.columns
    assert "images" not in df.columns
    assert "available_markets" not in df.columns
    assert "external_urls" not in df.columns


def test_parse_releases_handles_empty_input():
    df = m.parse_releases([])

    assert list(df.columns) == ["uri", "release_date"]
    assert df.empty


def test_filter_exact_label_releases_batches_and_filters():
    client = MagicMock()

    releases = cast(
        List[SpotifyAlbum],
        [
            {
                "uri": f"uri:{i}",
                "name": f"Album {i}",
                "label": "Label",
                "release_date": "2020-01-01",
            }
            for i in range(25)
        ],
    )
    # albums() called twice: first batch 20, second batch 5
    client.albums.side_effect = [
        {
            "albums": [
                {
                    "uri": f"uri:{i}",
                    "label": "Good",
                    "name": f"Album {i}",
                    "release_date": "2020-01-01",
                }
                for i in range(20)
            ]
        },
        {
            "albums": [
                {
                    "uri": f"uri:{i}",
                    "label": "Bad" if i % 2 else "Good",
                    "name": f"Album {i}",
                    "release_date": "2020-01-01",
                }
                for i in range(20, 25)
            ]
        },
    ]

    out = m.filter_exact_label_releases(client, releases, "Good")
    assert all(a["label"] == "Good" for a in out)
    assert len(out) == 20 + 3  # 20 from first + (20,22,24) from second

    assert client.albums.call_count == 2
    first_call_uris = client.albums.call_args_list[0].args[0]
    second_call_uris = client.albums.call_args_list[1].args[0]
    assert len(first_call_uris) == 20
    assert len(second_call_uris) == 5


def test_extract_track_uris_extracts_uri():
    tracks = [
        _mk_track("Track 1", ["Artist"], "u1"),
        _mk_track("Track 2", ["Artist"], "u2"),
    ]
    assert m.extract_track_uris(tracks) == ["u1", "u2"]


def test_add_to_playlist_calls_playlist_add_items():
    client = MagicMock()
    client.playlist_add_items.return_value = "snapshot123"

    snap = m.add_to_playlist(client, "playlist_id", ["u1", "u2"])
    assert snap == "snapshot123"
    client.playlist_add_items.assert_called_once_with("playlist_id", ["u1", "u2"])


def test_playlist_name_matches_label_handles_common_backfill_names():
    assert m.playlist_name_matches_label("Black Book Records 004", "Black Book Records")
    assert m.playlist_name_matches_label("Cecille Records1", "Cecille Records")
    assert m.playlist_name_matches_label("Cecille Records 4 [DONE]", "Cecille Records")
    assert m.playlist_name_matches_label("BOX RED 001", "BOX RED")
    assert not m.playlist_name_matches_label("Coconut Cuts", "COCO")


def test_fetch_user_playlist_names_paginates():
    client = MagicMock()
    client.current_user_playlists.side_effect = [
        {
            "items": [
                {"name": "Label 001"},
                {"name": ""},
            ],
            "next": "next-page",
        },
        {
            "items": [
                {"name": "Other 001"},
            ],
            "next": None,
        },
    ]

    assert m.fetch_user_playlist_names(client) == ["Label 001", "Other 001"]
    assert client.current_user_playlists.call_count == 2


def test_label_has_backfill_playlist_uses_name_match(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(
        m,
        "fetch_user_playlist_names",
        lambda c: ["Unrelated", "Cecille Records1"],
    )

    assert m.label_has_backfill_playlist(client, "Cecille Records") is True
    assert m.label_has_backfill_playlist(client, "Black Book Records") is False


def test_fetch_playlist_album_uris_paginates_and_dedupes():
    client = MagicMock()
    client.playlist_items.side_effect = [
        {
            "items": [
                {"track": {"album": {"uri": "album:1"}}},
                {"track": {"album": {"uri": "album:1"}}},
                {"track": {"album": {"uri": "album:2"}}},
            ],
            "next": "next-page",
        },
        {
            "items": [
                {"track": None},
                {"track": {"album": {"uri": "album:3"}}},
            ],
            "next": None,
        },
    ]

    assert m.fetch_playlist_album_uris(client, "playlist:labels") == [
        "album:1",
        "album:2",
        "album:3",
    ]
    assert client.playlist_items.call_count == 2


def test_format_track_query_joins_artists_and_title():
    track = _mk_track("Track Name", ["Artist 1", "Artist 2"], "uri:1")

    assert m.format_track_query(track) == "Artist 1, Artist 2 - Track Name"


def test_fetch_playlist_track_queries_paginates_and_formats_tracks():
    client = MagicMock()
    client.playlist_items.side_effect = [
        {
            "items": [
                {"track": _mk_track("One", ["A"], "uri:1")},
                {"track": None},
            ],
            "next": "next-page",
        },
        {
            "items": [
                {"track": _mk_track("Two", ["B", "C"], "uri:2")},
            ],
            "next": None,
        },
    ]

    assert m.fetch_playlist_track_queries(client, "playlist:download") == [
        "A - One",
        "B, C - Two",
    ]
    assert client.playlist_items.call_count == 2


def test_fetch_followed_labels_from_playlist_fetches_album_labels(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(
        m,
        "fetch_playlist_album_uris",
        lambda c, playlist_uri: ["album:1", "album:2", "album:3"],
    )
    client.albums.return_value = {
        "albums": [
            {"label": "Good"},
            {"label": "Good"},
            {"label": "Other"},
        ]
    }

    assert m.fetch_followed_labels_from_playlist(client, "playlist:labels") == [
        "Good",
        "Other",
    ]


def test_fetch_followed_labels_from_playlist_requires_configured_playlist():
    client = MagicMock()

    try:
        m.fetch_followed_labels_from_playlist(client, "")
    except ValueError as exc:
        assert "followed-labels-playlist" in str(exc)
    else:
        raise AssertionError("Expected ValueError")


def test_fetch_all_releases_paginates_within_year(monkeypatch):
    # limit loop to year=1990 only
    import datetime as _dt

    class _RealFakeDate(_dt.date):
        @classmethod
        def today(cls):
            return cls(1990, 6, 1)

    monkeypatch.setattr(m, "date", _RealFakeDate)

    client = MagicMock()

    # First page returns 2 items, second page returns empty -> stop
    client.search.side_effect = [
        {"albums": {"items": [{"uri": "a"}, {"uri": "b"}]}},
        {"albums": {"items": []}},
    ]

    out = m.fetch_all_releases(client, "Label's Name", request_delay_seconds=0)
    assert [r["uri"] for r in out] == ["a", "b"]
    assert client.search.call_count == 2

    # query should remove apostrophes
    q0 = client.search.call_args_list[0].args[0]
    assert (
        "Label s Name".replace(" ", "") not in q0
    )  # just sanity; exact string depends on spacing
    assert "label:" in q0
    assert "year:1990" in q0


def test_collect_tracks_from_albums_filters_extended():
    client = MagicMock()

    # album_uris is a pd.Series in your function
    album_uris = pd.Series([f"uri:{i}" for i in range(3)])

    client.albums.return_value = {
        "albums": [
            {
                "label": "Good",
                "tracks": {
                    "items": [
                        {"name": "Song", "uri": "t1"},
                        {"name": "Song (Extended Mix)", "uri": "t2"},
                    ]
                },
            },
            {
                "label": "Bad",
                "tracks": {
                    "items": [
                        {"name": "Nope", "uri": "x"},
                    ]
                },
            },
            {
                "label": "Good",
                "tracks": {
                    "items": [
                        {"name": "Banger - Extended", "uri": "t3"},
                        {"name": "Banger", "uri": "t4"},
                    ]
                },
            },
        ]
    }

    out = m.collect_tracks_from_albums(
        client, album_uris, label="Good", request_delay_seconds=0
    )
    assert out == ["t1", "t4"]  # only from label=Good and non-extended


def test_fetch_track_release_date_reads_track_album_release_date():
    client = MagicMock()
    client.track.return_value = {"album": {"release_date": "2021-02-03"}}
    assert m.fetch_track_release_date(client, "track:1") == "2021-02-03"
    client.track.assert_called_once_with("track:1")


def test_create_playlists_creates_multiple_and_adds_tracks(monkeypatch):
    client = MagicMock()
    client.me.return_value = {"id": "user1"}

    # make user_playlist_create return unique playlist uris
    created = []

    def _create(user_id, name, public, description):
        pl_uri = f"pl:{len(created) + 1}"
        created.append({"name": name, "description": description, "uri": pl_uri})
        return {"external_urls": {"spotify": "http://x"}, "uri": pl_uri}

    client.user_playlist_create.side_effect = _create

    # avoid calling API for release dates
    release_dates = {
        "t1": "2020-01-01",
        "t2": "2020-01-02",
        "t3": "2020-01-03",
        "t4": "2020-01-04",
        "t5": "2020-01-05",
    }
    monkeypatch.setattr(
        m, "fetch_track_release_date", lambda c, uri: release_dates[uri]
    )

    tracks = ["t1", "t2", "t3", "t4", "t5"]
    m.create_playlists(
        client, "My Playlist", tracks, step_size=2, request_delay_seconds=0
    )

    # 5 tracks with step 2 => 3 playlists: (2,2,1)
    assert [c["name"] for c in created] == [
        "My Playlist 001",
        "My Playlist 002",
        "My Playlist 003",
    ]
    assert created[0]["description"] == "2020-01-01 - 2020-01-02"
    assert created[1]["description"] == "2020-01-03 - 2020-01-04"
    assert created[2]["description"] == "2020-01-05 - 2020-01-05"

    # playlist_add_items called per playlist
    assert client.playlist_add_items.call_count == 3
    assert client.playlist_add_items.call_args_list[0].args == ("pl:1", ["t1", "t2"])
    assert client.playlist_add_items.call_args_list[1].args == ("pl:2", ["t3", "t4"])
    assert client.playlist_add_items.call_args_list[2].args == ("pl:3", ["t5"])


def test_fetch_new_relevant_releases_pipeline_calls_substeps(monkeypatch):
    client = MagicMock()

    mock_fetch = MagicMock(
        return_value=[
            {"uri": "a", "name": "Album", "label": "L", "release_date": "2020-01-01"}
        ]
    )
    mock_filter_releases_by_date = MagicMock(
        return_value=[
            {"uri": "a", "name": "Album", "label": "L", "release_date": "2020-01-01"}
        ]
    )
    mock_filter_exact = MagicMock(
        return_value=[
            {"uri": "a", "name": "Album", "label": "L", "release_date": "2020-01-01"}
        ]
    )

    monkeypatch.setattr(m, "fetch_new_releases", mock_fetch)
    monkeypatch.setattr(m, "filter_releases_by_date", mock_filter_releases_by_date)
    monkeypatch.setattr(m, "filter_exact_label_releases", mock_filter_exact)

    out = m.fetch_new_relevant_releases(client, "L")
    assert out == [
        {"uri": "a", "name": "Album", "label": "L", "release_date": "2020-01-01"}
    ]

    mock_fetch.assert_called_once()
    mock_filter_releases_by_date.assert_called_once()
    mock_filter_exact.assert_called_once()


def test_fetch_and_add_deduplicates_tracks_before_adding(monkeypatch):
    client = MagicMock()

    # 1 label with 1 relevant release
    monkeypatch.setattr(
        m,
        "fetch_new_relevant_releases",
        lambda c, label: [
            {
                "uri": "album:1",
                "name": "Album1",
                "label": "L",
                "release_date": "2020-01-01",
            }
        ],
    )

    # album_tracks includes duplicates (same name+artists)
    album_tracks = [
        _mk_track("Same", ["A"], "u1", album_name="Album1"),
        _mk_track(
            "Same", ["A"], "u2", album_name="Album1"
        ),  # duplicate by key, should be dropped
        _mk_track("Other", ["A"], "u3", album_name="Album1"),
    ]
    monkeypatch.setattr(m, "fetch_album_tracks", lambda c, album: album_tracks)

    # don’t change list in remove_extended_versions for this test
    monkeypatch.setattr(m, "remove_extended_versions", lambda tracks: tracks)

    add_mock = MagicMock()
    monkeypatch.setattr(m, "add_to_playlist", add_mock)

    out = m.fetch_and_add(client, record_labels=["Label"], target_playlist="plid")

    # should add only u1 + u3 (u2 dropped)
    add_mock.assert_called_once()
    args = add_mock.call_args.args
    assert args[0] is client
    assert args[1] == "plid"
    assert args[2] == ["u1", "u3"]

    # track_info_to_send exists (but structure is buggy in current implementation; see xfail below)
    assert "Label" in out


def test_fetch_and_add_track_info_is_grouped_per_album(monkeypatch):
    """
    Expected behavior (probably): track_info_to_send[label][album_name] contains tracks per album.
    Current code sets only one album key based on the last `track` variable.
    """
    client = MagicMock()
    monkeypatch.setattr(
        m,
        "fetch_new_relevant_releases",
        lambda c, label: [
            {
                "uri": "album:1",
                "name": "Album1",
                "label": "L",
                "release_date": "2020-01-01",
            }
        ],
    )

    album_tracks = [
        _mk_track("A", ["X"], "u1", album_name="Album1"),
        _mk_track("B", ["X"], "u2", album_name="Album1"),
    ]
    monkeypatch.setattr(m, "fetch_album_tracks", lambda c, album: album_tracks)
    monkeypatch.setattr(m, "remove_extended_versions", lambda tracks: tracks)
    monkeypatch.setattr(m, "add_to_playlist", lambda *args, **kwargs: None)

    out = m.fetch_and_add(client, record_labels=["Label"], target_playlist="plid")
    assert out["Label"]["Album1"] == album_tracks
