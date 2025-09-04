import re
import sys
import argparse
import unicodedata

from pathlib import Path
from typing import Dict
from mutagen.id3 import ID3, ID3NoHeaderError, COMM

# (Vocals) tail (with/without parentheses) and separators at the very end
VOCALS_ANY_RE = re.compile(r"[\s_\-–—]*\(?vocals\)?[\s_\-–—]*$", re.IGNORECASE)

# Leading index patterns: 01, 1-18, 2_01, 9_01-01, 01., 02) etc.; allow multiple groups
LEAD_IDX_RE = re.compile(r"""
    ^\s*                                   # start + optional space
    (?:\d{1,3}(?:[_.\-–—]\d{1,3})*)        # number or number-number etc.
    (?:[). _\-–—]*)                        # trailing separators after index
""", re.VERBOSE)

# Strip zero-width chars & NBSPs
ZWS_RE = re.compile(r"[\u200B-\u200D\uFEFF\u2060\u00A0]")

def strip_leading_index(s: str) -> str:
    # Remove one or more leading index groups if they repeat
    prev = None
    while prev != s:
        prev = s
        s = LEAD_IDX_RE.sub("", s, count=1)
    return s

def clean_stem(stem: str, is_acapella: bool) -> str:
    # Unicode normalize and remove invisible/nbsp
    s = unicodedata.normalize("NFKC", stem)
    s = ZWS_RE.sub("", s)

    # Drop leading index blobs (on BOTH sides, safer)
    s = strip_leading_index(s)

    # If acapella, strip trailing "(Vocals)" + surrounding cruft
    if is_acapella:
        s = VOCALS_ANY_RE.sub("", s)

    # Strip trailing separators that sometimes linger
    s = re.sub(r"[\s_\-–—\.,;:]+$", "", s)

    # Replace any run of non-alnum with a single space, then casefold
    s = re.sub(r"[^A-Za-z0-9]+", " ", s).strip().casefold()
    return s

def key_for_path(p: Path, is_acapella: bool) -> str:
    return clean_stem(p.stem, is_acapella)

def ensure_id3(p: Path) -> ID3:
    try:
        return ID3(p)
    except ID3NoHeaderError:
        id3 = ID3()
        id3.save(p)
        return ID3(p)

COPY_FRAMES = ["TIT2","TPE1","TALB","TDRC","TYER","TRCK","TCON","TBPM","TKEY","COMM"]

def frame_text(f):
    if f is None: return None
    try:
        v = getattr(f, "text", None)
        if v is None: return str(f)
        if isinstance(v, list): v = v[0] if v else ""
        return str(v)
    except Exception:
        return "<binary>"

def copy_frames(src: ID3, dst: ID3, overwrite: bool):
    changes = {}
    for fid in COPY_FRAMES:
        src_list = src.getall(fid)
        if not src_list:
            continue
        if fid == "COMM":
            srcf = src_list[0]
            exist = dst.getall("COMM")
            oldv = frame_text(exist[0]) if exist else None
            if overwrite or not exist:
                dst.setall("COMM", [COMM(encoding=3, lang=srcf.lang, desc=srcf.desc, text=srcf.text)])
                changes[fid] = (oldv, frame_text(srcf))
        else:
            srcf = src_list[0]
            oldf = dst.get(fid)
            oldv = frame_text(oldf)
            if overwrite or oldf is None or (hasattr(oldf, "text") and not getattr(oldf, "text")):
                cls = type(srcf)
                if hasattr(srcf, "text"):
                    dst.setall(fid, [cls(encoding=3, text=srcf.text)])
                else:
                    dst.setall(fid, [cls(encoding=3)])
                changes[fid] = (oldv, frame_text(srcf))
    return changes

def main():
    ap = argparse.ArgumentParser(description="Sync MP3 tags from full tracks to '(Vocals)' acapellas by filename (handles leading numbers).")
    ap.add_argument("full_dir")
    ap.add_argument("acapella_dir")
    ap.add_argument("--overwrite", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    full_dir = Path(args.full_dir)
    aca_dir  = Path(args.acapella_dir)

    # Build index of full tracks
    full_index: Dict[str, Path] = {}
    for f in full_dir.rglob("*.mp3"):
        k = key_for_path(f, is_acapella=False)
        full_index[k] = f  # last wins

    matched = updated = missing = 0

    for aca in aca_dir.rglob("*.mp3"):
        k = key_for_path(aca, is_acapella=True)
        src = full_index.get(k)
        if not src:
            missing += 1
            print(f"[MISS] {aca.name}  (key: {k})")
            continue

        matched += 1
        try:
            src_id3 = ID3(src)
            dst_id3 = ensure_id3(aca)
            changes = copy_frames(src_id3, dst_id3, overwrite=args.overwrite)
            if changes:
                updated += 1
                print(f"[SYNC] {aca.name}  <-  {src.name}")
                for fid, (oldv, newv) in changes.items():
                    print(f"       {fid}: '{oldv}' -> '{newv}'")
                if not args.dry_run:
                    dst_id3.save(aca)
            else:
                print(f"[OK]   {aca.name} (no changes)")
        except Exception as e:
            print(f"[ERR]  {aca.name}: {e}")

    print("\nSummary:")
    print(f"  Matched acapellas : {matched}")
    print(f"  Updated files     : {updated}")
    print(f"  Missing matches   : {missing}")
    if args.dry_run:
        print("  (dry-run: nothing written)")

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python sync_mp3_tags_by_filename_numbers.py <full_dir> <acapella_dir> [--overwrite] [--dry-run]")
        sys.exit(1)
    main()
