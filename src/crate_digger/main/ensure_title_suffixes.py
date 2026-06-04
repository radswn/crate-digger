import argparse
from pathlib import Path

from crate_digger.collection.title_suffix import (
    ACAPELLA_TAIL_RE,
    INSTRUMENTAL_TAIL_RE,
    TitleSuffixRule,
    ensure_title_suffixes,
)


DEFAULT_INSTRUMENTALS_DIR = Path("/mnt/c/Users/Radek/Music/instrumentals")
DEFAULT_ACAPELLAS_DIR = Path("/mnt/c/Users/Radek/Music/acapellas")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ensure instrumental/acapella title tags have canonical suffixes."
    )
    parser.add_argument(
        "--instrumentals-dir", type=Path, default=DEFAULT_INSTRUMENTALS_DIR
    )
    parser.add_argument("--acapellas-dir", type=Path, default=DEFAULT_ACAPELLAS_DIR)
    parser.add_argument(
        "--limit", type=int, help="stop after this many processed files"
    )
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        help="case-insensitive substring filter for filenames; can be repeated",
    )
    parser.add_argument(
        "--apply", action="store_true", help="write changes; default is dry-run"
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    rules = [
        TitleSuffixRule(
            path=args.instrumentals_dir,
            suffix="Instrumental",
            tail_pattern=INSTRUMENTAL_TAIL_RE,
        ),
        TitleSuffixRule(
            path=args.acapellas_dir,
            suffix="Acapella",
            tail_pattern=ACAPELLA_TAIL_RE,
        ),
    ]

    results = ensure_title_suffixes(
        rules,
        apply=args.apply,
        filters=args.only,
        limit=args.limit,
    )

    changed = 0
    errors = 0
    print(f"Mode: {'apply' if args.apply else 'dry-run'}")
    for result in results:
        if result.error:
            errors += 1
            print(f"[ERR]  {result.path}: {result.error}")
            continue
        if result.changed:
            changed += 1
            print(f"[FIX]  {result.path}")
            print(f"       title: {result.before_title!r} -> {result.after_title!r}")
        else:
            print(f"[OK]   {result.path}")

    print("\nSummary:")
    print(f"  Processed files : {len(results)}")
    print(f"  Changed files   : {changed}")
    print(f"  Errors          : {errors}")
    if not args.apply:
        print("  Dry-run: nothing written. Add --apply to update files.")


if __name__ == "__main__":
    main()
