from collections import Counter
from datetime import date
from pathlib import Path

from crate_digger.discover.models import SpotifyEntityTrack
from crate_digger.discover.ranking import (
    build_diverse_selection,
    rank_candidates,
    select_release_probes,
)
from crate_digger.discover.repository import (
    connect,
    list_eligible_candidates,
    upsert_spotify_tracks,
)
from crate_digger.discover.sessions import (
    build_session,
    expand_release,
    explore_label,
    get_session_item,
    record_feedback,
)
from crate_digger.discover.taste import rebuild_taste_index


def seed_track(
    db_path: Path,
    track_id: str,
    *,
    artist_id: str,
    label: str,
    release_id: str | None = None,
    release_title: str | None = None,
    title: str | None = None,
    release_date: str | None = "2020-01-01",
    track_number: int = 1,
    source: str = "followed_label",
) -> int:
    upsert_spotify_tracks(
        db_path,
        [
            SpotifyEntityTrack(
                spotify_track_id=track_id,
                spotify_uri=f"spotify:track:{track_id}",
                title=title or f"Track {track_id}",
                artists=((artist_id, artist_id.title()),),
                spotify_release_id=release_id or f"release-{track_id}",
                release_title=release_title or f"Release {track_id}",
                release_date=release_date,
                raw_label_name=label,
                track_number=track_number,
            )
        ],
        label_aliases={},
        source=source,  # type: ignore[arg-type]
    )
    with connect(db_path) as conn:
        return int(
            conn.execute(
                "select id from discovery_candidates where spotify_track_id = ?",
                (track_id,),
            ).fetchone()[0]
        )


def set_state(db_path: Path, track_id: str, state: str) -> None:
    with connect(db_path) as conn:
        conn.execute(
            "update discovery_candidates set state = ? where spotify_track_id = ?",
            (state, track_id),
        )


def test_fixed_seed_ranking_is_deterministic_and_retains_wildcard(tmp_path):
    db_path = tmp_path / "collection.sqlite3"
    seed_track(db_path, "positive", artist_id="known", label="Known")
    set_state(db_path, "positive", "kept")
    for index in range(8):
        seed_track(
            db_path,
            f"known-{index}",
            artist_id="known" if index < 2 else f"artist-{index}",
            label="Known",
            release_date="2026-08-01" if index < 4 else "2018-01-01",
        )
    seed_track(
        db_path,
        "wildcard",
        artist_id="unknown",
        label="Unknown Frontier",
        release_date=None,
        source="discovered_label",
    )
    rebuild_taste_index(db_path)
    candidates = list_eligible_candidates(db_path)

    first_rank = rank_candidates(
        db_path,
        candidates,
        mode="balanced",
        seed=42,
        freshness_days=90,
        today=date(2026, 8, 6),
    )
    second_rank = rank_candidates(
        db_path,
        candidates,
        mode="balanced",
        seed=42,
        freshness_days=90,
        today=date(2026, 8, 6),
    )
    first, _ = build_diverse_selection(first_rank, mode="balanced", size=6)
    second, _ = build_diverse_selection(second_rank, mode="balanced", size=6)

    assert [item.candidate.spotify_track_id for item in first] == [
        item.candidate.spotify_track_id for item in second
    ]
    assert any(item.bucket == "wildcard" for item in first)
    assert any(
        "Underexplored label" in reason for item in first for reason in item.reasons
    )
    assert any(item.bucket == "taste-adjacent" for item in first)
    adjacent = next(item for item in first if item.bucket == "taste-adjacent")
    assert any(
        "affinity" in reason or "positive taste" in reason
        for reason in adjacent.reasons
    )
    assert all(
        any("initial probe" in reason for reason in item.reasons) for item in first
    )


def test_release_probe_prefers_release_title_and_limits_initial_release(tmp_path):
    db_path = tmp_path / "collection.sqlite3"
    for track_id, title, number in (
        ("intro", "Intro", 1),
        ("title", "Night Moves", 2),
        ("other", "Late Tool", 3),
    ):
        seed_track(
            db_path,
            track_id,
            artist_id="artist",
            label="Label",
            release_id="release-one",
            release_title="Night Moves",
            title=title,
            track_number=number,
        )

    probes = select_release_probes(list_eligible_candidates(db_path))

    assert len(probes) == 1
    assert probes[0].spotify_track_id == "title"


def test_diversity_limits_artist_label_and_release_when_alternatives_exist(tmp_path):
    db_path = tmp_path / "collection.sqlite3"
    for index in range(12):
        seed_track(
            db_path,
            f"track-{index}",
            artist_id=f"artist-{index % 6}",
            label=f"label-{index % 4}",
            release_id=f"release-{index}",
            release_date="2020-01-01",
        )
    rebuild_taste_index(db_path)
    ranked = rank_candidates(
        db_path,
        list_eligible_candidates(db_path),
        mode="deep-dig",
        seed=7,
        freshness_days=90,
        today=date(2026, 8, 6),
    )
    selected, _ = build_diverse_selection(ranked, mode="deep-dig", size=8)

    artists = Counter(item.candidate.artist_id for item in selected)
    labels = Counter(item.candidate.label_id for item in selected)
    releases = Counter(item.candidate.release_id for item in selected)
    assert max(artists.values()) <= 2
    assert max(labels.values()) <= 3
    assert max(releases.values()) == 1
    consecutive = [
        left.candidate.label_id == right.candidate.label_id
        for left, right in zip(selected, selected[1:])
    ]
    assert not any(consecutive)


def test_kept_and_passed_are_avoided_while_skip_can_return(tmp_path):
    db_path = tmp_path / "collection.sqlite3"
    seed_track(db_path, "kept", artist_id="a", label="L")
    seed_track(db_path, "passed", artist_id="b", label="L")
    seed_track(db_path, "review", artist_id="c", label="Other")
    set_state(db_path, "kept", "kept")
    set_state(db_path, "passed", "passed")
    rebuild_taste_index(db_path)

    eligible = {item.spotify_track_id for item in list_eligible_candidates(db_path)}
    assert eligible == {"review"}
    session = build_session(db_path, size=1)
    item = session.items[0]
    record_feedback(
        db_path,
        session_id=session.session.session_id,
        item_id=item.item_id,
        decision="skip",
    )
    assert {item.spotify_track_id for item in list_eligible_candidates(db_path)} == {
        "review"
    }


def test_expand_release_only_adds_remaining_tracks(tmp_path):
    db_path = tmp_path / "collection.sqlite3"
    for index in range(3):
        seed_track(
            db_path,
            f"release-track-{index}",
            artist_id="artist",
            label="Label",
            release_id="shared-release",
            release_title="Shared Release",
            track_number=index + 1,
        )
    rebuild_taste_index(db_path)
    session = build_session(db_path, size=1)
    expanded = expand_release(
        db_path,
        session_id=session.session.session_id,
        item_id=session.items[0].item_id,
    )

    assert len(expanded) == 2
    with connect(db_path) as conn:
        sources = {
            row[0]
            for row in conn.execute(
                "select discovery_source from discovery_candidates where id in (?, ?)",
                expanded,
            )
        }
    assert sources == {"release_expansion"}


def test_explore_label_creates_bounded_release_sampler(tmp_path):
    db_path = tmp_path / "collection.sqlite3"
    for release in range(8):
        for track in range(2):
            seed_track(
                db_path,
                f"r{release}-t{track}",
                artist_id=f"artist-{release}",
                label="Sampler Label",
                release_id=f"release-{release}",
                release_title=f"Release {release}",
                release_date=f"{2016 + release}-01-01",
                track_number=track + 1,
            )
    rebuild_taste_index(db_path)
    session = build_session(db_path, size=1)
    sampled = explore_label(
        db_path,
        session_id=session.session.session_id,
        item_id=session.items[0].item_id,
    )

    assert 1 <= len(sampled) <= 5
    with connect(db_path) as conn:
        release_count = conn.execute(
            f"""
            select count(distinct t.release_id)
            from discovery_candidates c join discovery_tracks t
              on t.spotify_track_id = c.spotify_track_id
            where c.id in ({",".join("?" for _ in sampled)})
            """,
            sampled,
        ).fetchone()[0]
    assert release_count == len(sampled)


def test_unknown_label_overlap_and_old_explanations_are_preserved(tmp_path):
    db_path = tmp_path / "collection.sqlite3"
    for track_id, artist in (("kept-a", "artist-a"), ("kept-b", "artist-b")):
        seed_track(db_path, track_id, artist_id=artist, label="Unknown Label")
        set_state(db_path, track_id, "kept")
    seed_track(
        db_path,
        "candidate",
        artist_id="artist-c",
        label="Unknown Label",
        source="discovered_label",
    )
    rebuild_taste_index(db_path)
    result = build_session(db_path, mode="frontier", size=1)
    original_reasons = result.items[0].reasons_at_selection
    assert any("contains 2 artist" in reason for reason in original_reasons)

    record_feedback(
        db_path,
        session_id=result.session.session_id,
        item_id=result.items[0].item_id,
        decision="keep",
    )
    stored = get_session_item(
        db_path, result.session.session_id, result.items[0].item_id
    )
    assert stored is not None
    assert stored.reasons_at_selection == original_reasons


def test_archive_selection_spreads_across_periods(tmp_path):
    db_path = tmp_path / "collection.sqlite3"
    for index, year in enumerate((2010, 2011, 2015, 2016, 2020, 2021)):
        seed_track(
            db_path,
            f"archive-{index}",
            artist_id=f"artist-{index}",
            label=f"label-{index}",
            release_date=f"{year}-01-01",
        )
    rebuild_taste_index(db_path)
    ranked = rank_candidates(
        db_path,
        list_eligible_candidates(db_path),
        mode="deep-dig",
        seed=1,
        freshness_days=90,
        today=date(2026, 8, 6),
    )
    selected, _ = build_diverse_selection(ranked, mode="deep-dig", size=3)

    periods = {int(str(item.candidate.release_date)[:4]) // 5 for item in selected}
    assert len(periods) == 3


def test_spotify_partial_release_dates_still_classify_as_archive(tmp_path):
    db_path = tmp_path / "collection.sqlite3"
    seed_track(
        db_path,
        "year-only",
        artist_id="historical-artist",
        label="Historical Label",
        release_date="2012",
    )
    rebuild_taste_index(db_path)

    ranked = rank_candidates(
        db_path,
        list_eligible_candidates(db_path),
        mode="balanced",
        seed=1,
        freshness_days=90,
        today=date(2026, 8, 6),
    )

    assert ranked[0].bucket == "archive"
