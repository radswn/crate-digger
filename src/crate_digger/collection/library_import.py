import json
from pathlib import Path

from crate_digger.collection.importers import parse_rekordbox, parse_traktor
from crate_digger.collection.matching import PathMap, load_indexed_tracks, match_tracks
from crate_digger.collection.models import ImportReport, ImportSource
from crate_digger.collection.profiles import persist_import


def import_library(
    *,
    source: ImportSource,
    source_file: Path,
    db_path: Path,
    dry_run: bool = False,
    path_maps: tuple[PathMap, ...] = (),
) -> ImportReport:
    parser = parse_rekordbox if source == "rekordbox" else parse_traktor
    imported = parser(source_file)
    matches = match_tracks(
        imported,
        load_indexed_tracks(db_path, ensure_schema=not dry_run),
        path_maps,
    )
    report = ImportReport(
        source_type=source,
        source_file=str(source_file.resolve()),
        dry_run=dry_run,
        matches=tuple(matches),
        imported_ratings=sum(
            match.status == "matched" and match.track.legacy_rating is not None
            for match in matches
        ),
        imported_tags=sum(
            len(match.track.tags) for match in matches if match.status == "matched"
        ),
    )
    return report if dry_run else persist_import(db_path, report)


def report_to_dict(report: ImportReport) -> dict[str, object]:
    return {
        "source_type": report.source_type,
        "source_file": report.source_file,
        "dry_run": report.dry_run,
        "import_id": report.import_id,
        "imported_at": report.imported_at,
        "summary": {
            "parsed_tracks": report.parsed_count,
            "matched_tracks": report.matched_count,
            "unmatched_tracks": report.unmatched_count,
            "ambiguous_tracks": report.ambiguous_count,
            "invalid_records": report.invalid_count,
            "imported_ratings": report.imported_ratings,
            "imported_tags": report.imported_tags,
            "database_changes": report.database_changes,
        },
        "matches": [
            {
                "status": match.status,
                "source_path": match.track.source_path,
                "artist": match.track.artist,
                "title": match.track.title,
                "matched_path": match.track_path,
                "candidate_paths": list(match.candidate_paths),
                "reason": match.reason,
            }
            for match in report.matches
        ],
    }


def write_json_report(report: ImportReport, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report_to_dict(report), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
