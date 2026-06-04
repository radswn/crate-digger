import argparse
import copy
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, cast

from mutagen import File
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3, ID3NoHeaderError, TIT2, TPE1
from mutagen.wave import WAVE


DEFAULT_ORIGINALS_DIR = Path("/mnt/c/Users/Radek/Music/todo instrumentals")
DEFAULT_INSTRUMENTALS_DIR = Path("/mnt/c/Users/Radek/Music/instrumentals")

SOURCE_EXTENSIONS = {".aif", ".aiff", ".flac", ".mp3", ".wav"}
TARGET_EXTENSIONS = {".flac", ".mp3", ".wav"}

INSTRUMENTAL_TAIL_RE = re.compile(
    r"[\s_\-–—]*\(?instrumental\)?[\s_\-–—]*$", re.IGNORECASE
)
LEAD_IDX_RE = re.compile(
    r"""
    ^\s*
    (?:\d{1,4}(?:[_.\-–—]\d{1,4})*)
    (?:[). _\-–—]*)
    """,
    re.VERBOSE,
)
ZWS_RE = re.compile(r"[\u200B-\u200D\uFEFF\u2060\u00A0]")


@dataclass(frozen=True)
class SourceMetadata:
    title: str | None
    artist: str | None
    artwork: list[APIC]


def strip_leading_index(value: str) -> str:
    previous = None
    while previous != value:
        previous = value
        value = LEAD_IDX_RE.sub("", value, count=1)
    return value


def ascii_fold(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def clean_stem(stem: str, *, is_instrumental: bool) -> str:
    value = unicodedata.normalize("NFKC", stem)
    value = ZWS_RE.sub("", value)
    value = strip_leading_index(value)
    if is_instrumental:
        value = INSTRUMENTAL_TAIL_RE.sub("", value)
        value = strip_leading_index(value)
    value = re.sub(r"[\s_\-–—\.,;:]+$", "", value)
    value = ascii_fold(value).casefold()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def key_for_path(path: Path, *, is_instrumental: bool) -> str:
    return clean_stem(path.stem, is_instrumental=is_instrumental)


def iter_sources(path: Path) -> Iterable[Path]:
    for item in sorted(path.iterdir()):
        if item.is_file() and item.suffix.casefold() in SOURCE_EXTENSIONS:
            yield item


def iter_instrumentals(path: Path) -> Iterable[Path]:
    for item in sorted(path.iterdir()):
        if item.is_file() and item.suffix.casefold() in TARGET_EXTENSIONS:
            yield item


def text_frame_value(frame) -> str | None:
    if frame is None:
        return None
    text = getattr(frame, "text", None)
    if not text:
        return None
    if isinstance(text, list):
        return str(text[0]) if text else None
    return str(text)


def tag_text(tags, id3_key: str, easy_key: str) -> str | None:
    if tags is None:
        return None

    frame = tags.get(id3_key)
    value = text_frame_value(frame)
    if value:
        return value

    values = tags.get(easy_key)
    if isinstance(values, list) and values:
        return str(values[0])
    if values:
        return str(values)
    return None


def apic_from_picture(picture: Picture) -> APIC:
    return APIC(
        encoding=3,
        mime=picture.mime or "image/jpeg",
        type=picture.type,
        desc=picture.desc or "",
        data=picture.data,
    )


def picture_from_apic(frame: APIC) -> Picture:
    apic = cast(Any, frame)
    picture = Picture()
    picture.mime = apic.mime or "image/jpeg"
    picture.type = apic.type
    picture.desc = apic.desc or ""
    picture.data = apic.data
    return picture


def artwork_from_audio(audio) -> list[APIC]:
    artwork: list[APIC] = []
    tags = audio.tags
    if tags is not None and hasattr(tags, "getall"):
        artwork.extend(copy.deepcopy(tags.getall("APIC")))

    pictures = getattr(audio, "pictures", None)
    if pictures and not artwork:
        artwork.extend(apic_from_picture(picture) for picture in pictures)

    return artwork


def read_source_metadata(path: Path) -> SourceMetadata:
    audio = File(path)
    if audio is None:
        raise ValueError("unsupported audio file")

    tags = audio.tags
    title = tag_text(tags, "TIT2", "title")
    artist = tag_text(tags, "TPE1", "artist")
    return SourceMetadata(title=title, artist=artist, artwork=artwork_from_audio(audio))


def read_target_metadata(path: Path) -> SourceMetadata:
    audio = File(path)
    if audio is None:
        return SourceMetadata(title=None, artist=None, artwork=[])

    tags = audio.tags
    title = tag_text(tags, "TIT2", "title")
    artist = tag_text(tags, "TPE1", "artist")
    return SourceMetadata(title=title, artist=artist, artwork=artwork_from_audio(audio))


def load_mp3_tags(path: Path, *, create_on_disk: bool) -> ID3:
    try:
        return ID3(path)
    except ID3NoHeaderError:
        id3 = ID3()
        if create_on_disk:
            id3.save(path)
            return ID3(path)
        return id3


def describe_target(path: Path) -> tuple[str | None, str | None, int]:
    metadata = read_target_metadata(path)
    return metadata.title, metadata.artist, len(metadata.artwork)


def apic_signature(frame: APIC) -> tuple[str, int, str, bytes]:
    apic = cast(Any, frame)
    return apic.mime, apic.type, apic.desc, apic.data


def apic_signatures(frames: Iterable[APIC]) -> list[tuple[str, int, str, bytes]]:
    return [apic_signature(frame) for frame in frames]


def changed_text(
    changes: list[str],
    field_name: str,
    old_value: str | None,
    new_value: str | None,
    overwrite: bool,
) -> bool:
    if not new_value or new_value == old_value:
        return False
    if old_value and not overwrite:
        return False
    changes.append(f"{field_name}: {old_value!r} -> {new_value!r}")
    return True


def sync_id3_metadata(
    source_metadata: SourceMetadata,
    target: Path,
    *,
    apply: bool,
    overwrite_text: bool,
    overwrite_artwork: bool,
) -> list[str]:
    if target.suffix.casefold() == ".wav":
        audio = WAVE(target)
        target_tags = audio.tags
        if target_tags is None:
            target_tags = ID3()
            if apply:
                audio.add_tags()
                target_tags = audio.tags
    else:
        audio = None
        target_tags = load_mp3_tags(target, create_on_disk=apply)

    if target_tags is None:
        raise ValueError("could not load or create ID3 tags")

    changes: list[str] = []

    old_title = text_frame_value(target_tags.get("TIT2"))
    if changed_text(changes, "title", old_title, source_metadata.title, overwrite_text):
        target_tags.setall("TIT2", [TIT2(encoding=3, text=source_metadata.title)])

    old_artist = text_frame_value(target_tags.get("TPE1"))
    if changed_text(
        changes, "artist", old_artist, source_metadata.artist, overwrite_text
    ):
        target_tags.setall("TPE1", [TPE1(encoding=3, text=source_metadata.artist)])

    old_artwork = target_tags.getall("APIC")
    if source_metadata.artwork and apic_signatures(
        source_metadata.artwork
    ) != apic_signatures(old_artwork):
        if not old_artwork or overwrite_artwork:
            old_count = len(old_artwork)
            target_tags.delall("APIC")
            target_tags.setall("APIC", copy.deepcopy(source_metadata.artwork))
            changes.append(
                f"artwork: {old_count} frame(s) -> {len(source_metadata.artwork)} frame(s)"
            )

    if changes and apply:
        if audio is not None:
            audio.save()
        else:
            target_tags.save(target)

    return changes


def sync_flac_metadata(
    source_metadata: SourceMetadata,
    target: Path,
    *,
    apply: bool,
    overwrite_text: bool,
    overwrite_artwork: bool,
) -> list[str]:
    audio = FLAC(target)
    changes: list[str] = []

    old_title = tag_text(audio.tags, "TIT2", "title")
    if changed_text(changes, "title", old_title, source_metadata.title, overwrite_text):
        audio["title"] = [source_metadata.title]

    old_artist = tag_text(audio.tags, "TPE1", "artist")
    if changed_text(
        changes, "artist", old_artist, source_metadata.artist, overwrite_text
    ):
        audio["artist"] = [source_metadata.artist]

    old_artwork = [apic_from_picture(picture) for picture in audio.pictures]
    if source_metadata.artwork and apic_signatures(
        source_metadata.artwork
    ) != apic_signatures(old_artwork):
        if not old_artwork or overwrite_artwork:
            old_count = len(old_artwork)
            audio.clear_pictures()
            for frame in source_metadata.artwork:
                audio.add_picture(picture_from_apic(frame))
            changes.append(
                f"artwork: {old_count} frame(s) -> {len(source_metadata.artwork)} frame(s)"
            )

    if changes and apply:
        audio.save()

    return changes


def sync_metadata(
    source: Path,
    target: Path,
    *,
    apply: bool,
    overwrite_text: bool,
    overwrite_artwork: bool,
) -> list[str]:
    source_metadata = read_source_metadata(source)
    target_suffix = target.suffix.casefold()

    if target_suffix == ".flac":
        return sync_flac_metadata(
            source_metadata,
            target,
            apply=apply,
            overwrite_text=overwrite_text,
            overwrite_artwork=overwrite_artwork,
        )
    if target_suffix in {".mp3", ".wav"}:
        return sync_id3_metadata(
            source_metadata,
            target,
            apply=apply,
            overwrite_text=overwrite_text,
            overwrite_artwork=overwrite_artwork,
        )
    raise ValueError(f"unsupported target extension: {target.suffix}")


def build_source_index(path: Path) -> tuple[dict[str, Path], dict[str, list[Path]]]:
    grouped: dict[str, list[Path]] = {}
    for source in iter_sources(path):
        grouped.setdefault(key_for_path(source, is_instrumental=False), []).append(
            source
        )

    unique = {key: files[0] for key, files in grouped.items() if len(files) == 1}
    ambiguous = {key: files for key, files in grouped.items() if len(files) > 1}
    return unique, ambiguous


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Copy title, artist, and artwork from originals to matching instrumentals."
    )
    parser.add_argument("--originals-dir", type=Path, default=DEFAULT_ORIGINALS_DIR)
    parser.add_argument(
        "--instrumentals-dir", type=Path, default=DEFAULT_INSTRUMENTALS_DIR
    )
    parser.add_argument(
        "--limit", type=int, help="stop after this many matched instrumentals"
    )
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        help="case-insensitive substring filter for instrumental filenames; can be repeated",
    )
    parser.add_argument(
        "--apply", action="store_true", help="write changes; default is dry-run"
    )
    parser.add_argument(
        "--no-overwrite-text",
        action="store_true",
        help="keep existing target title/artist frames",
    )
    parser.add_argument(
        "--no-overwrite-artwork",
        action="store_true",
        help="keep existing target artwork frames",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    source_index, ambiguous_sources = build_source_index(args.originals_dir)
    filters = [value.casefold() for value in args.only]

    matched = changed = missing = ambiguous = 0
    print(f"Mode: {'apply' if args.apply else 'dry-run'}")

    for target in iter_instrumentals(args.instrumentals_dir):
        if filters and not any(value in target.name.casefold() for value in filters):
            continue

        key = key_for_path(target, is_instrumental=True)
        if key in ambiguous_sources:
            ambiguous += 1
            print(f"[AMBIG] {target.name}")
            for source in ambiguous_sources[key]:
                print(f"        candidate: {source.name}")
            continue

        source = source_index.get(key)
        if source is None:
            missing += 1
            print(f"[MISS]  {target.name}  (key: {key})")
            continue

        matched += 1
        before_title, before_artist, before_artwork_count = describe_target(target)
        try:
            changes = sync_metadata(
                source,
                target,
                apply=args.apply,
                overwrite_text=not args.no_overwrite_text,
                overwrite_artwork=not args.no_overwrite_artwork,
            )
        except Exception as exc:
            print(f"[ERR]   {target.name}: {exc}")
            continue

        status = "SYNC" if changes else "OK"
        if changes:
            changed += 1
        print(f"[{status}]  {target.name}  <-  {source.name}")
        print(
            "        before: "
            f"title={before_title!r}, artist={before_artist!r}, artwork={before_artwork_count}"
        )
        for change in changes:
            print(f"        {change}")

        if args.limit is not None and matched >= args.limit:
            break

    print("\nSummary:")
    print(f"  Matched instrumentals : {matched}")
    print(f"  Changed files         : {changed}")
    print(f"  Missing matches       : {missing}")
    print(f"  Ambiguous matches     : {ambiguous}")
    if not args.apply:
        print("  Dry-run: nothing written. Add --apply to update files.")


if __name__ == "__main__":
    main()
