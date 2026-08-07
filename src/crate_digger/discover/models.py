from dataclasses import dataclass
from typing import Literal


DiscoverySource = Literal[
    "followed_label",
    "discovered_label",
    "followed_artist",
    "release_expansion",
    "label_expansion",
    "taste_adjacent",
    "manual",
]
CandidateState = Literal["new", "queued", "kept", "maybe", "passed", "skipped"]
SessionMode = Literal["balanced", "fresh", "deep-dig", "frontier"]
CandidateBucket = Literal["fresh", "taste-adjacent", "archive", "wildcard"]
Decision = Literal["keep", "maybe", "pass", "skip"]
EntityType = Literal["artist", "label", "tag", "source"]


@dataclass(frozen=True)
class SpotifyEntityTrack:
    spotify_track_id: str
    spotify_uri: str
    title: str
    artists: tuple[tuple[str, str], ...]
    spotify_release_id: str | None
    release_title: str | None
    release_date: str | None
    raw_label_name: str | None
    track_number: int | None = None
    disc_number: int | None = None
    duration_ms: int | None = None
    preview_url: str | None = None
    external_url: str | None = None
    local_track_path: str | None = None


@dataclass(frozen=True)
class IndexSummary:
    tracks_inspected: int = 0
    tracks_with_spotify_ids: int = 0
    tracks_linked_to_releases: int = 0
    tracks_linked_to_labels: int = 0
    created_candidates: int = 0
    already_indexed: int = 0
    skipped: int = 0
    missing_metadata: int = 0
    label_aliases_applied: int = 0


@dataclass(frozen=True)
class TasteSignal:
    spotify_track_id: str
    signal_type: str
    signal_value: float
    weight: float
    source: str
    metadata: dict[str, object]

    @property
    def weighted_value(self) -> float:
        return self.signal_value * self.weight


@dataclass(frozen=True)
class AffinityStats:
    entity_type: EntityType
    entity_key: str
    entity_name: str
    positive_evidence_count: int
    negative_evidence_count: int
    neutral_indexed_count: int
    weighted_positive_score: float
    weighted_negative_score: float
    smoothed_affinity: float
    sample_size: int
    confidence: float
    top_contributing_signals: tuple[tuple[str, int], ...]
    snapshot_version: int


@dataclass(frozen=True)
class TasteRebuildSummary:
    snapshot_version: int
    tracks_inspected: int
    positive_taste_tracks: int
    negative_taste_tracks: int
    neutral_catalogue_tracks: int
    artists_with_evidence: int
    labels_with_evidence: int
    tags_with_evidence: int
    sources_with_evidence: int


@dataclass(frozen=True)
class CandidateRecord:
    candidate_id: int
    spotify_track_id: str
    title: str
    artist_id: str | None
    artist_name: str
    release_id: str | None
    release_title: str | None
    release_date: str | None
    label_id: int | None
    label_name: str | None
    label_followed: bool
    local_track_path: str | None
    track_number: int | None
    source: DiscoverySource
    state: CandidateState
    presentation_count: int
    last_presented_at: str | None


@dataclass(frozen=True)
class ScoredCandidate:
    candidate: CandidateRecord
    bucket: CandidateBucket
    score: float
    affinity_snapshot: dict[str, object]
    reasons: tuple[str, ...]
    tie_breaker: str


@dataclass(frozen=True)
class DiscoverySession:
    session_id: int
    mode: SessionMode
    target_size: int
    actual_size: int
    seed: int
    taste_snapshot_version: int
    status: str
    created_at: str
    completed_at: str | None
    shortages: dict[str, int]


@dataclass(frozen=True)
class DiscoverySessionItem:
    item_id: int
    session_id: int
    candidate_id: int
    position: int
    bucket: CandidateBucket
    score_at_selection: float
    affinity_at_selection: dict[str, object]
    reasons_at_selection: tuple[str, ...]
    decision: Decision | None
    decided_at: str | None
    track: CandidateRecord


@dataclass(frozen=True)
class SessionBuildResult:
    session: DiscoverySession
    items: tuple[DiscoverySessionItem, ...]
    bucket_counts: dict[str, int]
    high_affinity_entities: tuple[str, ...]
    underexplored_labels: tuple[str, ...]
