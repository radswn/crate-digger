import argparse
import csv
import sqlite3
import sys
from collections.abc import Sequence
from dataclasses import asdict, fields, replace
from pathlib import Path
from typing import cast

from crate_digger.collection.index import DEFAULT_COLLECTION_DB_PATH
from crate_digger.collection.library_import import import_library, write_json_report
from crate_digger.collection.matching import parse_path_map
from crate_digger.collection.models import ImportReport
from crate_digger.collection.profiles import (
    calculate_status_counts,
    list_all_tags,
    list_training_rows,
)
from crate_digger.discover.indexer import (
    enrich_missing_spotify_metadata,
    index_existing_playlists,
    index_local_collection,
)
from crate_digger.discover.labels import normalize_label_name
from crate_digger.discover.models import (
    AffinityStats,
    IndexSummary,
    SessionBuildResult,
    SessionMode,
    TasteRebuildSummary,
)
from crate_digger.discover.repository import (
    catalogue_linkage_counts,
    discovery_counts,
    find_affinity,
    get_affinities,
)
from crate_digger.discover.sessions import (
    build_session,
    expand_release,
    explore_label,
    get_session,
    get_session_item,
    list_session_items,
    list_sessions,
    record_feedback,
)
from crate_digger.discover.taste import rebuild_taste_index
from crate_digger.utils.config import get_settings
from crate_digger.utils.followed_labels import load_followed_labels_state
from crate_digger.utils.spotify import get_spotify_client


TRAINING_TAG_DELIMITER = "|"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="crate-digger")
    namespaces = parser.add_subparsers(dest="namespace", required=True)
    library = namespaces.add_parser(
        "library", help="Import and review local library metadata."
    )
    commands = library.add_subparsers(dest="library_command", required=True)

    for name, source, help_text in (
        ("import-rekordbox", "rekordbox", "Import a Rekordbox XML collection."),
        ("import-traktor", "traktor", "Import a Traktor NML collection."),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("path", type=Path)
        command.add_argument("--db-path", type=Path, default=DEFAULT_COLLECTION_DB_PATH)
        command.add_argument("--dry-run", action="store_true")
        command.add_argument(
            "--path-map",
            action="append",
            default=[],
            metavar="SOURCE=DESTINATION",
        )
        command.add_argument("--report", type=Path)
        command.set_defaults(import_source=source)

    status = commands.add_parser("status", help="Show Track Profiles coverage.")
    status.add_argument("--db-path", type=Path, default=DEFAULT_COLLECTION_DB_PATH)

    export = commands.add_parser(
        "export-training-data", help="Export Track Profiles as UTF-8 CSV."
    )
    export.add_argument("output", type=Path)
    export.add_argument("--db-path", type=Path, default=DEFAULT_COLLECTION_DB_PATH)

    discover = namespaces.add_parser(
        "discover", help="Build taste-aware, explainable listening sessions."
    )
    discover_commands = discover.add_subparsers(dest="discover_command", required=True)

    index_existing = discover_commands.add_parser(
        "index-existing", help="Index local and playlist catalogue tracks."
    )
    _add_discovery_paths(index_existing, config=True)

    rebuild = discover_commands.add_parser(
        "rebuild-taste", help="Refresh taste signals and affinity statistics."
    )
    _add_discovery_paths(rebuild, config=True)
    rebuild.add_argument(
        "--offline",
        action="store_true",
        help="Do not call Spotify to fill missing entity metadata.",
    )

    taste_stats = discover_commands.add_parser(
        "taste-stats", help="Explain artist and label affinity statistics."
    )
    _add_discovery_paths(taste_stats, config=True)
    entity_filter = taste_stats.add_mutually_exclusive_group()
    entity_filter.add_argument("--label")
    entity_filter.add_argument("--artist")

    build = discover_commands.add_parser(
        "build", help="Build a deterministic discovery session."
    )
    _add_discovery_paths(build, config=True)
    build.add_argument(
        "--mode",
        choices=("balanced", "fresh", "deep-dig", "frontier"),
        default="balanced",
    )
    build.add_argument("--size", type=int, default=30)
    build.add_argument("--seed", type=int, default=0)
    build.add_argument("--label")
    build.add_argument("--artist")

    list_command = discover_commands.add_parser("list", help="List sessions.")
    _add_discovery_paths(list_command)
    show = discover_commands.add_parser("show", help="Show a session.")
    show.add_argument("session_id", type=int)
    _add_discovery_paths(show)
    explain = discover_commands.add_parser("explain", help="Explain one session item.")
    explain.add_argument("session_id", type=int)
    explain.add_argument("item_id", type=int)
    _add_discovery_paths(explain)
    feedback = discover_commands.add_parser(
        "feedback", help="Record Keep, Maybe, Pass, or Skip."
    )
    feedback.add_argument("session_id", type=int)
    feedback.add_argument("item_id", type=int)
    feedback.add_argument("decision", choices=("keep", "maybe", "pass", "skip"))
    _add_discovery_paths(feedback)
    expand = discover_commands.add_parser(
        "expand-release", help="Expose remaining tracks from an item’s release."
    )
    expand.add_argument("session_id", type=int)
    expand.add_argument("item_id", type=int)
    _add_discovery_paths(expand)
    explore = discover_commands.add_parser(
        "explore-label", help="Create a bounded sampler for an item’s label."
    )
    explore.add_argument("session_id", type=int)
    explore.add_argument("item_id", type=int)
    _add_discovery_paths(explore)
    stats = discover_commands.add_parser(
        "stats", help="Show discovery and decision statistics."
    )
    _add_discovery_paths(stats)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.namespace == "discover":
            return _run_discover(args)
        if args.library_command.startswith("import-"):
            path_maps = tuple(parse_path_map(value) for value in args.path_map)
            report = import_library(
                source=args.import_source,
                source_file=args.path,
                db_path=args.db_path,
                dry_run=args.dry_run,
                path_maps=path_maps,
            )
            if args.report is not None:
                write_json_report(report, args.report)
            _print_import_report(report)
            return 0
        if args.library_command == "status":
            _print_status(calculate_status_counts(args.db_path))
            return 0
        if args.library_command == "export-training-data":
            _export_training_data(args.db_path, args.output)
            print(f"Exported training data to {args.output}")
            return 0
    except (KeyError, OSError, sqlite3.Error, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    parser.error("Unknown command")
    return 2


def _print_import_report(report: ImportReport) -> None:
    values = (
        ("Source type", report.source_type),
        ("Parsed tracks", report.parsed_count),
        ("Matched tracks", report.matched_count),
        ("Unmatched tracks", report.unmatched_count),
        ("Ambiguous tracks", report.ambiguous_count),
        ("Invalid records", report.invalid_count),
        ("Imported ratings", report.imported_ratings),
        ("Imported tags", report.imported_tags),
        ("Database changes made", report.database_changes),
    )
    if report.dry_run:
        print("Dry run — no database changes were made.")
    for label, value in values:
        print(f"{label}: {value}")


def _print_status(status: dict[str, object]) -> None:
    fields = (
        ("Total indexed tracks", "total_tracks"),
        ("Tracks with any source metadata", "tracks_with_source_metadata"),
        ("Tracks imported from Rekordbox", "rekordbox_tracks"),
        ("Tracks imported from Traktor", "traktor_tracks"),
        ("Tracks with manual profiles", "tracks_with_manual_profiles"),
        ("Tracks missing energy", "tracks_missing_energy"),
        ("Tracks with energy", "tracks_with_energy"),
        ("Tracks with personal rating", "tracks_with_personal_rating"),
        ("Tracks with role", "tracks_with_role"),
    )
    print("Track Profiles status")
    for label, key in fields:
        print(f"{label}: {status.get(key, 0)}")
    _print_breakdown("Tags by category", status.get("tags_by_category"))
    _print_breakdown("Tags by source", status.get("tags_by_source"))
    _print_breakdown("Most recent import by source", status.get("latest_imports"))


def _print_breakdown(title: str, value: object) -> None:
    print(f"{title}:")
    if not isinstance(value, dict) or not value:
        print("  none")
        return
    for key, count in value.items():
        print(f"  {key}: {count}")


def _export_training_data(db_path: Path, output: Path) -> None:
    fieldnames = (
        "path",
        "artist",
        "title",
        "album",
        "duration_seconds",
        "audio_format",
        "spotify_uri",
        "rekordbox_legacy_rating",
        "traktor_legacy_rating",
        "energy",
        "personal_rating",
        "set_role",
        "manual_tags",
        "rekordbox_tags",
        "traktor_tags",
    )
    tags_by_track: dict[str, dict[str, list[str]]] = {}
    for tag in list_all_tags(db_path):
        sources = tags_by_track.setdefault(
            tag.track_path, {"manual": [], "rekordbox": [], "traktor": []}
        )
        if tag.source in sources:
            sources[tag.source].append(f"{tag.category}:{tag.value}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in list_training_rows(db_path):
            tags_by_source = tags_by_track.get(
                str(row["path"]),
                {"manual": [], "rekordbox": [], "traktor": []},
            )
            record = {key: row[key] for key in fieldnames if not key.endswith("_tags")}
            for source in tags_by_source:
                record[f"{source}_tags"] = TRAINING_TAG_DELIMITER.join(
                    sorted(tags_by_source[source])
                )
            writer.writerow(record)


def _add_discovery_paths(
    parser: argparse.ArgumentParser, *, config: bool = False
) -> None:
    parser.add_argument("--db-path", type=Path, default=DEFAULT_COLLECTION_DB_PATH)
    if config:
        parser.add_argument("--config", default="config.toml")


def _run_discover(args: argparse.Namespace) -> int:
    command = args.discover_command
    if command == "index-existing":
        settings = get_settings(args.config)
        followed = load_followed_labels_state() or []
        local = index_local_collection(
            args.db_path,
            label_aliases=settings["discovery"]["label_aliases"],
        )
        client = get_spotify_client(" ".join(settings["spotify"]["scopes"]))
        playlist = index_existing_playlists(
            client,
            args.db_path,
            followed_labels=followed,
            configured_playlists={
                "to_download": settings["spotify"]["to_download_playlist"],
                "acapella": settings["spotify"]["acapella_playlist"],
                "to_listen": settings["spotify"]["to_listen_playlist"],
                "followed_labels": settings["spotify"]["followed_labels_playlist"],
            },
            label_aliases=settings["discovery"]["label_aliases"],
        )
        enriched = enrich_missing_spotify_metadata(
            client,
            args.db_path,
            label_aliases=settings["discovery"]["label_aliases"],
        )
        _print_index_summary(
            _with_catalogue_counts(
                args.db_path, _merge_index_summaries(local, playlist, enriched)
            )
        )
        return 0
    if command == "rebuild-taste":
        settings = get_settings(args.config)
        local = index_local_collection(
            args.db_path,
            label_aliases=settings["discovery"]["label_aliases"],
        )
        if args.offline:
            enriched = IndexSummary()
        else:
            client = get_spotify_client(" ".join(settings["spotify"]["scopes"]))
            enriched = enrich_missing_spotify_metadata(
                client,
                args.db_path,
                label_aliases=settings["discovery"]["label_aliases"],
            )
        summary = rebuild_taste_index(args.db_path)
        _print_index_summary(
            _with_catalogue_counts(
                args.db_path, _merge_index_summaries(local, enriched)
            )
        )
        _print_taste_rebuild(summary)
        return 0
    if command == "taste-stats":
        if args.label:
            settings = get_settings(args.config)
            label = normalize_label_name(
                args.label, settings["discovery"]["label_aliases"]
            ).display_name
            _print_affinity(
                find_affinity(args.db_path, entity_type="label", name=label)
            )
        elif args.artist:
            _print_affinity(
                find_affinity(args.db_path, entity_type="artist", name=args.artist)
            )
        else:
            for entity_type in ("artist", "label", "tag", "source"):
                affinities = get_affinities(args.db_path, entity_type)
                print(
                    f"{entity_type.title()} affinities: {sum(item.sample_size > 0 for item in affinities)} with evidence"
                )
                for item in affinities[:5]:
                    if item.sample_size:
                        print(
                            f"  {item.entity_name}: {item.smoothed_affinity:.0%} "
                            f"({item.sample_size} reviewed; {item.neutral_indexed_count} neutral)"
                        )
        return 0
    if command == "build":
        settings = get_settings(args.config)
        label_filter = (
            normalize_label_name(
                args.label, settings["discovery"]["label_aliases"]
            ).display_name
            if args.label
            else None
        )
        result = build_session(
            args.db_path,
            mode=cast(SessionMode, args.mode),
            size=args.size,
            seed=args.seed,
            freshness_days=settings["discovery"]["freshness_days"],
            label_filter=label_filter,
            artist_filter=args.artist,
        )
        _print_built_session(result)
        return 0
    if command == "list":
        for session in list_sessions(args.db_path):
            print(
                f"{session.session_id}: {session.mode} · {session.actual_size}/{session.target_size} "
                f"· {session.status} · {session.created_at}"
            )
        return 0
    if command == "show":
        session = get_session(args.db_path, args.session_id)
        if session is None:
            raise KeyError(f"Discovery session not found: {args.session_id}")
        print(
            f"Session {session.session_id}: {session.mode} · "
            f"{session.actual_size}/{session.target_size} · {session.status}"
        )
        for item in list_session_items(args.db_path, session.session_id):
            print(
                f"{item.item_id:>4}  {item.position:>2}. [{item.bucket}] "
                f"{item.track.artist_name} — {item.track.title} "
                f"({item.decision or 'pending'})"
            )
        return 0
    if command == "explain":
        item = get_session_item(args.db_path, args.session_id, args.item_id)
        if item is None:
            raise KeyError(f"Discovery session item not found: {args.item_id}")
        print(f"{item.track.artist_name} — {item.track.title}")
        print(f"Score at selection: {item.score_at_selection:.1f}")
        for reason in item.reasons_at_selection:
            print(f"- {reason}")
        return 0
    if command == "feedback":
        item = record_feedback(
            args.db_path,
            session_id=args.session_id,
            item_id=args.item_id,
            decision=args.decision,
        )
        print(
            f"Recorded {item.decision}: {item.track.artist_name} — {item.track.title}"
        )
        return 0
    if command == "expand-release":
        candidate_ids = expand_release(
            args.db_path, session_id=args.session_id, item_id=args.item_id
        )
        print(f"Release expansion made {len(candidate_ids)} remaining tracks eligible.")
        return 0
    if command == "explore-label":
        candidate_ids = explore_label(
            args.db_path, session_id=args.session_id, item_id=args.item_id
        )
        print(f"Label sampler made {len(candidate_ids)} release probes eligible.")
        return 0
    if command == "stats":
        for key, value in discovery_counts(args.db_path).items():
            print(f"{key.replace('_', ' ').title()}: {value}")
        return 0
    raise ValueError(f"Unknown discover command: {command}")


def _merge_index_summaries(*summaries: IndexSummary) -> IndexSummary:
    values = {
        field.name: sum(getattr(summary, field.name) for summary in summaries)
        for field in fields(IndexSummary)
    }
    return IndexSummary(**values)


def _with_catalogue_counts(db_path: Path, summary: IndexSummary) -> IndexSummary:
    return replace(summary, **catalogue_linkage_counts(db_path))


def _print_index_summary(summary: IndexSummary) -> None:
    labels = {
        "tracks_inspected": "Tracks inspected",
        "tracks_with_spotify_ids": "Tracks with Spotify IDs",
        "tracks_linked_to_releases": "Tracks linked to releases",
        "tracks_linked_to_labels": "Tracks linked to normalized labels",
        "created_candidates": "Candidates created",
        "already_indexed": "Already indexed",
        "skipped": "Skipped records",
        "missing_metadata": "Missing metadata",
        "label_aliases_applied": "Label aliases applied",
    }
    for key, value in asdict(summary).items():
        print(f"{labels[key]}: {value}")


def _print_taste_rebuild(summary: TasteRebuildSummary) -> None:
    print("Taste baseline")
    for key, value in asdict(summary).items():
        print(f"{key.replace('_', ' ').title()}: {value}")


def _print_affinity(affinity: AffinityStats | None) -> None:
    if affinity is None:
        print("No matching affinity statistics.")
        return
    data = asdict(affinity)
    print(f"{data['entity_type'].title()}: {data['entity_name']}")
    print(f"Smoothed affinity: {data['smoothed_affinity']:.0%}")
    print(f"Positive evidence: {data['positive_evidence_count']} tracks")
    print(f"Negative evidence: {data['negative_evidence_count']} tracks")
    print(f"Neutral indexed tracks: {data['neutral_indexed_count']}")
    print(f"Sample size: {data['sample_size']}")
    print("Strongest evidence:")
    for signal_type, count in data["top_contributing_signals"]:
        print(f"- {count} {signal_type.replace('_', ' ')} signal(s)")


def _print_built_session(result: SessionBuildResult) -> None:
    session = result.session
    print(f"Session ID: {session.session_id}")
    print(f"Requested size: {session.target_size}")
    print(f"Actual size: {session.actual_size}")
    print(
        "Bucket counts: "
        + ", ".join(
            f"{key}={value}" for key, value in sorted(result.bucket_counts.items())
        )
    )
    shortages = session.shortages or {"none": 0}
    print(
        "Quota shortages: "
        + ", ".join(f"{key}={value}" for key, value in sorted(shortages.items()))
    )
    print(
        "High-affinity entities: "
        + (", ".join(result.high_affinity_entities) or "none")
    )
    print("Underexplored labels: " + (", ".join(result.underexplored_labels) or "none"))
    print("Top explanations:")
    for item in result.items[:5]:
        print(
            f"- {item.track.artist_name} — {item.track.title}: {item.reasons_at_selection[0]}"
        )
    print(f"Review: /discover?session_id={session.session_id}")


if __name__ == "__main__":
    raise SystemExit(main())
