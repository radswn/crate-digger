import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any, cast

from crate_digger.discover.models import (
    CandidateBucket,
    Decision,
    DiscoverySession,
    DiscoverySessionItem,
    SessionBuildResult,
    SessionMode,
)
from crate_digger.discover.ranking import (
    build_diverse_selection,
    rank_candidates,
    select_release_probes,
)
from crate_digger.discover.repository import (
    candidate_from_row,
    connect,
    get_candidate,
    latest_snapshot_version,
    list_eligible_candidates,
    now_iso,
)
from crate_digger.discover.taste import rebuild_taste_index


def build_session(
    db_path: Path,
    *,
    mode: SessionMode = "balanced",
    size: int = 30,
    seed: int = 0,
    freshness_days: int = 90,
    label_filter: str | None = None,
    artist_filter: str | None = None,
) -> SessionBuildResult:
    if mode not in {"balanced", "fresh", "deep-dig", "frontier"}:
        raise ValueError(f"Invalid discovery mode: {mode}")
    if not 1 <= size <= 100:
        raise ValueError("Session size must be between 1 and 100")
    snapshot_version = latest_snapshot_version(db_path)
    if snapshot_version == 0:
        snapshot_version = rebuild_taste_index(db_path).snapshot_version
    candidates = list_eligible_candidates(
        db_path, label_filter=label_filter, artist_filter=artist_filter
    )
    ranked = rank_candidates(
        db_path,
        candidates,
        mode=mode,
        seed=seed,
        freshness_days=freshness_days,
    )
    selected, shortages = build_diverse_selection(ranked, mode=mode, size=size)
    now = now_iso()
    with connect(db_path) as conn:
        cursor = conn.execute(
            """
            insert into discovery_sessions (
                mode, target_size, seed, taste_snapshot_version,
                label_filter, artist_filter, created_at, status, shortages
            ) values (?, ?, ?, ?, ?, ?, ?, 'open', ?)
            """,
            (
                mode,
                size,
                seed,
                snapshot_version,
                label_filter,
                artist_filter,
                now,
                json.dumps(shortages, sort_keys=True),
            ),
        )
        if cursor.lastrowid is None:
            raise sqlite3.DatabaseError("Could not create discovery session")
        session_id = int(cursor.lastrowid)
        for position, item in enumerate(selected, start=1):
            conn.execute(
                """
                insert into discovery_session_items (
                    session_id, candidate_id, position, bucket,
                    score_at_selection, affinity_at_selection,
                    reasons_at_selection
                ) values (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    item.candidate.candidate_id,
                    position,
                    item.bucket,
                    item.score,
                    json.dumps(item.affinity_snapshot, sort_keys=True),
                    json.dumps(item.reasons),
                ),
            )
            conn.execute(
                """
                update discovery_candidates
                set state = 'queued',
                    score = ?,
                    bucket = ?,
                    presentation_count = presentation_count + 1,
                    first_presented_at = coalesce(first_presented_at, ?),
                    last_presented_at = ?,
                    updated_at = ?
                where id = ?
                """,
                (
                    item.score,
                    item.bucket,
                    now,
                    now,
                    now,
                    item.candidate.candidate_id,
                ),
            )
    result = get_session(db_path, session_id)
    if result is None:
        raise sqlite3.DatabaseError("Created discovery session could not be loaded")
    items = list_session_items(db_path, session_id)
    bucket_counts = dict(Counter(item.bucket for item in items))
    high_affinity_set: set[str] = set()
    for item in items:
        for raw_entity in item.affinity_at_selection.values():
            if not isinstance(raw_entity, dict):
                continue
            entity = cast(dict[str, object], raw_entity)
            affinity = entity.get("smoothed_affinity")
            name = entity.get("entity_name")
            if isinstance(affinity, int | float) and affinity > 0.55 and name:
                high_affinity_set.add(str(name))
    high_affinity = sorted(high_affinity_set)
    underexplored = sorted(
        {
            item.track.label_name
            for item in items
            if item.track.label_name
            and any(
                "Underexplored label" in reason for reason in item.reasons_at_selection
            )
        }
    )
    return SessionBuildResult(
        session=result,
        items=tuple(items),
        bucket_counts=bucket_counts,
        high_affinity_entities=tuple(high_affinity),
        underexplored_labels=tuple(underexplored),
    )


def list_sessions(db_path: Path) -> list[DiscoverySession]:
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            select s.*, count(i.id) as actual_size
            from discovery_sessions s
            left join discovery_session_items i on i.session_id = s.id
            group by s.id order by s.id desc
            """
        ).fetchall()
    return [_session_from_row(row) for row in rows]


def get_session(db_path: Path, session_id: int) -> DiscoverySession | None:
    with connect(db_path) as conn:
        row = conn.execute(
            """
            select s.*, count(i.id) as actual_size
            from discovery_sessions s
            left join discovery_session_items i on i.session_id = s.id
            where s.id = ? group by s.id
            """,
            (session_id,),
        ).fetchone()
    return _session_from_row(row) if row is not None else None


def latest_open_session(db_path: Path) -> DiscoverySession | None:
    sessions = list_sessions(db_path)
    return next((session for session in sessions if session.status == "open"), None)


def list_session_items(db_path: Path, session_id: int) -> list[DiscoverySessionItem]:
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            select * from discovery_session_items
            where session_id = ? order by position
            """,
            (session_id,),
        ).fetchall()
    items: list[DiscoverySessionItem] = []
    for row in rows:
        candidate = get_candidate(db_path, int(row["candidate_id"]))
        if candidate is None:
            continue
        items.append(
            DiscoverySessionItem(
                item_id=int(row["id"]),
                session_id=int(row["session_id"]),
                candidate_id=int(row["candidate_id"]),
                position=int(row["position"]),
                bucket=cast(CandidateBucket, row["bucket"]),
                score_at_selection=float(row["score_at_selection"]),
                affinity_at_selection=json.loads(row["affinity_at_selection"]),
                reasons_at_selection=tuple(json.loads(row["reasons_at_selection"])),
                decision=cast(Decision | None, row["decision"]),
                decided_at=row["decided_at"],
                track=candidate,
            )
        )
    return items


def get_session_item(
    db_path: Path, session_id: int, item_id: int
) -> DiscoverySessionItem | None:
    return next(
        (
            item
            for item in list_session_items(db_path, session_id)
            if item.item_id == item_id
        ),
        None,
    )


def record_feedback(
    db_path: Path,
    *,
    session_id: int,
    item_id: int,
    decision: Decision,
) -> DiscoverySessionItem:
    if decision not in {"keep", "maybe", "pass", "skip"}:
        raise ValueError(f"Invalid feedback decision: {decision}")
    item = get_session_item(db_path, session_id, item_id)
    if item is None:
        raise KeyError(f"Discovery session item not found: {item_id}")
    if item.decision == decision:
        return item
    candidate_state = {
        "keep": "kept",
        "maybe": "maybe",
        "pass": "passed",
        "skip": "skipped",
    }[decision]
    now = now_iso()
    with connect(db_path) as conn:
        conn.execute(
            """
            update discovery_session_items
            set decision = ?, decided_at = ?
            where id = ? and session_id = ?
            """,
            (decision, now, item_id, session_id),
        )
        conn.execute(
            """
            update discovery_candidates
            set state = ?, feedback = ?, updated_at = ? where id = ?
            """,
            (candidate_state, decision, now, item.candidate_id),
        )
        if decision == "keep" and item.track.label_id is not None:
            conn.execute(
                """
                update discovery_labels
                set interest_reason = coalesce(
                        interest_reason, 'discovered through kept track'
                    ),
                    promoted_at = case when followed = 0 then ? else promoted_at end,
                    updated_at = ?
                where id = ?
                """,
                (now, now, item.track.label_id),
            )
        undecided = conn.execute(
            """
            select count(*) from discovery_session_items
            where session_id = ? and decision is null
            """,
            (session_id,),
        ).fetchone()[0]
        if int(undecided) == 0:
            conn.execute(
                """
                update discovery_sessions
                set status = 'completed', completed_at = ? where id = ?
                """,
                (now, session_id),
            )
    rebuild_taste_index(db_path)
    updated = get_session_item(db_path, session_id, item_id)
    if updated is None:
        raise sqlite3.DatabaseError("Updated session item could not be loaded")
    return updated


def expand_release(db_path: Path, *, session_id: int, item_id: int) -> list[int]:
    item = get_session_item(db_path, session_id, item_id)
    if item is None:
        raise KeyError(f"Discovery session item not found: {item_id}")
    release_id = item.track.release_id
    if release_id is None:
        raise ValueError("This track has no linked release to expand")
    now = now_iso()
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            select c.id from discovery_candidates c
            join discovery_tracks t on t.spotify_track_id = c.spotify_track_id
            where t.release_id = ? and c.id != ?
              and t.local_track_path is null
              and c.state in ('new', 'skipped')
            order by coalesce(t.track_number, 1000000), lower(t.title), c.id
            """,
            (release_id, item.candidate_id),
        ).fetchall()
        candidate_ids = [int(row[0]) for row in rows]
        for candidate_id in candidate_ids:
            conn.execute(
                """
                update discovery_candidates
                set discovery_source = 'release_expansion',
                    provenance = ?, state = 'new', updated_at = ?
                where id = ?
                """,
                (
                    json.dumps(
                        {
                            "expanded_from_session": session_id,
                            "expanded_from_item": item_id,
                            "release_id": release_id,
                        },
                        sort_keys=True,
                    ),
                    now,
                    candidate_id,
                ),
            )
        conn.execute(
            """
            update discovery_candidates
            set release_expanded = 1, updated_at = ? where id = ?
            """,
            (now, item.candidate_id),
        )
    return candidate_ids


def explore_label(
    db_path: Path,
    *,
    session_id: int,
    item_id: int,
    max_releases: int = 5,
) -> list[int]:
    item = get_session_item(db_path, session_id, item_id)
    if item is None:
        raise KeyError(f"Discovery session item not found: {item_id}")
    label_id = item.track.label_id
    if label_id is None:
        raise ValueError("This track has no linked label to explore")
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            select c.id as candidate_id, c.spotify_track_id, t.title,
                   t.primary_artist_id as artist_id,
                   coalesce(a.name, 'Unknown artist') as artist_name,
                   t.release_id, r.title as release_title, r.release_date,
                   l.id as label_id, l.display_name as label_name,
                   l.followed as label_followed, t.local_track_path,
                   t.track_number, c.discovery_source, c.state,
                   c.presentation_count, c.last_presented_at
            from discovery_candidates c
            join discovery_tracks t on t.spotify_track_id = c.spotify_track_id
            join discovery_releases r on r.spotify_release_id = t.release_id
            join discovery_labels l on l.id = r.label_id
            left join discovery_artists a on a.spotify_artist_id = t.primary_artist_id
            where l.id = ? and c.id != ? and t.local_track_path is null
              and c.state in ('new', 'skipped')
            order by r.release_date desc, r.spotify_release_id, t.track_number, c.id
            """,
            (label_id, item.candidate_id),
        ).fetchall()
    candidates = [candidate_from_row(row) for row in rows]
    probes = select_release_probes(candidates)
    chosen = _spread_release_periods(probes, max_releases=max_releases)
    now = now_iso()
    with connect(db_path) as conn:
        for candidate in chosen:
            conn.execute(
                """
                update discovery_candidates
                set discovery_source = 'label_expansion', provenance = ?,
                    state = 'new', updated_at = ? where id = ?
                """,
                (
                    json.dumps(
                        {
                            "explored_from_session": session_id,
                            "explored_from_item": item_id,
                            "label_id": label_id,
                        },
                        sort_keys=True,
                    ),
                    now,
                    candidate.candidate_id,
                ),
            )
        conn.execute(
            """
            update discovery_candidates
            set label_expanded = 1, updated_at = ? where id = ?
            """,
            (now, item.candidate_id),
        )
        conn.execute(
            """
            update discovery_labels
            set explored_at = ?, interest_reason = coalesce(
                    interest_reason, 'explicitly explored from discovery session'
                ), updated_at = ? where id = ?
            """,
            (now, now, label_id),
        )
        conn.execute(
            """
            insert into discovery_label_explorations (
                label_id, source_session_item_id, reason, created_at
            ) values (?, ?, ?, ?)
            on conflict(label_id, source_session_item_id) do nothing
            """,
            (label_id, item_id, "Explicit Explore label action", now),
        )
    return [candidate.candidate_id for candidate in chosen]


def item_media(db_path: Path, item_id: int) -> dict[str, str | None] | None:
    with connect(db_path) as conn:
        row = conn.execute(
            """
            select t.local_track_path, t.preview_url, t.external_url, t.spotify_uri
            from discovery_session_items i
            join discovery_candidates c on c.id = i.candidate_id
            join discovery_tracks t on t.spotify_track_id = c.spotify_track_id
            where i.id = ?
            """,
            (item_id,),
        ).fetchone()
    return dict(row) if row is not None else None


def _session_from_row(row: sqlite3.Row) -> DiscoverySession:
    return DiscoverySession(
        session_id=int(row["id"]),
        mode=cast(SessionMode, row["mode"]),
        target_size=int(row["target_size"]),
        actual_size=int(row["actual_size"]),
        seed=int(row["seed"]),
        taste_snapshot_version=int(row["taste_snapshot_version"]),
        status=str(row["status"]),
        created_at=str(row["created_at"]),
        completed_at=row["completed_at"],
        shortages={
            str(key): int(value) for key, value in json.loads(row["shortages"]).items()
        },
    )


def _spread_release_periods(candidates: list[Any], *, max_releases: int) -> list[Any]:
    by_period: dict[str, list[Any]] = {}
    for candidate in candidates:
        year = (candidate.release_date or "unknown")[:4]
        period = year if year.isdigit() else "unknown"
        by_period.setdefault(period, []).append(candidate)
    selected: list[Any] = []
    periods = sorted(by_period, reverse=True)
    while len(selected) < max_releases and periods:
        next_periods: list[str] = []
        for period in periods:
            if by_period[period]:
                selected.append(by_period[period].pop(0))
                if len(selected) == max_releases:
                    break
            if by_period[period]:
                next_periods.append(period)
        periods = next_periods
    return selected
