import sqlite3
from pathlib import Path

from crate_digger.collection.index import _ensure_schema
from crate_digger.collection.models import SourceTrackMetadata
from crate_digger.collection.profiles import upsert_source_metadata
from crate_digger.discover.indexer import (
    enrich_missing_spotify_metadata,
    index_existing_playlists,
    index_local_collection,
)
from crate_digger.discover.labels import normalize_label_name
from crate_digger.discover.models import SpotifyEntityTrack
from crate_digger.discover.repository import (
    connect,
    find_affinity,
    upsert_spotify_tracks,
)
from crate_digger.discover.taste import (
    HISTORICAL_RATING_WEIGHTS,
    SIGNAL_WEIGHTS,
    rebuild_taste_index,
    smoothed_affinity,
)


def seed_remote(
    db_path: Path,
    track_id: str,
    *,
    artist_id: str = "artist-1",
    artist_name: str = "Artist One",
    release_id: str | None = None,
    release_title: str | None = None,
    label: str = "Issues Records",
    release_date: str | None = "2024-01-01",
    track_number: int = 1,
    source: str = "followed_label",
) -> None:
    release_id = release_id or f"release-{track_id}"
    release_title = release_title or f"Release {track_id}"
    upsert_spotify_tracks(
        db_path,
        [
            SpotifyEntityTrack(
                spotify_track_id=track_id,
                spotify_uri=f"spotify:track:{track_id}",
                title=f"Track {track_id}",
                artists=((artist_id, artist_name),),
                spotify_release_id=release_id,
                release_title=release_title,
                release_date=release_date,
                raw_label_name=label,
                track_number=track_number,
            )
        ],
        label_aliases={"Issues Records": "Issues"},
        source=source,  # type: ignore[arg-type]
    )


def set_state(db_path: Path, track_id: str, state: str) -> None:
    with connect(db_path) as conn:
        conn.execute(
            "update discovery_candidates set state = ? where spotify_track_id = ?",
            (state, track_id),
        )


def test_label_normalization_is_conservative_and_supports_aliases():
    punctuation = normalize_label_name("  Hot-Creations.  ")
    alias = normalize_label_name("ISSUES RECORDS", {"issues-records": "Issues"})

    assert punctuation.normalized_name == "hot creations"
    assert punctuation.display_name == "Hot-Creations."
    assert not punctuation.alias_applied
    assert alias.normalized_name == "issues"
    assert alias.display_name == "Issues"
    assert alias.alias_applied


def test_unreviewed_backfill_tracks_remain_neutral_and_smoothing_shows_sample(tmp_path):
    db_path = tmp_path / "collection.sqlite3"
    for index in range(4):
        seed_remote(db_path, f"neutral-{index}")

    summary = rebuild_taste_index(db_path)
    label = find_affinity(db_path, entity_type="label", name="Issues")

    assert summary.positive_taste_tracks == 0
    assert summary.negative_taste_tracks == 0
    assert summary.neutral_catalogue_tracks == 4
    assert label is not None
    assert label.smoothed_affinity == 0.5
    assert label.sample_size == 0
    assert label.neutral_indexed_count == 4
    assert smoothed_affinity(3.0, 0.0) == 0.8


def test_keep_maybe_and_pass_have_readable_weights_without_blacklisting_label(tmp_path):
    db_path = tmp_path / "collection.sqlite3"
    for track_id, state in (
        ("keep", "kept"),
        ("maybe", "maybe"),
        ("pass", "passed"),
        ("neutral", "new"),
    ):
        seed_remote(db_path, track_id)
        set_state(db_path, track_id, state)

    rebuild_taste_index(db_path)
    label = find_affinity(db_path, entity_type="label", name="Issues")

    assert label is not None
    assert label.positive_evidence_count == 2
    assert label.negative_evidence_count == 1
    assert label.neutral_indexed_count == 1
    assert (
        label.weighted_positive_score
        == SIGNAL_WEIGHTS["keep"] + SIGNAL_WEIGHTS["maybe"]
    )
    assert label.weighted_negative_score == SIGNAL_WEIGHTS["pass"]
    assert label.smoothed_affinity > 0.5


def test_artist_and_label_affinity_aggregate_and_rebuild_idempotently(tmp_path):
    db_path = tmp_path / "collection.sqlite3"
    seed_remote(db_path, "one", artist_id="artist-a", artist_name="Iglesias")
    seed_remote(db_path, "two", artist_id="artist-a", artist_name="Iglesias")
    set_state(db_path, "one", "kept")

    first = rebuild_taste_index(db_path)
    with connect(db_path) as conn:
        first_signal_count = conn.execute(
            "select count(*) from taste_signals"
        ).fetchone()[0]
    second = rebuild_taste_index(db_path)
    with connect(db_path) as conn:
        second_signal_count = conn.execute(
            "select count(*) from taste_signals"
        ).fetchone()[0]

    artist = find_affinity(db_path, entity_type="artist", name="Iglesias")
    label = find_affinity(db_path, entity_type="label", name="Issues")
    assert artist is not None and label is not None
    assert artist.positive_evidence_count == label.positive_evidence_count == 1
    assert artist.neutral_indexed_count == label.neutral_indexed_count == 1
    assert first_signal_count == second_signal_count == 1
    assert second.snapshot_version == first.snapshot_version + 1


def test_historical_rating_weight_is_lower_than_explicit_keep(tmp_path):
    db_path = tmp_path / "collection.sqlite3"
    local_path = tmp_path / "rated.mp3"
    local_path.write_bytes(b"audio")
    with sqlite3.connect(db_path) as conn:
        _ensure_schema(conn)
        conn.execute(
            """
            insert into tracks (
                path, stem, title, artist, audio_format, spotify_uri,
                artwork_checked, size, mtime_ns, indexed_at
            ) values (?, 'rated', 'Rated', 'Ada', 'MP3', 'spotify:track:rated',
                      1, 1, 1, '2026-01-01T00:00:00+00:00')
            """,
            (str(local_path),),
        )
    index_local_collection(db_path, label_aliases={})
    upsert_source_metadata(
        db_path,
        metadata=SourceTrackMetadata(
            track_path=str(local_path),
            source="rekordbox",
            source_track_id="rated",
            legacy_rating=5,
            genre=None,
            comment=None,
            comment2=None,
            imported_at="2026-01-01T00:00:00+00:00",
        ),
    )
    seed_remote(db_path, "kept")
    set_state(db_path, "kept", "kept")

    rebuild_taste_index(db_path)
    with connect(db_path) as conn:
        star_weight = conn.execute(
            "select weight from taste_signals where signal_type = 'star_rating'"
        ).fetchone()[0]
        keep_weight = conn.execute(
            "select weight from taste_signals where signal_type = 'keep'"
        ).fetchone()[0]

    assert star_weight == HISTORICAL_RATING_WEIGHTS[5]
    assert star_weight < keep_weight


class FakeSpotifyClient:
    def tracks(self, uris):
        return {
            "tracks": [
                {
                    "id": "linked",
                    "uri": "spotify:track:linked",
                    "name": "Linked Track",
                    "artists": [{"id": "artist-linked", "name": "Linked Artist"}],
                    "album": {
                        "id": "album-linked",
                        "name": "Linked Release",
                        "release_date": "2026-01-02",
                    },
                    "track_number": 1,
                }
            ]
        }

    def albums(self, ids):
        return {
            "albums": [
                {
                    "id": "album-linked",
                    "name": "Linked Release",
                    "release_date": "2026-01-02",
                    "label": "Issues Records",
                }
            ]
        }


def test_spotify_enrichment_links_local_track_release_artist_and_label(tmp_path):
    db_path = tmp_path / "collection.sqlite3"
    with sqlite3.connect(db_path) as conn:
        _ensure_schema(conn)
        conn.execute(
            """
            insert into tracks (
                path, stem, title, artist, audio_format, spotify_uri,
                artwork_checked, size, mtime_ns, indexed_at
            ) values ('/music/linked.mp3', 'linked', 'Linked Track', 'Linked Artist',
                      'MP3', 'spotify:track:linked', 1, 1, 1,
                      '2026-01-01T00:00:00+00:00')
            """
        )
    index_local_collection(db_path, label_aliases={})
    summary = enrich_missing_spotify_metadata(
        FakeSpotifyClient(),
        db_path,
        label_aliases={"Issues Records": "Issues"},
    )

    with connect(db_path) as conn:
        linked = conn.execute(
            """
            select t.primary_artist_id, t.release_id, l.display_name
            from discovery_tracks t
            join discovery_releases r on r.spotify_release_id = t.release_id
            join discovery_labels l on l.id = r.label_id
            where t.spotify_track_id = 'linked'
            """
        ).fetchone()
    assert tuple(linked) == ("artist-linked", "album-linked", "Issues")
    assert summary.tracks_linked_to_releases == 1
    assert summary.tracks_linked_to_labels == 1
    assert summary.label_aliases_applied >= 1


class FakePlaylistClient:
    def current_user_playlists(self, *, limit, offset):
        return {
            "items": (
                [{"uri": "spotify:playlist:issues", "name": "Issues Records 001"}]
                if offset == 0
                else []
            ),
            "next": None,
        }

    def playlist_items(self, playlist_uri, *, limit, offset, additional_types):
        return {
            "items": [
                {
                    "track": {
                        "id": "catalogue-track",
                        "uri": "spotify:track:catalogue-track",
                        "name": "Catalogue Track",
                        "artists": [{"id": "catalogue-artist", "name": "Artist"}],
                        "album": {
                            "id": "catalogue-release",
                            "name": "Catalogue Release",
                            "release_date": "2020-01-01",
                        },
                    }
                }
            ],
            "next": None,
        }

    def albums(self, ids):
        return {
            "albums": [
                {
                    "id": "catalogue-release",
                    "name": "Catalogue Release",
                    "release_date": "2020-01-01",
                    "label": "Issues Records",
                }
            ]
        }

    def tracks(self, uris):
        return {"tracks": []}


def test_followed_label_playlist_catalogue_indexes_idempotently_and_stays_neutral(
    tmp_path,
):
    db_path = tmp_path / "collection.sqlite3"
    client = FakePlaylistClient()
    first = index_existing_playlists(
        client,
        db_path,
        followed_labels=["Issues Records"],
        configured_playlists={},
        label_aliases={"Issues Records": "Issues"},
    )
    second = index_existing_playlists(
        client,
        db_path,
        followed_labels=["Issues Records"],
        configured_playlists={},
        label_aliases={"Issues Records": "Issues"},
    )
    enrich_missing_spotify_metadata(
        client,
        db_path,
        label_aliases={"Issues Records": "Issues"},
    )
    taste = rebuild_taste_index(db_path)

    assert first.created_candidates == 1
    assert second.created_candidates == 0
    assert second.already_indexed == 1
    assert taste.neutral_catalogue_tracks == 1
    label = find_affinity(db_path, entity_type="label", name="Issues")
    assert label is not None
    assert label.sample_size == 0
    assert label.neutral_indexed_count == 1
