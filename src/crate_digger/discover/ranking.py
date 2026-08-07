import hashlib
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

from crate_digger.discover.models import (
    AffinityStats,
    CandidateBucket,
    CandidateRecord,
    ScoredCandidate,
    SessionMode,
)
from crate_digger.discover.repository import connect, get_affinities
from crate_digger.utils.spotify import normalize_title


MODE_ALLOCATIONS: dict[SessionMode, dict[CandidateBucket, float]] = {
    "balanced": {
        "fresh": 0.45,
        "taste-adjacent": 0.30,
        "archive": 0.20,
        "wildcard": 0.05,
    },
    "fresh": {
        "fresh": 0.70,
        "taste-adjacent": 0.20,
        "archive": 0.05,
        "wildcard": 0.05,
    },
    "deep-dig": {
        "fresh": 0.10,
        "taste-adjacent": 0.30,
        "archive": 0.50,
        "wildcard": 0.10,
    },
    "frontier": {
        "fresh": 0.15,
        "taste-adjacent": 0.30,
        "archive": 0.25,
        "wildcard": 0.30,
    },
}
MAX_PRIMARY_ARTIST = 2
MAX_LABEL = 3
MAX_INITIAL_RELEASE = 1
ADJACENT_AFFINITY = 0.55
RECENT_PRESENTATION_DAYS = 30
BAD_PROBE_WORDS = ("intro", "outro", "interlude", "radio edit", "duplicate mix")


def rank_candidates(
    db_path: Path,
    candidates: list[CandidateRecord],
    *,
    mode: SessionMode,
    seed: int,
    freshness_days: int,
    today: date | None = None,
) -> list[ScoredCandidate]:
    today = today or date.today()
    artist_affinities = {
        affinity.entity_key: affinity for affinity in get_affinities(db_path, "artist")
    }
    label_affinities = {
        affinity.entity_key: affinity for affinity in get_affinities(db_path, "label")
    }
    overlap = _positive_artist_overlap_by_label(db_path, artist_affinities)
    probes = select_release_probes(candidates)
    return [
        _score_candidate(
            candidate,
            mode=mode,
            seed=seed,
            freshness_days=freshness_days,
            today=today,
            artist_affinity=artist_affinities.get(candidate.artist_id or ""),
            label_affinity=label_affinities.get(str(candidate.label_id or "")),
            positive_artist_overlap=overlap.get(candidate.label_id or -1, 0),
        )
        for candidate in probes
    ]


def select_release_probes(candidates: list[CandidateRecord]) -> list[CandidateRecord]:
    by_release: dict[str, list[CandidateRecord]] = defaultdict(list)
    for candidate in candidates:
        key = candidate.release_id or f"track:{candidate.spotify_track_id}"
        by_release[key].append(candidate)
    selected: list[CandidateRecord] = []
    for release_id in sorted(by_release):
        group = by_release[release_id]
        expanded = [item for item in group if item.source == "release_expansion"]
        if expanded:
            selected.extend(sorted(expanded, key=_track_order))
            continue
        selected.append(_representative_probe(group))
    return selected


def build_diverse_selection(
    ranked: list[ScoredCandidate],
    *,
    mode: SessionMode,
    size: int,
) -> tuple[list[ScoredCandidate], dict[str, int]]:
    targets = bucket_targets(
        mode, size, wildcard_available=any(item.bucket == "wildcard" for item in ranked)
    )
    pools: dict[CandidateBucket, list[ScoredCandidate]] = {
        bucket: sorted(
            (item for item in ranked if item.bucket == bucket),
            key=lambda item: (-item.score, item.tie_breaker),
        )
        for bucket in MODE_ALLOCATIONS[mode]
    }
    selected: list[ScoredCandidate] = []
    artist_counts: Counter[str] = Counter()
    label_counts: Counter[int] = Counter()
    release_counts: Counter[str] = Counter()
    archive_periods: Counter[str] = Counter()
    used_ids: set[int] = set()

    combined = sorted(ranked, key=lambda item: (-item.score, item.tie_breaker))
    wildcard_pool = [item for item in combined if item.bucket == "wildcard"]
    underexplored_pool = [
        item
        for item in combined
        if item.candidate.label_id is not None and "label" not in item.affinity_snapshot
    ]
    for reserved_pool in (wildcard_pool, underexplored_pool):
        if len(selected) >= size or not reserved_pool:
            continue
        if reserved_pool is underexplored_pool and any(
            item.candidate.label_id is not None
            and "label" not in item.affinity_snapshot
            for item in selected
        ):
            continue
        choice = _choose_candidate(
            reserved_pool,
            selected=selected,
            used_ids=used_ids,
            artist_counts=artist_counts,
            label_counts=label_counts,
            release_counts=release_counts,
            archive_periods=archive_periods,
            relax_repetition=False,
        )
        if choice is not None:
            _record_choice(
                choice,
                selected,
                used_ids,
                artist_counts,
                label_counts,
                release_counts,
                archive_periods,
            )

    remaining = dict(targets)
    for choice in selected:
        if remaining[choice.bucket] > 0:
            remaining[choice.bucket] -= 1
            continue
        donor = max(remaining, key=lambda bucket: remaining[bucket])
        if remaining[donor] > 0:
            remaining[donor] -= 1
    plan: list[CandidateBucket] = []
    while sum(remaining.values()) > 0:
        for bucket in MODE_ALLOCATIONS[mode]:
            if remaining[bucket] > 0:
                plan.append(bucket)
                remaining[bucket] -= 1

    for bucket in plan:
        choice = _choose_candidate(
            pools[bucket],
            selected=selected,
            used_ids=used_ids,
            artist_counts=artist_counts,
            label_counts=label_counts,
            release_counts=release_counts,
            archive_periods=archive_periods,
            relax_repetition=False,
        )
        if choice is not None:
            _record_choice(
                choice,
                selected,
                used_ids,
                artist_counts,
                label_counts,
                release_counts,
                archive_periods,
            )

    while len(selected) < size:
        choice = _choose_candidate(
            combined,
            selected=selected,
            used_ids=used_ids,
            artist_counts=artist_counts,
            label_counts=label_counts,
            release_counts=release_counts,
            archive_periods=archive_periods,
            relax_repetition=False,
        )
        if choice is None:
            choice = _choose_candidate(
                combined,
                selected=selected,
                used_ids=used_ids,
                artist_counts=artist_counts,
                label_counts=label_counts,
                release_counts=release_counts,
                archive_periods=archive_periods,
                relax_repetition=True,
            )
        if choice is None:
            break
        _record_choice(
            choice,
            selected,
            used_ids,
            artist_counts,
            label_counts,
            release_counts,
            archive_periods,
        )

    actual = Counter(item.bucket for item in selected)
    shortages = {
        bucket: max(0, target - actual[bucket])
        for bucket, target in targets.items()
        if target > actual[bucket]
    }
    if len(selected) < size:
        shortages["total"] = size - len(selected)
    return selected, shortages


def bucket_targets(
    mode: SessionMode, size: int, *, wildcard_available: bool
) -> dict[CandidateBucket, int]:
    allocation = MODE_ALLOCATIONS[mode]
    raw: dict[CandidateBucket, float] = {
        bucket: size * share for bucket, share in allocation.items()
    }
    targets: dict[CandidateBucket, int] = {
        bucket: int(value) for bucket, value in raw.items()
    }
    remaining = size - sum(targets.values())
    for bucket in sorted(raw, key=lambda item: (-(raw[item] - targets[item]), item)):
        if remaining == 0:
            break
        targets[bucket] += 1
        remaining -= 1
    if wildcard_available and size > 0 and targets["wildcard"] == 0:
        donor = max(
            (bucket for bucket in targets if bucket != "wildcard"),
            key=lambda item: targets[item],
        )
        if targets[donor] > 0:
            targets[donor] -= 1
            targets["wildcard"] = 1
    return targets


def _score_candidate(
    candidate: CandidateRecord,
    *,
    mode: SessionMode,
    seed: int,
    freshness_days: int,
    today: date,
    artist_affinity: AffinityStats | None,
    label_affinity: AffinityStats | None,
    positive_artist_overlap: int,
) -> ScoredCandidate:
    score = 50.0
    reasons: list[str] = []
    affinity_snapshot: dict[str, object] = {}
    if artist_affinity and artist_affinity.sample_size:
        component = (artist_affinity.smoothed_affinity - 0.5) * 30
        score += component
        affinity_snapshot["artist"] = _affinity_snapshot(artist_affinity)
        if artist_affinity.positive_evidence_count:
            reasons.append(
                f"Artist appears in {artist_affinity.positive_evidence_count} positive taste tracks"
            )
    if label_affinity and label_affinity.sample_size:
        component = (label_affinity.smoothed_affinity - 0.5) * 25
        score += component
        affinity_snapshot["label"] = _affinity_snapshot(label_affinity)
        reasons.append(
            f"Label has {label_affinity.smoothed_affinity:.0%} smoothed affinity from {label_affinity.sample_size} reviewed tracks"
        )

    release_age = _release_age_days(candidate.release_date, today)
    is_fresh = release_age is not None and release_age <= freshness_days
    underexplored = label_affinity is None or label_affinity.sample_size == 0
    adjacent = (
        (
            artist_affinity is not None
            and artist_affinity.sample_size > 0
            and artist_affinity.smoothed_affinity > ADJACENT_AFFINITY
        )
        or (
            label_affinity is not None
            and label_affinity.sample_size > 0
            and label_affinity.smoothed_affinity > ADJACENT_AFFINITY
        )
        or positive_artist_overlap >= 2
        or candidate.source
        in {"taste_adjacent", "release_expansion", "label_expansion"}
    )
    if is_fresh:
        bucket: CandidateBucket = "fresh"
        score += 10
        reasons.append(f"Released within the {freshness_days}-day freshness window")
    elif mode == "frontier" and not candidate.label_followed and underexplored:
        bucket = "wildcard"
    elif adjacent:
        bucket = "taste-adjacent"
    elif release_age is not None and release_age > freshness_days:
        bucket = "archive"
        score += 3
        reasons.append("Archive probe not previously reviewed")
    else:
        bucket = "wildcard"

    if candidate.presentation_count == 0:
        score += 8
        reasons.append("Never presented before")
    else:
        score -= 4 * candidate.presentation_count
        reasons.append(f"Previously presented {candidate.presentation_count} time(s)")
    if _recently_presented(candidate.last_presented_at, today):
        score -= 8

    if positive_artist_overlap:
        score += min(positive_artist_overlap, 4) * 3
        reasons.append(
            f"Label contains {positive_artist_overlap} artist(s) represented in positive taste evidence"
        )
    if not candidate.label_followed and underexplored:
        score += 5
        reasons.append("Underexplored label selected for controlled novelty")
    if candidate.source in {"release_expansion", "label_expansion"}:
        score += 5
        reasons.append(candidate.source.replace("_", " ").capitalize())
    elif candidate.source == "followed_label":
        score += 2
        reasons.append("Track came from a followed-label catalogue")
    if candidate.release_id and candidate.source != "release_expansion":
        reasons.append("Selected as the initial probe for this release")
    reasons.append(f"Selected for the {bucket} bucket")
    tie = hashlib.sha256(f"{seed}:{candidate.spotify_track_id}".encode()).hexdigest()
    return ScoredCandidate(
        candidate=candidate,
        bucket=bucket,
        score=round(score, 3),
        affinity_snapshot=affinity_snapshot,
        reasons=tuple(reasons),
        tie_breaker=tie,
    )


def _representative_probe(group: list[CandidateRecord]) -> CandidateRecord:
    ordered = sorted(group, key=_track_order)
    if len(ordered) == 1:
        return ordered[0]
    release_title = normalize_title(ordered[0].release_title or "")
    if release_title:
        matching = [
            item
            for item in ordered
            if normalize_title(item.title) == release_title
            or normalize_title(item.title).startswith(release_title)
        ]
        if matching:
            return matching[0]
    normal = [
        item
        for item in ordered
        if not any(word in normalize_title(item.title) for word in BAD_PROBE_WORDS)
    ]
    return normal[0] if normal else ordered[0]


def _choose_candidate(
    pool: list[ScoredCandidate],
    *,
    selected: list[ScoredCandidate],
    used_ids: set[int],
    artist_counts: Counter[str],
    label_counts: Counter[int],
    release_counts: Counter[str],
    archive_periods: Counter[str],
    relax_repetition: bool,
) -> ScoredCandidate | None:
    eligible: list[ScoredCandidate] = []
    for item in pool:
        candidate = item.candidate
        if candidate.candidate_id in used_ids:
            continue
        if not relax_repetition:
            if (
                candidate.artist_id
                and artist_counts[candidate.artist_id] >= MAX_PRIMARY_ARTIST
            ):
                continue
            if candidate.label_id and label_counts[candidate.label_id] >= MAX_LABEL:
                continue
        if (
            candidate.release_id
            and candidate.source != "release_expansion"
            and release_counts[candidate.release_id] >= MAX_INITIAL_RELEASE
        ):
            continue
        eligible.append(item)
    if not eligible:
        return None
    if selected and selected[-1].candidate.label_id is not None:
        different_label = [
            item
            for item in eligible
            if item.candidate.label_id != selected[-1].candidate.label_id
        ]
        if different_label:
            eligible = different_label
    archive_choices = [item for item in eligible if item.bucket == "archive"]
    if archive_choices:
        unused_period = [
            item
            for item in archive_choices
            if archive_periods[_release_period(item.candidate.release_date)] == 0
        ]
        if unused_period:
            eligible = unused_period
    return min(eligible, key=lambda item: (-item.score, item.tie_breaker))


def _record_choice(
    choice: ScoredCandidate,
    selected: list[ScoredCandidate],
    used_ids: set[int],
    artist_counts: Counter[str],
    label_counts: Counter[int],
    release_counts: Counter[str],
    archive_periods: Counter[str],
) -> None:
    selected.append(choice)
    candidate = choice.candidate
    used_ids.add(candidate.candidate_id)
    if candidate.artist_id:
        artist_counts[candidate.artist_id] += 1
    if candidate.label_id:
        label_counts[candidate.label_id] += 1
    if candidate.release_id:
        release_counts[candidate.release_id] += 1
    if choice.bucket == "archive":
        archive_periods[_release_period(candidate.release_date)] += 1


def _positive_artist_overlap_by_label(
    db_path: Path, artist_affinities: dict[str, AffinityStats]
) -> dict[int, int]:
    positive_artist_ids = {
        key
        for key, affinity in artist_affinities.items()
        if affinity.sample_size > 0 and affinity.smoothed_affinity > ADJACENT_AFFINITY
    }
    if not positive_artist_ids:
        return {}
    placeholders = ",".join("?" for _ in positive_artist_ids)
    with connect(db_path) as conn:
        rows = conn.execute(
            f"""
            select r.label_id, count(distinct ta.spotify_artist_id)
            from discovery_track_artists ta
            join discovery_tracks t on t.spotify_track_id = ta.spotify_track_id
            join discovery_releases r on r.spotify_release_id = t.release_id
            where r.label_id is not null
              and ta.spotify_artist_id in ({placeholders})
            group by r.label_id
            """,
            tuple(sorted(positive_artist_ids)),
        ).fetchall()
    return {int(row[0]): int(row[1]) for row in rows}


def _affinity_snapshot(affinity: AffinityStats) -> dict[str, object]:
    return {
        "entity_key": affinity.entity_key,
        "entity_name": affinity.entity_name,
        "smoothed_affinity": affinity.smoothed_affinity,
        "sample_size": affinity.sample_size,
        "positive_evidence_count": affinity.positive_evidence_count,
        "negative_evidence_count": affinity.negative_evidence_count,
    }


def _track_order(candidate: CandidateRecord) -> tuple[int, str, int]:
    return (
        candidate.track_number if candidate.track_number is not None else 1_000_000,
        normalize_title(candidate.title),
        candidate.candidate_id,
    )


def _release_age_days(value: str | None, today: date) -> int | None:
    if not value:
        return None
    try:
        if len(value) >= 10:
            released = date.fromisoformat(value[:10])
        elif len(value) >= 7:
            year, month = (int(part) for part in value[:7].split("-"))
            released = date(year, month, 1)
        elif len(value) >= 4:
            released = date(int(value[:4]), 1, 1)
        else:
            return None
    except ValueError:
        return None
    return max(0, (today - released).days)


def _recently_presented(value: str | None, today: date) -> bool:
    if not value:
        return False
    try:
        presented = datetime.fromisoformat(value).astimezone(timezone.utc).date()
    except ValueError:
        return False
    return (today - presented).days <= RECENT_PRESENTATION_DAYS


def _release_period(value: str | None) -> str:
    if not value or len(value) < 4 or not value[:4].isdigit():
        return "unknown"
    year = int(value[:4])
    return f"{year // 5 * 5}-{year // 5 * 5 + 4}"
