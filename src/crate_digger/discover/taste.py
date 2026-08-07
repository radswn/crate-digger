import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable

from crate_digger.discover.models import AffinityStats, EntityType, TasteRebuildSummary
from crate_digger.discover.repository import connect, now_iso


SIGNAL_WEIGHTS = {
    "keep": 3.0,
    "dj_library": 2.5,
    "track_profile": 2.0,
    "maybe": 1.5,
    "curated_playlist": 1.25,
    "pass": 3.0,
}
HISTORICAL_RATING_WEIGHTS = {1: 0.1, 2: 0.2, 3: 0.5, 4: 0.9, 5: 1.25}
PRIOR_POSITIVE = 1.0
PRIOR_TOTAL = 2.0
CONFIDENCE_PRIOR_SAMPLE = 5.0


def rebuild_taste_index(db_path: Path) -> TasteRebuildSummary:
    now = now_iso()
    with connect(db_path) as conn:
        _rebuild_derived_signals(conn, now)
        track_signal_rows = conn.execute(
            """
            select spotify_track_id, signal_type, signal_value * weight as weighted
            from taste_signals
            order by spotify_track_id, signal_type, source
            """
        ).fetchall()
        signals_by_track: dict[str, list[tuple[str, float]]] = defaultdict(list)
        for row in track_signal_rows:
            signals_by_track[str(row["spotify_track_id"])].append(
                (str(row["signal_type"]), float(row["weighted"]))
            )

        track_ids = {
            str(row[0])
            for row in conn.execute(
                "select spotify_track_id from discovery_tracks"
            ).fetchall()
        }
        positive_tracks = {
            track_id
            for track_id, signals in signals_by_track.items()
            if any(weighted > 0 for _kind, weighted in signals)
        }
        negative_tracks = {
            track_id
            for track_id, signals in signals_by_track.items()
            if any(weighted < 0 for _kind, weighted in signals)
        }
        neutral_tracks = track_ids - positive_tracks - negative_tracks
        summary_payload = {
            "tracks_inspected": len(track_ids),
            "positive_taste_tracks": len(positive_tracks),
            "negative_taste_tracks": len(negative_tracks),
            "neutral_catalogue_tracks": len(neutral_tracks),
        }
        cursor = conn.execute(
            "insert into discovery_taste_snapshots (created_at, summary) values (?, ?)",
            (now, json.dumps(summary_payload, sort_keys=True)),
        )
        if cursor.lastrowid is None:
            raise sqlite3.DatabaseError("Could not create taste snapshot")
        snapshot_version = int(cursor.lastrowid)

        memberships = _entity_memberships(conn)
        affinities = [
            _aggregate_entity(
                entity_type=entity_type,
                entity_key=entity_key,
                entity_name=entity_name,
                track_ids=entity_tracks,
                signals_by_track=signals_by_track,
                snapshot_version=snapshot_version,
            )
            for (
                entity_type,
                entity_key,
                entity_name,
            ), entity_tracks in memberships.items()
        ]
        conn.execute("delete from taste_affinities")
        conn.executemany(
            """
            insert into taste_affinities (
                entity_type, entity_key, entity_name, positive_evidence_count,
                negative_evidence_count, neutral_indexed_count,
                weighted_positive_score, weighted_negative_score,
                smoothed_affinity, sample_size, confidence,
                top_contributing_signals, snapshot_version, updated_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    item.entity_type,
                    item.entity_key,
                    item.entity_name,
                    item.positive_evidence_count,
                    item.negative_evidence_count,
                    item.neutral_indexed_count,
                    item.weighted_positive_score,
                    item.weighted_negative_score,
                    item.smoothed_affinity,
                    item.sample_size,
                    item.confidence,
                    json.dumps(item.top_contributing_signals),
                    item.snapshot_version,
                    now,
                )
                for item in affinities
            ],
        )
        _mark_interesting_unknown_labels(conn, affinities, now)

    evidence_counts = Counter(
        item.entity_type for item in affinities if item.sample_size > 0
    )
    return TasteRebuildSummary(
        snapshot_version=snapshot_version,
        tracks_inspected=len(track_ids),
        positive_taste_tracks=len(positive_tracks),
        negative_taste_tracks=len(negative_tracks),
        neutral_catalogue_tracks=len(neutral_tracks),
        artists_with_evidence=evidence_counts["artist"],
        labels_with_evidence=evidence_counts["label"],
        tags_with_evidence=evidence_counts["tag"],
        sources_with_evidence=evidence_counts["source"],
    )


def smoothed_affinity(positive_weight: float, negative_weight: float) -> float:
    return (positive_weight + PRIOR_POSITIVE) / (
        positive_weight + negative_weight + PRIOR_TOTAL
    )


def _rebuild_derived_signals(conn: sqlite3.Connection, now: str) -> None:
    conn.execute("delete from taste_signals")

    local_rows = conn.execute(
        """
        select spotify_track_id from discovery_tracks
        where local_track_path is not null
        """
    ).fetchall()
    _insert_signals(
        conn,
        (
            (
                str(row[0]),
                "dj_library",
                1.0,
                SIGNAL_WEIGHTS["dj_library"],
                "local_collection",
                {"basis": "indexed local audio file"},
            )
            for row in local_rows
        ),
        now,
    )

    profile_rows = conn.execute(
        """
        select d.spotify_track_id, p.energy, p.personal_rating, p.set_role
        from discovery_tracks d
        join track_profiles p on p.track_path = d.local_track_path
        where p.energy is not null
          and p.personal_rating is not null
          and p.set_role is not null
        """
    ).fetchall()
    _insert_signals(
        conn,
        (
            (
                str(row["spotify_track_id"]),
                "track_profile",
                1.0,
                SIGNAL_WEIGHTS["track_profile"],
                "track_profiles",
                {
                    "energy": row["energy"],
                    "personal_rating": row["personal_rating"],
                    "set_role": row["set_role"],
                },
            )
            for row in profile_rows
        ),
        now,
    )

    rating_rows = conn.execute(
        """
        select d.spotify_track_id, max(s.legacy_rating) as legacy_rating,
               group_concat(s.source) as rating_sources
        from discovery_tracks d
        join track_source_metadata s on s.track_path = d.local_track_path
        where s.legacy_rating is not null
        group by d.spotify_track_id
        """
    ).fetchall()
    _insert_signals(
        conn,
        (
            (
                str(row["spotify_track_id"]),
                "star_rating",
                1.0,
                HISTORICAL_RATING_WEIGHTS[int(row["legacy_rating"])],
                "historical_rating",
                {
                    "legacy_rating": row["legacy_rating"],
                    "sources": str(row["rating_sources"]).split(","),
                },
            )
            for row in rating_rows
        ),
        now,
    )

    curated_rows = conn.execute(
        """
        select distinct spotify_track_id, playlist_name
        from discovery_playlist_memberships
        where source_type = 'curated_positive'
        """
    ).fetchall()
    _insert_signals(
        conn,
        (
            (
                str(row["spotify_track_id"]),
                "curated_playlist",
                1.0,
                SIGNAL_WEIGHTS["curated_playlist"],
                f"curated_playlist:{row['playlist_name']}",
                {"playlist": row["playlist_name"]},
            )
            for row in curated_rows
        ),
        now,
    )

    feedback_rows = conn.execute(
        """
        select spotify_track_id, state from discovery_candidates
        where state in ('kept', 'maybe', 'passed')
        """
    ).fetchall()
    _insert_signals(
        conn,
        (
            (
                str(row["spotify_track_id"]),
                "pass"
                if row["state"] == "passed"
                else "keep"
                if row["state"] == "kept"
                else "maybe",
                -1.0 if row["state"] == "passed" else 1.0,
                SIGNAL_WEIGHTS[
                    "pass"
                    if row["state"] == "passed"
                    else "keep"
                    if row["state"] == "kept"
                    else "maybe"
                ],
                "discovery_feedback",
                {"candidate_state": row["state"]},
            )
            for row in feedback_rows
        ),
        now,
    )


def _insert_signals(
    conn: sqlite3.Connection,
    signals: Iterable[tuple[str, str, float, float, str, dict[str, object]]],
    now: str,
) -> None:
    conn.executemany(
        """
        insert into taste_signals (
            spotify_track_id, signal_type, signal_value, weight, source,
            created_at, updated_at, metadata
        ) values (?, ?, ?, ?, ?, ?, ?, ?)
        on conflict(spotify_track_id, signal_type, source) do update set
            signal_value = excluded.signal_value,
            weight = excluded.weight,
            updated_at = excluded.updated_at,
            metadata = excluded.metadata
        """,
        [
            (
                track_id,
                signal_type,
                value,
                weight,
                source,
                now,
                now,
                json.dumps(metadata, sort_keys=True),
            )
            for track_id, signal_type, value, weight, source, metadata in signals
        ],
    )


def _entity_memberships(
    conn: sqlite3.Connection,
) -> dict[tuple[EntityType, str, str], set[str]]:
    memberships: dict[tuple[EntityType, str, str], set[str]] = defaultdict(set)
    for row in conn.execute(
        """
        select ta.spotify_track_id, a.spotify_artist_id, a.name
        from discovery_track_artists ta
        join discovery_artists a on a.spotify_artist_id = ta.spotify_artist_id
        """
    ):
        memberships[("artist", str(row[1]), str(row[2]))].add(str(row[0]))
    for row in conn.execute(
        """
        select t.spotify_track_id, cast(l.id as text), l.display_name
        from discovery_tracks t
        join discovery_releases r on r.spotify_release_id = t.release_id
        join discovery_labels l on l.id = r.label_id
        """
    ):
        memberships[("label", str(row[1]), str(row[2]))].add(str(row[0]))
    for row in conn.execute(
        """
        select d.spotify_track_id, tags.category || ':' || tags.value,
               tags.value
        from discovery_tracks d
        join track_tags tags on tags.track_path = d.local_track_path
        where tags.approved = 1
        """
    ):
        memberships[("tag", str(row[1]), str(row[2]))].add(str(row[0]))
    for row in conn.execute(
        "select spotify_track_id, discovery_source from discovery_candidates"
    ):
        name = str(row[1]).replace("_", " ").title()
        memberships[("source", str(row[1]), name)].add(str(row[0]))
    return memberships


def _aggregate_entity(
    *,
    entity_type: EntityType,
    entity_key: str,
    entity_name: str,
    track_ids: set[str],
    signals_by_track: dict[str, list[tuple[str, float]]],
    snapshot_version: int,
) -> AffinityStats:
    positive_count = 0
    negative_count = 0
    neutral_count = 0
    positive_weight = 0.0
    negative_weight = 0.0
    contributors: Counter[str] = Counter()
    for track_id in track_ids:
        signals = signals_by_track.get(track_id, [])
        positive = [item for item in signals if item[1] > 0]
        negative = [item for item in signals if item[1] < 0]
        if positive:
            positive_count += 1
            positive_weight += sum(weight for _kind, weight in positive)
        if negative:
            negative_count += 1
            negative_weight += sum(abs(weight) for _kind, weight in negative)
        if not positive and not negative:
            neutral_count += 1
        contributors.update(kind for kind, _weight in positive + negative)
    sample_size = positive_count + negative_count
    return AffinityStats(
        entity_type=entity_type,
        entity_key=entity_key,
        entity_name=entity_name,
        positive_evidence_count=positive_count,
        negative_evidence_count=negative_count,
        neutral_indexed_count=neutral_count,
        weighted_positive_score=round(positive_weight, 4),
        weighted_negative_score=round(negative_weight, 4),
        smoothed_affinity=round(smoothed_affinity(positive_weight, negative_weight), 6),
        sample_size=sample_size,
        confidence=round(sample_size / (sample_size + CONFIDENCE_PRIOR_SAMPLE), 6),
        top_contributing_signals=tuple(contributors.most_common(5)),
        snapshot_version=snapshot_version,
    )


def _mark_interesting_unknown_labels(
    conn: sqlite3.Connection,
    affinities: list[AffinityStats],
    now: str,
) -> None:
    positive_artist_ids = [
        affinity.entity_key
        for affinity in affinities
        if affinity.entity_type == "artist"
        and affinity.sample_size > 0
        and affinity.smoothed_affinity > 0.55
    ]
    if not positive_artist_ids:
        return
    placeholders = ",".join("?" for _ in positive_artist_ids)
    rows = conn.execute(
        f"""
        select r.label_id, count(distinct ta.spotify_artist_id) as overlap
        from discovery_track_artists ta
        join discovery_tracks t on t.spotify_track_id = ta.spotify_track_id
        join discovery_releases r on r.spotify_release_id = t.release_id
        join discovery_labels l on l.id = r.label_id
        where l.followed = 0 and ta.spotify_artist_id in ({placeholders})
        group by r.label_id having count(distinct ta.spotify_artist_id) >= 2
        """,
        positive_artist_ids,
    ).fetchall()
    for label_id, overlap in rows:
        conn.execute(
            """
            update discovery_labels
            set interest_reason = coalesce(
                    interest_reason, ?
                ), updated_at = ?
            where id = ?
            """,
            (
                f"contains {int(overlap)} artists with positive taste evidence",
                now,
                int(label_id),
            ),
        )
