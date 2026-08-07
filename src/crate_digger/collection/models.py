from dataclasses import dataclass
from pathlib import Path
from typing import Literal


ProfileRole = Literal[
    "warmup",
    "builder",
    "peak",
    "reset",
    "afterhours",
    "closer",
]
TagCategory = Literal["groove", "palette", "mood", "structure", "legacy"]
TagSource = Literal["manual", "rekordbox", "traktor", "model"]
ImportSource = Literal["rekordbox", "traktor"]
MatchStatus = Literal["matched", "unmatched", "ambiguous", "invalid"]
ConflictPolicy = Literal[
    "manual",
    "latest",
    "prefer-rekordbox",
    "prefer-traktor",
]


@dataclass(frozen=True)
class CuePoint:
    """Source-neutral cue or loop marker.

    Positions are milliseconds so Traktor values round-trip without losing
    precision.  ``kind`` retains an unknown source value rather than dropping it.
    """

    name: str | None
    kind: str
    start_ms: float
    length_ms: float | None = None
    hotcue: int | None = None


@dataclass(frozen=True)
class BeatGridMarker:
    start_ms: float
    bpm: float
    meter: str | None = None
    beat: int = 1


@dataclass(frozen=True)
class ImportedPlaylist:
    """A flattened playlist path and the source paths of its members."""

    name: str
    folder_path: tuple[str, ...]
    track_paths: tuple[str, ...]


@dataclass(frozen=True)
class LibraryDocument:
    source: ImportSource
    tracks: tuple["ImportedTrack", ...]
    playlists: tuple[ImportedPlaylist, ...] = ()


@dataclass(frozen=True)
class LocalTrack:
    """A lightweight view of one audio file in the local collection."""

    path: Path
    title: str | None
    artist: str | None
    album: str | None
    duration_seconds: float | None
    bitrate: int | None
    audio_format: str | None
    comment: str | None = None
    genre: str | None = None
    release_date: str | None = None
    file_created_at: str | None = None
    artwork_mime: str | None = None
    artwork_data: bytes | None = None
    spotify_uri: str | None = None
    soundcloud_url: str | None = None
    spotify_link_skipped_at: str | None = None
    indexed_at: str | None = None

    @property
    def display_title(self) -> str:
        return self.title or self.path.stem

    @property
    def display_artist(self) -> str:
        return self.artist or "Unknown artist"


@dataclass(frozen=True)
class TrackProfile:
    track_path: str
    energy: int | None
    personal_rating: int | None
    set_role: ProfileRole | None
    notes: str | None
    updated_at: str


@dataclass(frozen=True)
class TrackTag:
    track_path: str
    category: TagCategory
    value: str
    source: TagSource
    approved: bool
    confidence: float | None
    updated_at: str


@dataclass(frozen=True)
class SourceTrackMetadata:
    track_path: str
    source: ImportSource
    source_track_id: str | None
    legacy_rating: int | None
    genre: str | None
    comment: str | None
    comment2: str | None
    imported_at: str


@dataclass(frozen=True)
class ImportedTrack:
    source: ImportSource
    source_path: str | None
    source_track_id: str | None
    title: str | None
    artist: str | None
    genre: str | None
    comment: str | None
    comment2: str | None
    legacy_rating: int | None
    tags: tuple[tuple[TagCategory, str], ...]
    invalid_reason: str | None = None
    album: str | None = None
    label: str | None = None
    bpm: float | None = None
    musical_key: str | None = None
    color: str | None = None
    play_count: int | None = None
    date_added: str | None = None
    cues: tuple[CuePoint, ...] = ()
    beatgrids: tuple[BeatGridMarker, ...] = ()


@dataclass(frozen=True)
class CanonicalTrackMetadata:
    track_path: str
    title: str | None
    artist: str | None
    album: str | None
    genre: str | None
    label: str | None
    comment: str | None
    rating: int | None
    bpm: float | None
    musical_key: str | None
    color: str | None
    play_count: int | None
    date_added: str | None
    cues: tuple[CuePoint, ...]
    beatgrids: tuple[BeatGridMarker, ...]
    updated_at: str


@dataclass(frozen=True)
class SyncConflict:
    conflict_id: int
    track_path: str | None
    playlist_path: str | None
    field_name: str
    rekordbox_value: object
    traktor_value: object
    status: str
    resolution: str | None
    created_at: str
    resolved_at: str | None


@dataclass(frozen=True)
class SyncReport:
    dry_run: bool
    matched_rekordbox: int
    matched_traktor: int
    canonical_tracks: int
    playlists: int
    conflicts: int
    unresolved_conflicts: int
    rekordbox_changes: int
    traktor_changes: int
    audio_files_copied: int = 0
    backups: tuple[str, ...] = ()


@dataclass(frozen=True)
class ImportMatch:
    track: ImportedTrack
    status: MatchStatus
    track_path: str | None
    candidate_paths: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class ImportReport:
    source_type: ImportSource
    source_file: str
    dry_run: bool
    matches: tuple[ImportMatch, ...]
    imported_ratings: int = 0
    imported_tags: int = 0
    database_changes: int = 0
    import_id: int | None = None
    imported_at: str | None = None

    @property
    def parsed_count(self) -> int:
        return len(self.matches)

    def status_count(self, status: MatchStatus) -> int:
        return sum(match.status == status for match in self.matches)

    @property
    def matched_count(self) -> int:
        return self.status_count("matched")

    @property
    def unmatched_count(self) -> int:
        return self.status_count("unmatched")

    @property
    def ambiguous_count(self) -> int:
        return self.status_count("ambiguous")

    @property
    def invalid_count(self) -> int:
        return self.status_count("invalid")


@dataclass(frozen=True)
class LibraryImport:
    import_id: int
    source_type: ImportSource
    source_file: str
    imported_at: str
    dry_run: bool
    parsed_count: int
    matched_count: int
    unmatched_count: int
    ambiguous_count: int
    invalid_count: int
