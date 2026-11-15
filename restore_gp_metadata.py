#!/usr/bin/env python3
"""restore_gp_metadata.py — write Google Photos Takeout sidecar metadata back into the media files.

Google Takeout ships every photo/video with a `<name>.<ext>.json` sidecar holding
`photoTakenTime`, `geoData`/`geoDataExif` and `description`; the media itself often
lost its EXIF on upload. Apple Photos reads capture date from EXIF/QuickTime and
location from EXIF GPS, so an import without this step dumps everything into
"today" and loses places.

Pairing rules implemented (all real Takeout layouts):
  * `IMG_1.JPG` + `IMG_1.JPG.json`
  * current exports: `IMG_1.JPG` + `IMG_1.JPG.supplemental-metadata.json`
  * duplicate upload: `IMG_1(1).JPG` + `IMG_1.JPG(1).json` /
    `IMG_1.JPG.supplemental-metadata(1).json`
  * edited copies: `IMG_1-edited.JPG` + `IMG_1-edited.JPG.json`
  * original never exported, only its render: `IMG_1.JPG` sidecar -> `IMG_1-edited.JPG`
  * album/`metadata.json` files (no `photoTakenTime`) are ignored
  * sidecars whose media is missing are reported, never guessed

Metadata and media are split across Takeout's zip parts, so extract *all* parts
into one directory before running this; otherwise most sidecars cannot pair.

Default behaviour is additive: a tag that already exists on the media is kept
(the camera's own EXIF is more trustworthy than Google's copy). Use --overwrite
to force sidecar values.

Time zone: Takeout stores an absolute UTC instant but no offset, so local wall
time cannot be recovered from the export. `--tz local` (default) interprets it in
the machine's time zone at that instant; pass `--tz UTC`, `--tz +08:00` or
`--tz Asia/Shanghai` when the library was captured elsewhere.

Requires exiftool:  brew install exiftool
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

MEDIA_EXTS = {
    ".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".gif", ".tif", ".tiff",
    ".bmp", ".dng", ".avif",
    ".mp4", ".mov", ".m4v", ".3gp", ".avi", ".mkv", ".webm",
}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".3gp", ".avi", ".mkv", ".webm"}

EXIF_FMT = "%Y:%m:%d %H:%M:%S"
DUP_RE = re.compile(r"^(.*)\((\d+)\)$")

# Tags read from existing media to decide what is missing.
READ_TAGS = [
    "-EXIF:DateTimeOriginal",
    "-EXIF:CreateDate",
    "-EXIF:GPSLatitude",
    "-EXIF:GPSLongitude",
    "-EXIF:ImageDescription",
    "-XMP-dc:Description",
    "-IPTC:Caption-Abstract",
    "-QuickTime:CreateDate",
    "-QuickTime:CreationDate",
    "-QuickTime:GPSCoordinates",
    "-FileModifyDate",
]


class Ctx:
    def __init__(self) -> None:
        self.exif = ""            # exiftool path
        self.tz = None            # timezone | None (= system local)
        self.overwrite = False
        self.dry_run = False
        self.file_times = True
        self.description = True
        self.backup = False
        self.verbose = False


# ---------------------------------------------------------------- time helpers

def build_tz(spec: str):
    if spec in ("", "local"):
        return None
    if spec.upper() == "UTC":
        return timezone.utc
    m = re.fullmatch(r"([+-])(\d{1,2}):?(\d{2})", spec)
    if m:
        sign = 1 if m.group(1) == "+" else -1
        return timezone(sign * timedelta(hours=int(m.group(2)), minutes=int(m.group(3))))
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(spec)
    except Exception as exc:  # noqa: BLE001 - report any tz failure verbatim
        raise SystemExit(
            f"invalid --tz {spec!r} ({exc}); use 'local', 'UTC', '+08:00' or an IANA name"
        )


def fmt_offset(off: timedelta) -> str:
    total = int(off.total_seconds())
    sign = "-" if total < 0 else "+"
    total = abs(total)
    return f"{sign}{total // 3600:02d}:{(total % 3600) // 60:02d}"


def capture_time(ts: int, tz):
    """Return (local wall time, utc offset) for an absolute epoch timestamp."""
    if tz is None:
        local = datetime.fromtimestamp(ts)          # naive, machine-local rules
        off = local.astimezone().utcoffset() or timedelta(0)
        return local, off
    aware = datetime.fromtimestamp(ts, tz)
    return aware.replace(tzinfo=None), aware.utcoffset() or timedelta(0)


# ------------------------------------------------------------- sidecar parsing

def sidecar_timestamp(data: dict):
    """Epoch seconds from photoTakenTime / creationTime, else None if not a sidecar."""
    for key in ("photoTakenTime", "creationTime"):
        block = data.get(key)
        if isinstance(block, dict):
            raw = block.get("timestamp")
            try:
                ts = int(float(raw))
            except (TypeError, ValueError):
                ts = None
            if ts and ts > 0:
                return ts
    return None


def parse_formatted(block: dict):
    """Last-resort epoch from the human-readable time, e.g. 'Aug 12, 2019, 6:32:11 PM UTC'.

    A trailing 'UTC' means the wall clock is UTC; anything else is treated as
    machine-local wall time (there is nothing better available in the export).
    """
    text = (block or {}).get("formatted")
    if not isinstance(text, str):
        return None
    text = text.strip()
    utc = text.endswith("UTC")
    if utc:
        text = text[:-3].strip().rstrip(",")
    for pattern in ("%b %d, %Y, %I:%M:%S %p", "%b %d, %Y, %I:%M %p", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(text, pattern)
        except ValueError:
            continue
        return int(dt.replace(tzinfo=timezone.utc if utc else None).timestamp())
    return None


def sidecar_gps(data: dict):
    """(lat, lon, altitude|None) from geoDataExif, falling back to geoData."""
    for key in ("geoDataExif", "geoData"):
        block = data.get(key)
        if not isinstance(block, dict):
            continue
        try:
            lat = float(block.get("latitude"))
            lon = float(block.get("longitude"))
        except (TypeError, ValueError):
            continue
        if lat == 0.0 and lon == 0.0:   # Google's "no location" placeholder
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        try:
            alt = float(block.get("altitude"))
        except (TypeError, ValueError):
            alt = None
        return lat, lon, (alt if alt else None)
    return None


def media_key(filename: str):
    """('IMG_1', 1, '.jpg') for 'IMG_1(1).jpg' — Takeout appends the counter before the extension."""
    stem, ext = os.path.splitext(filename)
    m = DUP_RE.match(stem)
    if m:
        return (m.group(1), int(m.group(2)), ext.lower())
    return (stem, 0, ext.lower())


def sidecar_key(stem: str, dup):
    """Index key for a sidecar stem, honouring Google's after-the-extension counter."""
    if dup is None:
        return media_key(stem)
    base, ext = os.path.splitext(stem)
    return (base, dup, ext.lower())


SUPPLEMENTAL = ".supplemental-metadata"


def parse_sidecar_name(name: str):
    """('IMG_1.JPG', None) / ('IMG_1.JPG', 2) for every Takeout sidecar spelling.

    Current exports name sidecars `<name>.supplemental-metadata.json`, older ones
    `<name>.json`; duplicate uploads get a counter appended to the whole name in
    both styles, i.e. `IMG_1.JPG(2).json` and `IMG_1.JPG.supplemental-metadata(2).json`.
    """
    if not name.lower().endswith(".json"):
        return None
    stem = name[: -len(".json")]
    dup = None
    m = DUP_RE.match(stem)
    if m:
        stem, dup = m.group(1), int(m.group(2))
    if stem.lower().endswith(SUPPLEMENTAL):
        stem = stem[: -len(SUPPLEMENTAL)]
    return stem, dup


def is_media(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in MEDIA_EXTS


# ------------------------------------------------------------------ discovery

def index_media(root: str):
    """dir -> {media_key: path}"""
    index: dict[str, dict[tuple, str]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        found = {}
        for name in filenames:
            if not is_media(name):
                continue
            found[media_key(name)] = os.path.join(dirpath, name)
        if found:
            index[dirpath] = found
    return index


def pair_in_dir(media_names, sidecar_names):
    """Pair sidecar filenames with media filenames inside one Takeout directory.

    Exact name matches claim their media first; leftovers may then match the
    `-edited` render of the same shot, or plain media when only the sidecar
    carries Google's duplicate counter.

    Returns (pairs, unpaired): pairs is [(json_name, media_name, via)], unpaired
    is [(json_name, reason)]. Shared by the directory and zip-part front ends so
    both apply exactly the same rules.
    """
    index = {}
    for name in media_names:
        index[media_key(name)] = name

    pairs, unpaired, pending = [], [], []
    claimed = set()
    for name in sorted(sidecar_names):
        stem, dup = parse_sidecar_name(name)
        if stem is None:
            unpaired.append((name, "not a sidecar name"))
            continue
        target = index.get(sidecar_key(stem, dup))
        if target is None:
            pending.append((name, stem, dup))
        elif target in claimed:
            unpaired.append((name, "duplicate sidecar (already used "
                             f"{target!r})"))
        else:
            claimed.add(target)
            pairs.append((name, target, None))

    for name, stem, dup in pending:
        base, ext = os.path.splitext(stem)
        candidates = []
        if dup is not None:
            # Google numbers the sidecar when the same file name exists twice in
            # the album view, while the media keeps its plain name on disk.
            candidates.append(("unnumbered", media_key(stem)))
        if not base.lower().endswith("-edited"):
            # Takeout often ships only the edited render while the sidecar
            # describes the original: IMG_1.JPG -> IMG_1-edited.JPG
            candidates.append(("edited", (base + "-edited", dup or 0, ext.lower())))

        for via, key in candidates:
            hit = index.get(key)
            if hit is not None and hit not in claimed:
                claimed.add(hit)
                pairs.append((name, hit, via))
                break
        else:
            unpaired.append((name, f"no media for {stem!r}"))
    return pairs, unpaired


def collect(root: str):
    """Walk JSON sidecars and pair them with the media next to them.

    Returns (entries, albums, orphans, invalid); entries are dicts with
    path/json/ts/gps/description/video/via.
    """
    entries, albums, orphans, invalid = [], [], [], []

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        payloads = {}
        for name in sorted(filenames):
            if not name.lower().endswith(".json"):
                continue
            json_path = os.path.join(dirpath, name)
            try:
                with open(json_path, "r", encoding="utf-8-sig") as fh:
                    data = json.load(fh)
            except (OSError, ValueError) as exc:
                invalid.append((json_path, f"unreadable sidecar: {exc}"))
                continue
            if not isinstance(data, dict):
                invalid.append((json_path, "not a JSON object"))
                continue

            ts = sidecar_timestamp(data)
            if ts is None:
                for block in (data.get("photoTakenTime"), data.get("creationTime")):
                    if isinstance(block, dict):
                        ts = parse_formatted(block)
                        if ts:
                            break
            if ts is None:
                albums.append(json_path)      # album folder metadata, no capture time
                continue
            data["ts"] = ts
            payloads[name] = data

        pairs, unpaired = pair_in_dir(
            [f for f in filenames if is_media(f)], payloads.keys()
        )
        for json_name, media_name, via in pairs:
            data = payloads[json_name]
            entries.append(
                {
                    "path": os.path.join(dirpath, media_name),
                    "json": os.path.join(dirpath, json_name),
                    "ts": data["ts"],
                    "gps": sidecar_gps(data),
                    "description": (data.get("description") or "").strip() or None,
                    "video": os.path.splitext(media_name)[1].lower() in VIDEO_EXTS,
                    "via": via,
                }
            )
        for json_name, reason in unpaired:
            orphans.append((os.path.join(dirpath, json_name), reason))
    return entries, albums, orphans, invalid


# ------------------------------------------------------------------ exiftool

def exiftool_batch(ctx: Ctx, args, files=()):
    """Run exiftool with an argument file (avoids ARG_MAX on huge libraries)."""
    fd, argfile = tempfile.mkstemp(suffix=".args")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for arg in args:
                fh.write(arg + "\n")
            for path in files:
                fh.write(path + "\n")
        cmd = [ctx.exif, "-@", argfile, "-charset", "filename=UTF8"]
        return subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    finally:
        os.unlink(argfile)


def read_existing(ctx: Ctx, paths):
    """path -> {grouped tag name: value} plus 'Error' entries on failure."""
    out = {}
    for start in range(0, len(paths), 500):
        chunk = paths[start:start + 500]
        res = exiftool_batch(ctx, ["-json", "-G1", "-n", "-m", *READ_TAGS], chunk)
        if res.returncode > 1:
            raise SystemExit(f"exiftool failed reading metadata:\n{res.stderr.strip()}")
        try:
            records = json.loads(res.stdout or "[]")
        except ValueError:
            raise SystemExit(f"exiftool returned unparsable JSON:\n{res.stdout[:500]}")
        for rec in records:
            src = rec.get("SourceFile")
            if src:
                out[src] = rec
    return out


ZERO_DATE = re.compile(r"^0{4}[:\-]0{2}[:\-]0{2}")
STAMP_RE = re.compile(r"^(\d{4}):(\d{2}):(\d{2}) (\d{2}):(\d{2}):(\d{2})(?:\.\d+)?([+-])(\d{2}):?(\d{2})$")


def offset_tz(text):
    """timezone from '+08:00' / '+0800' / '-05:00'."""
    m = re.fullmatch(r"([+-])(\d{2}):?(\d{2})", text or "")
    if not m:
        return None
    sign = 1 if m.group(1) == "+" else -1
    return timezone(sign * timedelta(hours=int(m.group(2)), minutes=int(m.group(3))))


def parse_stamp(text):
    """Epoch seconds from an exiftool date string, or None."""
    m = STAMP_RE.match(text or "")
    if not m:
        return None
    tz = offset_tz(m.group(7) + m.group(8) + ":" + m.group(9))
    return int(
        datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                 int(m.group(4)), int(m.group(5)), int(m.group(6)), tzinfo=tz).timestamp()
    )


def stamp_epoch(stamp: str, offset: str) -> int:
    """Epoch seconds for an EXIF stamp plus an offset string."""
    return int(datetime.strptime(stamp, EXIF_FMT).replace(tzinfo=offset_tz(offset)).timestamp())


def _filled(value) -> bool:
    if value in (None, "", 0, 0.0, "0"):
        return False
    if isinstance(value, str) and ZERO_DATE.match(value):
        return False      # cameras and ffmpeg both write 0000:00:00:00 as a null date
    return True


def has_tag(rec: dict, *names: str) -> bool:
    """True if any tag with that basename exists in any group (keys are `Group:Tag`)."""
    wanted = set(names)
    return any(
        key != "SourceFile" and key.rsplit(":", 1)[-1] in wanted and _filled(value)
        for key, value in rec.items()
    )


def has_exact(rec: dict, *names: str) -> bool:
    """True if any of the given `Group:Tag` keys carries a value."""
    return any(_filled(rec.get(name)) for name in names)


def iso6709(lat: float, lon: float, alt) -> str:
    """ISO 6709 point reference — the only place QuickTime/MOV stores a location."""
    body = f"{'+' if lat >= 0 else '-'}{abs(lat):07.4f}{'+' if lon >= 0 else '-'}{abs(lon):08.4f}"
    if alt:
        body += f"{'+' if alt >= 0 else '-'}{abs(alt):.3f}"
    return body + "/"


def build_tags(ctx: Ctx, entry: dict, rec: dict):
    """Return the tag dict to write, honouring the add-only default."""
    tags: dict[str, object] = {}
    dt, off = capture_time(entry["ts"], ctx.tz)
    stamp = dt.strftime(EXIF_FMT)
    offset = fmt_offset(off)
    dated = False

    if entry["video"]:
        # Never create an EXIF block inside a video container.
        if ctx.overwrite or not has_tag(rec, "CreateDate", "CreationDate"):
            tags["QuickTime:CreateDate"] = stamp
            tags["QuickTime:ModifyDate"] = stamp
            tags["QuickTime:CreationDate"] = stamp
            dated = True
    elif ctx.overwrite or not has_exact(rec, "ExifIFD:DateTimeOriginal", "ExifIFD:CreateDate"):
        tags["EXIF:DateTimeOriginal"] = stamp
        tags["EXIF:CreateDate"] = stamp
        tags["EXIF:ModifyDate"] = stamp
        tags["EXIF:OffsetTimeOriginal"] = offset
        tags["EXIF:OffsetTimeDigitized"] = offset
        tags["EXIF:OffsetTime"] = offset
        dated = True

    if entry["gps"]:
        lat, lon, alt = entry["gps"]
        if entry["video"]:
            if ctx.overwrite or not has_tag(rec, "GPSCoordinates"):
                tags["QuickTime:GPSCoordinates"] = iso6709(lat, lon, alt)
        elif ctx.overwrite or not has_exact(rec, "GPS:GPSLatitude", "GPS:GPSLongitude"):
            tags["EXIF:GPSLatitude"] = f"{abs(lat):.6f}"
            tags["EXIF:GPSLatitudeRef"] = "N" if lat >= 0 else "S"
            tags["EXIF:GPSLongitude"] = f"{abs(lon):.6f}"
            tags["EXIF:GPSLongitudeRef"] = "E" if lon >= 0 else "W"
            if alt:
                tags["EXIF:GPSAltitude"] = f"{abs(alt):.3f}"
                tags["EXIF:GPSAltitudeRef"] = 0 if alt >= 0 else 1

    if ctx.description and entry["description"] and not entry["video"]:
        if ctx.overwrite or not has_tag(rec, "Description", "ImageDescription", "Caption-Abstract"):
            tags["XMP:Description"] = entry["description"]
            tags["EXIF:ImageDescription"] = entry["description"]

    # File dates are what Photos falls back to, and exiftool resets them to "now"
    # on every rewrite — so pin them to whichever capture date this file ends up
    # with: the sidecar's, or the EXIF one we deliberately left alone.
    if ctx.file_times:
        file_stamp, file_offset = stamp, offset
        if not dated:
            for key in ("ExifIFD:DateTimeOriginal", "ExifIFD:CreateDate", "QuickTime:CreateDate"):
                if _filled(rec.get(key)):
                    file_stamp = str(rec[key])
                    kept_offset = rec.get("ExifIFD:OffsetTimeOriginal")
                    file_offset = kept_offset if _filled(kept_offset) else offset
                    break
        target = f"{file_stamp}{file_offset}"
        if tags:
            tags["FileModifyDate"] = target          # file is being rewritten anyway
            tags["FileCreateDate"] = target
        else:
            # Only mtime is repaired on its own: macOS stores birthtime with a
            # local-time quirk (a 1h shift for far-future dates), so comparing it
            # would rewrite the whole library on every run.
            current = parse_stamp(rec.get("System:FileModifyDate"))
            if current is None or abs(current - stamp_epoch(file_stamp, file_offset)) >= 1:
                tags["FileModifyDate"] = target
                tags["FileCreateDate"] = target
    return tags


# ----------------------------------------------------------------------- main

def apply_plan(ctx: Ctx, plan, label: str = "file"):
    """Write one exiftool JSON-import batch. plan is [(entry, tags, rec)].

    Returns (written_count, warnings).
    """
    payload = [{"SourceFile": entry["path"], **tags} for entry, tags, _ in plan]
    warnings: list[str] = []
    fd, import_path = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        flags = ["-json=" + import_path, "-m", "-api", "LargeFileSupport=1",
                 "-charset", "exif=UTF8", "-charset", "iptc=UTF8",
                 "-charset", "quicktime=UTF8", "-charset", "photoshop=UTF8"]
        if not ctx.backup:
            flags.append("-overwrite_original")
        print(f"writing {len(payload)} {label}(s) with exiftool…")
        res = exiftool_batch(ctx, flags, [entry["path"] for entry, _, _ in plan])
        if res.returncode > 1:
            print((res.stdout or "") + (res.stderr or ""), file=sys.stderr)
            raise SystemExit(f"exiftool failed (exit {res.returncode})")
        combined = (res.stdout or "") + (res.stderr or "")
        m = re.search(r"(\d+) image files? updated", combined)
        if m:
            written = int(m.group(1))
        elif "Nothing to do" in combined:
            written = 0
        else:
            print(combined.strip(), file=sys.stderr)
            raise SystemExit("exiftool did not report an update count")
        for line in (res.stderr or "").splitlines():
            if re.match(r"^(Warning|Error)", line):
                warnings.append(line.strip())
        if written != len(payload):
            print(f"  note: exiftool updated {written} of {len(payload)} file(s); "
                  f"see warnings below", file=sys.stderr)
        return written, warnings
    finally:
        os.unlink(import_path)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Write Google Photos Takeout JSON sidecar metadata into photo/video files.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  %(prog)s --root ~/Downloads/Takeout/Google\\ Photos --dry-run
  %(prog)s --root ~/Downloads/Takeout/Google\\ Photos --tz +08:00
  %(prog)s --root ~/Downloads/Takeout/Google\\ Photos --overwrite
""",
    )
    ap.add_argument("--root", default=".", help="folder to scan recursively (default: .)")
    ap.add_argument("--tz", default="local",
                    help="time zone for captured timestamps: local|UTC|+08:00|Asia/Shanghai (default: local)")
    ap.add_argument("--overwrite", action="store_true",
                    help="replace existing dates/GPS/descriptions instead of only filling gaps")
    ap.add_argument("--dry-run", action="store_true", help="show what would be written, touch nothing")
    ap.add_argument("--backup", action="store_true",
                    help="keep exiftool's *_original copies instead of editing in place")
    ap.add_argument("--keep-file-times", action="store_true",
                    help="do not set file modify/create dates to the capture time")
    ap.add_argument("--no-description", action="store_true", help="do not write JSON descriptions")
    ap.add_argument("--report", metavar="FILE", help="write a JSON report of every decision")
    ap.add_argument("-v", "--verbose", action="store_true", help="list every file")
    args = ap.parse_args(argv)

    root = os.path.abspath(os.path.expanduser(args.root))
    if not os.path.isdir(root):
        raise SystemExit(f"--root is not a directory: {root}")

    exif = shutil.which("exiftool")
    if not exif:
        raise SystemExit("exiftool not found. Install it with:  brew install exiftool")

    ctx = Ctx()
    ctx.exif = exif
    ctx.tz = build_tz(args.tz)
    ctx.overwrite = args.overwrite
    ctx.dry_run = args.dry_run
    ctx.file_times = not args.keep_file_times
    ctx.description = not args.no_description
    ctx.backup = args.backup
    ctx.verbose = args.verbose

    print(f"scanning {root}")
    index = index_media(root)
    media_total = sum(len(v) for v in index.values())
    entries, albums, orphans, invalid = collect(root)
    print(f"  {media_total} media files, {len(entries) + len(orphans) + len(invalid)} sidecars, "
          f"{len(albums)} album/info JSON skipped")

    plan = []
    if entries:
        existing = read_existing(ctx, [e["path"] for e in entries])
        for entry in entries:
            rec = existing.get(entry["path"], {})
            if "Error" in rec:
                invalid.append((entry["path"], f"exiftool: {rec['Error']}"))
                continue
            tags = build_tags(ctx, entry, rec)
            if not tags:
                if ctx.verbose:
                    print(f"  keep    {entry['path']} (already complete)")
                continue
            plan.append((entry, tags, rec))

    written = 0
    warnings: list[str] = []
    if plan and not ctx.dry_run:
        written, warnings = apply_plan(ctx, plan)
    elif plan:
        for entry, tags, _ in plan:
            if ctx.verbose:
                print(f"  would   {entry['path']}")
                for key, value in tags.items():
                    print(f"            {key} = {value}")

    print()
    via_edited = sum(1 for entry in entries if entry.get("via") == "edited")
    via_unnumbered = sum(1 for entry in entries if entry.get("via") == "unnumbered")
    extra = []
    if via_edited:
        extra.append(f"{via_edited} via -edited copies")
    if via_unnumbered:
        extra.append(f"{via_unnumbered} via unnumbered media")
    print(f"paired sidecars : {len(entries)}"
          + (f" ({', '.join(extra)})" if extra else ""))
    print(f"to write        : {len(plan)}" + (f" ({written} written)" if not ctx.dry_run else " (dry run)"))
    print(f"already set     : {len(entries) - len(plan)} kept as-is")
    if orphans:
        print(f"unmatched JSON  : {len(orphans)}")
        for path, why in orphans[:10]:
            print(f"    {path}  [{why}]")
        if len(orphans) > 10:
            print(f"    … {len(orphans) - 10} more")
    if invalid:
        print(f"broken          : {len(invalid)}")
        for path, why in invalid[:10]:
            print(f"    {path}  [{why}]")
    if warnings:
        print(f"exiftool notes  : {len(warnings)}")
        for line in warnings[:15]:
            print(f"    {line}")
        if len(warnings) > 15:
            print(f"    … {len(warnings) - 15} more")

    if args.report:
        planned = {entry["path"] for entry, _, _ in plan}
        report = {
            "root": root,
            "tz": args.tz,
            "overwrite": ctx.overwrite,
            "dry_run": ctx.dry_run,
            "media_files": media_total,
            "paired": [{"media": e["path"], "json": e["json"], "via": e["via"], "tags": t}
                       for e, t, _ in plan],
            "kept": [e["path"] for e in entries if e["path"] not in planned],
            "album_json": albums,
            "orphans": [{"json": p, "reason": w} for p, w in orphans],
            "broken": [{"path": p, "reason": w} for p, w in invalid],
            "exiftool_warnings": warnings,
        }
        with open(os.path.expanduser(args.report), "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
        print(f"report          : {args.report}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
