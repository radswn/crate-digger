import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from crate_digger.collection.index import DEFAULT_COLLECTION_DB_PATH, _ensure_schema
from crate_digger.discover.labels import normalize_entity_name, normalize_label_name
from crate_digger.discover.models import (
    AffinityStats,
    CandidateRecord,
    CandidateState,
    DiscoverySource,
    EntityType,
    SpotifyEntityTrack,
)
from crate_digger.discover.schema import ensure_discovery_schema


def connect(db_path: Path = DEFAULT_COLLECTION_DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    ensure_discovery_schema(conn)
    return conn


def upsert_spotify_tracks(
    db_path: Path,
    tracks: list[SpotifyEntityTrack],
    *,
    label_aliases: dict[str, str],
    source: DiscoverySource,
    provenance: dict[str, object] | None = None,
    playlist: tuple[str, str, str] | None = None,
) -> tuple[int, int, int]:
    created = 0
    already_indexed = 0
    aliases_applied = 0
    now = _now()
    with connect(db_path) as conn:
        for track in tracks:
            existed = conn.execute(
                "select 1 from discovery_tracks where spotify_track_id = ?",
                (track.spotify_track_id,),
            ).fetchone()
            label_id = None
            if track.raw_label_name:
                normalized = normalize_label_name(track.raw_label_name, label_aliases)
                aliases_applied += normalized.alias_applied
                label_id = _upsert_label(
                    conn,
                    normalized_name=normalized.normalized_name,
                    display_name=normalized.display_name,
                    now=now,
                )
            if track.spotify_release_id and track.release_title:
                conn.execute(
                    """
                    insert into discovery_releases (
                        spotify_release_id, title, release_date, raw_label_name,
                        label_id, created_at, updated_at
                    ) values (?, ?, ?, ?, ?, ?, ?)
                    on conflict(spotify_release_id) do update set
                        title = excluded.title,
                        release_date = coalesce(excluded.release_date, discovery_releases.release_date),
                        raw_label_name = coalesce(excluded.raw_label_name, discovery_releases.raw_label_name),
                        label_id = coalesce(excluded.label_id, discovery_releases.label_id),
                        updated_at = excluded.updated_at
                    """,
                    (
                        track.spotify_release_id,
                        track.release_title,
                        track.release_date,
                        track.raw_label_name,
                        label_id,
                        now,
                        now,
                    ),
                )
            for artist_id, artist_name in track.artists:
                conn.execute(
                    """
                    insert into discovery_artists (
                        spotify_artist_id, name, normalized_name, created_at, updated_at
                    ) values (?, ?, ?, ?, ?)
                    on conflict(spotify_artist_id) do update set
                        name = excluded.name,
                        normalized_name = excluded.normalized_name,
                        updated_at = excluded.updated_at
                    """,
                    (
                        artist_id,
                        artist_name,
                        normalize_entity_name(artist_name),
                        now,
                        now,
                    ),
                )
            primary_artist_id = track.artists[0][0] if track.artists else None
            conn.execute(
                """
                insert into discovery_tracks (
                    spotify_track_id, spotify_uri, local_track_path, release_id,
                    primary_artist_id, title, track_number, disc_number,
                    duration_ms, preview_url, external_url, indexed_at, updated_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(spotify_track_id) do update set
                    spotify_uri = excluded.spotify_uri,
                    local_track_path = coalesce(excluded.local_track_path, discovery_tracks.local_track_path),
                    release_id = coalesce(excluded.release_id, discovery_tracks.release_id),
                    primary_artist_id = coalesce(excluded.primary_artist_id, discovery_tracks.primary_artist_id),
                    title = excluded.title,
                    track_number = coalesce(excluded.track_number, discovery_tracks.track_number),
                    disc_number = coalesce(excluded.disc_number, discovery_tracks.disc_number),
                    duration_ms = coalesce(excluded.duration_ms, discovery_tracks.duration_ms),
                    preview_url = coalesce(excluded.preview_url, discovery_tracks.preview_url),
                    external_url = coalesce(excluded.external_url, discovery_tracks.external_url),
                    updated_at = excluded.updated_at
                """,
                (
                    track.spotify_track_id,
                    track.spotify_uri,
                    track.local_track_path,
                    track.spotify_release_id,
                    primary_artist_id,
                    track.title,
                    track.track_number,
                    track.disc_number,
                    track.duration_ms,
                    track.preview_url,
                    track.external_url,
                    now,
                    now,
                ),
            )
            if track.artists:
                conn.execute(
                    "delete from discovery_track_artists where spotify_track_id = ?",
                    (track.spotify_track_id,),
                )
                conn.executemany(
                    """
                    insert into discovery_track_artists (
                        spotify_track_id, spotify_artist_id, position
                    ) values (?, ?, ?)
                    """,
                    [
                        (track.spotify_track_id, artist_id, position)
                        for position, (artist_id, _name) in enumerate(track.artists)
                    ],
                )
            cursor = conn.execute(
                """
                insert into discovery_candidates (
                    spotify_track_id, discovery_source, provenance,
                    created_at, updated_at
                ) values (?, ?, ?, ?, ?)
                on conflict(spotify_track_id) do nothing
                """,
                (
                    track.spotify_track_id,
                    source,
                    json.dumps(provenance or {}, sort_keys=True),
                    now,
                    now,
                ),
            )
            created += cursor.rowcount > 0
            already_indexed += existed is not None
            if playlist is not None:
                playlist_uri, playlist_name, source_type = playlist
                conn.execute(
                    """
                    insert into discovery_playlist_memberships (
                        spotify_track_id, playlist_uri, playlist_name,
                        source_type, indexed_at
                    ) values (?, ?, ?, ?, ?)
                    on conflict(spotify_track_id, playlist_uri) do update set
                        playlist_name = excluded.playlist_name,
                        source_type = excluded.source_type,
                        indexed_at = excluded.indexed_at
                    """,
                    (
                        track.spotify_track_id,
                        playlist_uri,
                        playlist_name,
                        source_type,
                        now,
                    ),
                )
    return created, already_indexed, aliases_applied


def list_local_spotify_tracks(db_path: Path) -> list[sqlite3.Row]:
    with connect(db_path) as conn:
        return conn.execute(
            """
            select path, title, artist, album, release_date, spotify_uri
            from tracks where spotify_uri is not null and spotify_uri != ''
            order by lower(path)
            """
        ).fetchall()


def list_track_uris_missing_metadata(db_path: Path) -> list[str]:
    with connect(db_path) as conn:
        return [
            str(row[0])
            for row in conn.execute(
                """
                select spotify_uri from discovery_tracks
                where release_id is null or primary_artist_id is null
                order by spotify_track_id
                """
            ).fetchall()
        ]


def list_release_ids_missing_labels(db_path: Path) -> list[str]:
    with connect(db_path) as conn:
        return [
            str(row[0])
            for row in conn.execute(
                """
                select spotify_release_id from discovery_releases
                where label_id is null or raw_label_name is null
                order by spotify_release_id
                """
            ).fetchall()
        ]


def mark_followed_labels(
    db_path: Path, labels: list[str], aliases: dict[str, str]
) -> int:
    now = _now()
    applied = 0
    with connect(db_path) as conn:
        for raw_name in labels:
            normalized = normalize_label_name(raw_name, aliases)
            applied += normalized.alias_applied
            label_id = _upsert_label(
                conn,
                normalized_name=normalized.normalized_name,
                display_name=normalized.display_name,
                now=now,
            )
            conn.execute(
                "update discovery_labels set followed = 1, updated_at = ? where id = ?",
                (now, label_id),
            )
    return applied


def upsert_release_metadata(
    db_path: Path,
    releases: list[tuple[str, str, str | None, str | None]],
    *,
    label_aliases: dict[str, str],
) -> tuple[int, int]:
    linked_labels = 0
    aliases_applied = 0
    now = _now()
    with connect(db_path) as conn:
        for release_id, title, release_date, raw_label in releases:
            label_id = None
            if raw_label:
                normalized = normalize_label_name(raw_label, label_aliases)
                aliases_applied += normalized.alias_applied
                label_id = _upsert_label(
                    conn,
                    normalized_name=normalized.normalized_name,
                    display_name=normalized.display_name,
                    now=now,
                )
                linked_labels += 1
            conn.execute(
                """
                insert into discovery_releases (
                    spotify_release_id, title, release_date, raw_label_name,
                    label_id, created_at, updated_at
                ) values (?, ?, ?, ?, ?, ?, ?)
                on conflict(spotify_release_id) do update set
                    title = excluded.title,
                    release_date = coalesce(excluded.release_date, discovery_releases.release_date),
                    raw_label_name = coalesce(excluded.raw_label_name, discovery_releases.raw_label_name),
                    label_id = coalesce(excluded.label_id, discovery_releases.label_id),
                    updated_at = excluded.updated_at
                """,
                (release_id, title, release_date, raw_label, label_id, now, now),
            )
    return linked_labels, aliases_applied


def get_affinities(
    db_path: Path,
    entity_type: EntityType | None = None,
) -> list[AffinityStats]:
    with connect(db_path) as conn:
        if entity_type is None:
            rows = conn.execute(
                """
                select * from taste_affinities
                order by entity_type, smoothed_affinity desc, sample_size desc, entity_name
                """
            ).fetchall()
        else:
            rows = conn.execute(
                """
                select * from taste_affinities where entity_type = ?
                order by smoothed_affinity desc, sample_size desc, entity_name
                """,
                (entity_type,),
            ).fetchall()
    return [_affinity_from_row(row) for row in rows]


def find_affinity(
    db_path: Path,
    *,
    entity_type: EntityType,
    name: str,
) -> AffinityStats | None:
    normalized = normalize_entity_name(name)
    matches = [
        affinity
        for affinity in get_affinities(db_path, entity_type)
        if normalize_entity_name(affinity.entity_name) == normalized
        or affinity.entity_key == name
    ]
    return matches[0] if matches else None


def list_eligible_candidates(
    db_path: Path,
    *,
    label_filter: str | None = None,
    artist_filter: str | None = None,
) -> list[CandidateRecord]:
    clauses = ["c.state in ('new', 'skipped')", "t.local_track_path is null"]
    params: list[object] = []
    if label_filter:
        clauses.append("(lower(l.display_name) = lower(?) or l.normalized_name = ?)")
        params.extend(
            (label_filter.strip(), normalize_entity_name(label_filter.strip()))
        )
    if artist_filter:
        clauses.append("lower(a.name) = lower(?)")
        params.append(artist_filter.strip())
    with connect(db_path) as conn:
        rows = conn.execute(
            f"""
            select c.id as candidate_id, c.spotify_track_id, t.title,
                   t.primary_artist_id as artist_id,
                   coalesce(a.name, 'Unknown artist') as artist_name,
                   t.release_id, r.title as release_title, r.release_date,
                   l.id as label_id, l.display_name as label_name,
                   coalesce(l.followed, 0) as label_followed,
                   t.local_track_path, t.track_number,
                   c.discovery_source, c.state, c.presentation_count,
                   c.last_presented_at
            from discovery_candidates c
            join discovery_tracks t on t.spotify_track_id = c.spotify_track_id
            left join discovery_artists a
                on a.spotify_artist_id = t.primary_artist_id
            left join discovery_releases r on r.spotify_release_id = t.release_id
            left join discovery_labels l on l.id = r.label_id
            where {" and ".join(clauses)}
            order by c.id
            """,
            params,
        ).fetchall()
    return [candidate_from_row(row) for row in rows]


def get_candidate(db_path: Path, candidate_id: int) -> CandidateRecord | None:
    with connect(db_path) as conn:
        row = conn.execute(
            """
            select c.id as candidate_id, c.spotify_track_id, t.title,
                   t.primary_artist_id as artist_id,
                   coalesce(a.name, 'Unknown artist') as artist_name,
                   t.release_id, r.title as release_title, r.release_date,
                   l.id as label_id, l.display_name as label_name,
                   coalesce(l.followed, 0) as label_followed,
                   t.local_track_path, t.track_number,
                   c.discovery_source, c.state, c.presentation_count,
                   c.last_presented_at
            from discovery_candidates c
            join discovery_tracks t on t.spotify_track_id = c.spotify_track_id
            left join discovery_artists a on a.spotify_artist_id = t.primary_artist_id
            left join discovery_releases r on r.spotify_release_id = t.release_id
            left join discovery_labels l on l.id = r.label_id
            where c.id = ?
            """,
            (candidate_id,),
        ).fetchone()
    return candidate_from_row(row) if row is not None else None


def latest_snapshot_version(db_path: Path) -> int:
    with connect(db_path) as conn:
        row = conn.execute(
            "select max(version) from discovery_taste_snapshots"
        ).fetchone()
    return int(row[0]) if row is not None and row[0] is not None else 0


def discovery_counts(db_path: Path) -> dict[str, Any]:
    with connect(db_path) as conn:
        row = conn.execute(
            """
            select
                count(*) filter (
                    where c.state in ('new', 'skipped') and t.local_track_path is null
                ) as total_eligible_candidates,
                count(*) filter (
                    where c.state in ('new', 'skipped') and t.local_track_path is null
                ) as unreviewed_candidates,
                count(*) filter (where state = 'kept') as kept,
                count(*) filter (where state = 'maybe') as maybe,
                count(*) filter (where state = 'passed') as passed
            from discovery_candidates c
            join discovery_tracks t on t.spotify_track_id = c.spotify_track_id
            """
        ).fetchone()
        signals = conn.execute(
            """
            select
                count(distinct spotify_track_id) filter (where signal_value > 0)
                    as positive_taste_tracks,
                count(distinct spotify_track_id) filter (where signal_value < 0)
                    as negative_taste_tracks
            from taste_signals
            """
        ).fetchone()
        completed = conn.execute(
            "select count(*) from discovery_sessions where status = 'completed'"
        ).fetchone()[0]
        unknown = conn.execute(
            "select count(*) from discovery_labels where followed = 0 and interest_reason is not null"
        ).fetchone()[0]
        promoted = conn.execute(
            "select count(*) from discovery_labels where followed = 0 and promoted_at is not null"
        ).fetchone()[0]
        through_keep = conn.execute(
            """
            select count(distinct l.id)
            from discovery_labels l
            join discovery_releases r on r.label_id = l.id
            join discovery_tracks t on t.release_id = r.spotify_release_id
            join discovery_candidates c on c.spotify_track_id = t.spotify_track_id
            where l.followed = 0 and c.state = 'kept'
            """
        ).fetchone()[0]
        neutral = conn.execute(
            """
            select count(*) from discovery_tracks t
            where not exists (
                select 1 from taste_signals s
                where s.spotify_track_id = t.spotify_track_id
            )
            """
        ).fetchone()[0]
        bucket_decisions: dict[str, dict[str, int]] = {}
        for bucket, decision, count in conn.execute(
            """
            select bucket, coalesce(decision, 'pending'), count(*)
            from discovery_session_items
            group by bucket, coalesce(decision, 'pending')
            order by bucket, decision
            """
        ):
            bucket_decisions.setdefault(str(bucket), {})[str(decision)] = int(count)
        bucket_rates: dict[str, dict[str, float]] = {}
        for bucket, decisions in bucket_decisions.items():
            decided_total = sum(
                count for decision, count in decisions.items() if decision != "pending"
            )
            bucket_rates[bucket] = {
                decision: round(count / decided_total, 4)
                for decision, count in decisions.items()
                if decision != "pending" and decided_total
            }
        affinity_statistics: dict[str, dict[str, object]] = {}
        for entity_type in ("artist", "label", "source"):
            affinity_rows = conn.execute(
                """
                select entity_name, smoothed_affinity, sample_size
                from taste_affinities
                where entity_type = ?
                order by (sample_size > 0) desc, smoothed_affinity desc,
                         sample_size desc, lower(entity_name)
                """,
                (entity_type,),
            ).fetchall()
            affinity_statistics[entity_type] = {
                "entities": len(affinity_rows),
                "with_evidence": sum(
                    int(item["sample_size"]) > 0 for item in affinity_rows
                ),
                "top": [
                    {
                        "entity": str(item["entity_name"]),
                        "smoothed_affinity": float(item["smoothed_affinity"]),
                        "sample_size": int(item["sample_size"]),
                    }
                    for item in affinity_rows
                    if int(item["sample_size"]) > 0
                ][:5],
            }
    result = dict(row) if row is not None else {}
    if signals is not None:
        result.update(dict(signals))
    result.update(
        completed_sessions=int(completed),
        unknown_labels_discovered=int(unknown),
        unknown_labels_promoted=int(promoted),
        labels_discovered_through_kept_tracks=int(through_keep),
        neutral_indexed_tracks=int(neutral),
        decision_counts_by_bucket=bucket_decisions,
        decision_rates_by_bucket=bucket_rates,
        artist_affinity_statistics=affinity_statistics["artist"],
        label_affinity_statistics=affinity_statistics["label"],
        source_affinity_statistics=affinity_statistics["source"],
    )
    return result


def catalogue_linkage_counts(db_path: Path) -> dict[str, int]:
    with connect(db_path) as conn:
        row = conn.execute(
            """
            select
                count(*) as tracks_inspected,
                count(spotify_uri) as tracks_with_spotify_ids,
                count(release_id) as tracks_linked_to_releases,
                count(r.label_id) as tracks_linked_to_labels
            from discovery_tracks t
            left join discovery_releases r on r.spotify_release_id = t.release_id
            """
        ).fetchone()
    counts = {key: int(value) for key, value in dict(row).items()}
    counts["missing_metadata"] = (
        counts["tracks_inspected"] - counts["tracks_linked_to_labels"]
    )
    return counts


def _upsert_label(
    conn: sqlite3.Connection,
    *,
    normalized_name: str,
    display_name: str,
    now: str,
) -> int:
    conn.execute(
        """
        insert into discovery_labels (
            normalized_name, display_name, created_at, updated_at
        ) values (?, ?, ?, ?)
        on conflict(normalized_name) do update set
            updated_at = excluded.updated_at
        """,
        (normalized_name, display_name, now, now),
    )
    row = conn.execute(
        "select id from discovery_labels where normalized_name = ?",
        (normalized_name,),
    ).fetchone()
    if row is None:
        raise sqlite3.DatabaseError("Could not resolve normalized label")
    return int(row[0])


def candidate_from_row(row: sqlite3.Row) -> CandidateRecord:
    return CandidateRecord(
        candidate_id=int(row["candidate_id"]),
        spotify_track_id=str(row["spotify_track_id"]),
        title=str(row["title"]),
        artist_id=row["artist_id"],
        artist_name=str(row["artist_name"]),
        release_id=row["release_id"],
        release_title=row["release_title"],
        release_date=row["release_date"],
        label_id=row["label_id"],
        label_name=row["label_name"],
        label_followed=bool(row["label_followed"]),
        local_track_path=row["local_track_path"],
        track_number=row["track_number"],
        source=cast(DiscoverySource, row["discovery_source"]),
        state=cast(CandidateState, row["state"]),
        presentation_count=int(row["presentation_count"]),
        last_presented_at=row["last_presented_at"],
    )


def _affinity_from_row(row: sqlite3.Row) -> AffinityStats:
    return AffinityStats(
        entity_type=cast(EntityType, row["entity_type"]),
        entity_key=str(row["entity_key"]),
        entity_name=str(row["entity_name"]),
        positive_evidence_count=int(row["positive_evidence_count"]),
        negative_evidence_count=int(row["negative_evidence_count"]),
        neutral_indexed_count=int(row["neutral_indexed_count"]),
        weighted_positive_score=float(row["weighted_positive_score"]),
        weighted_negative_score=float(row["weighted_negative_score"]),
        smoothed_affinity=float(row["smoothed_affinity"]),
        sample_size=int(row["sample_size"]),
        confidence=float(row["confidence"]),
        top_contributing_signals=tuple(
            (str(item[0]), int(item[1]))
            for item in json.loads(row["top_contributing_signals"])
        ),
        snapshot_version=int(row["snapshot_version"]),
    )


def now_iso() -> str:
    return _now()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
