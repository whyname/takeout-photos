#!/usr/bin/env python3
"""restore_from_zips.py — write Takeout metadata for a folder of zip parts, one part at a time.

Google splits a Takeout into ~2 GB parts and does *not* keep a media file and its
`<name>.supplemental-metadata.json` sidecar in the same part, so pairing only works
if every part is considered before anything is written. Unpacking 50 GB first is
usually impossible, so this front end never does:

  1. reads every part's central directory (no extraction) and parses the sidecars;
  2. pairs media with sidecars globally, using the same rules as
     restore_gp_metadata.py (exact / duplicate counter / `-edited` / unnumbered);
  3. processes one part at a time: extract into a small staging area, write the
     tags with exiftool, then file the media into one of two folders:

       <out>/待导入  — has a capture date (from the sidecar or the camera itself)
       <out>/待匹配  — no date yet; kept so a later part can supply its metadata

Only 待导入 should be dragged into Photos. When a later part brings the missing
sidecar, the next run writes it and moves the file to 待导入 automatically.

Peak extra disk = one part + whatever already landed in --out. Re-running resumes:
media already present in --out are skipped, so an interrupted 27-part job can be
started again with the same command.

Run it with no arguments: it finds the parts in ~/Downloads, unpacks them into
~/Pictures/Takeout in as many batches as the disk allows, keeps the sidecar cache
in ~/Library/Application Support/TakeoutFix/, and asks before deleting anything.

Place it on the Desktop or run it from anywhere:
  python3 restore_from_zips.py            # the only command you need
  python3 restore_from_zips.py --dry-run  # preview pairing and space
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import sys
import time
import zipfile
from collections import defaultdict

from datetime import datetime

from restore_gp_metadata import (
    MEDIA_EXTS,
    VIDEO_EXTS,
    Ctx,
    apply_plan,
    build_tags,
    build_tz,
    has_exact,
    has_tag,
    is_media,
    media_key,
    parse_formatted,
    parse_sidecar_name,
    parse_stamp,
    pair_in_dir,
    read_existing,
    sidecar_gps,
    sidecar_key,
    sidecar_timestamp,
)

GB = 1024 ** 3
CACHE_NAME = ".takeout-sidecars.json"

# Output is split so only media that carry a capture date get imported:
# everything else waits (media included) for a sidecar a later part may bring.
READY_DIR = "待导入"
PENDING_DIR = "待匹配"
MARKER_VERSION = "own-dates v2"
UNWRITABLE_NAME = ".takeout-unwritable.json"
BAD_CONTENT = re.compile(r"^Error: Not a valid .*looks more like")


def placed_at(out_dir, rel):
    """Path of an already-unpacked media file, wherever it currently sits."""
    for root in (READY_DIR, PENDING_DIR, ""):
        candidate = os.path.join(out_dir, root, rel) if root else os.path.join(out_dir, rel)
        if os.path.exists(candidate):
            return candidate
    return None


def failed_paths(warnings):
    """Paths exiftool complained about (`Warning: ... - /path/file.jpg`)."""
    paths = set()
    for line in warnings:
        match = re.search(r" - (/.*)$", line)
        if match:
            paths.add(match.group(1))
    return paths


def load_cache(path):
    """{dir: {sidecar_name: payload}} from earlier runs (kept for deleted parts)."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_cache(path, cache):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cache, fh, ensure_ascii=False)
    os.replace(tmp, path)


def prune_parts(zip_paths, media, out_dir):
    """Split parts into (redundant, kept).

    A part is redundant once every media file it holds already exists in --out;
    its sidecars are in the cache, so deleting it loses nothing. Metadata-only
    parts are redundant as soon as their sidecars are cached.
    """
    media_by_part = defaultdict(list)
    for (_, _), (zip_path, member, _size) in media.items():
        media_by_part[zip_path].append(member)
    redundant, kept = [], []
    for zip_path in zip_paths:
        members = media_by_part.get(zip_path, [])
        if all(placed_at(out_dir, out_rel(m)) for m in members):
            redundant.append(zip_path)
        else:
            kept.append(zip_path)
    return redundant, kept


def merge_cache(cache, sidecars):
    """Fold freshly scanned sidecars into the cache; returns the flat mapping."""
    for (directory, name), payload in sidecars.items():
        cache.setdefault(directory, {})[name] = payload
    return {(d, n): p for d, names in cache.items() for n, p in names.items()}


def scan_legacy(out_dir):
    """Media still sitting in the old flat layout, i.e. not yet in a bucket."""
    found = []
    for dirpath, dirnames, filenames in os.walk(out_dir):
        dirnames[:] = sorted(d for d in dirnames
                             if d not in (READY_DIR, PENDING_DIR) and not d.startswith("."))
        for name in filenames:
            if name.startswith(".") or not is_media(name):
                continue
            full = os.path.join(dirpath, name)
            found.append((full, os.path.relpath(full, out_dir)))
    return found


def cached_payload(cache, rel, media_name):
    """Sidecar data for an already-unpacked file, from the cache (its part may be gone)."""
    parts = [p for p in os.path.dirname(rel).split(os.sep) if p]
    key_dir = "/".join(["Takeout", "Google Photos", *parts])
    for side_name in cache.get(key_dir, {}):
        stem, dup = parse_sidecar_name(side_name)
        if stem is None or sidecar_key(stem, dup) != media_key(media_name):
            continue
        usable, _, _ = split_sidecars({(key_dir, side_name): cache[key_dir][side_name]})
        payload = usable.get((key_dir, side_name))
        if payload:
            return payload
    return None


def own_capture_ts(rec, tz):
    """Epoch of a file's own QuickTime/Apple date, for media that have no EXIF date."""
    for key in ("QuickTime:CreateDate", "Keys:CreationDate", "QuickTime:CreationDate"):
        value = rec.get(key)
        if not isinstance(value, str) or not value or value.startswith("0000"):
            continue
        ts = parse_stamp(value)                    # strings that carry an offset
        if ts is not None:
            return ts
        try:
            naive = datetime.strptime(value, "%Y:%m:%d %H:%M:%S")
        except ValueError:
            continue
        return int(naive.replace(tzinfo=tz).timestamp()) if tz else int(naive.timestamp())
    return None


def scan_bucket(out_dir, bucket):
    """Media inside one of the two output buckets."""
    root = os.path.join(out_dir, bucket)
    found = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if name.startswith(".") or not is_media(name):
                continue
            full = os.path.join(dirpath, name)
            found.append((full, os.path.relpath(full, root)))
    return found


def prune_empty_dirs(out_dir, keep):
    """Drop the now-empty `Photos from 20xx/` shells left behind by a re-layout."""
    for dirpath, _dirnames, _filenames in os.walk(out_dir, topdown=False):
        if os.path.abspath(dirpath) == os.path.abspath(out_dir):
            continue
        first = os.path.relpath(dirpath, out_dir).split(os.sep)[0]
        if first in keep:
            continue
        try:
            if os.listdir(dirpath):
                continue
            os.rmdir(dirpath)
        except OSError:
            continue


def out_rel(member: str) -> str:
    """Flatten a zip member path to `Archive/...`, `Photos from 2019/...`."""
    parts = member.split("/")
    lowered = [p.lower() for p in parts]
    if "google photos" in lowered:
        parts = parts[lowered.index("google photos") + 1:]
    elif parts and parts[0].lower() == "takeout":
        parts = parts[1:]
    return "/".join(p for p in parts if p)


def scan_parts(zip_paths):
    """Index every part from its central directory.

    Returns (media, sidecars, media_bytes, skipped) where
      media    {(dir, name): (zip_path, member, size)}
      sidecars {(dir, name): payload}   ('__error__' marks an unreadable sidecar)
    """
    media, sidecars, skipped = {}, {}, []
    total = 0
    for zip_path in zip_paths:
        try:
            with zipfile.ZipFile(zip_path) as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    member = info.filename
                    name = os.path.basename(member)
                    if not name or name.startswith("."):
                        continue
                    key = (os.path.dirname(member), name)
                    if name.lower().endswith(".json"):
                        if key in sidecars:
                            continue          # same sidecar in two parts
                        try:
                            with zf.open(info) as fh:
                                payload = json.load(io.TextIOWrapper(fh, "utf-8-sig"))
                        except (ValueError, OSError) as exc:
                            payload = {"__error__": str(exc)}
                        if not isinstance(payload, dict):
                            payload = {"__error__": "not a JSON object"}
                        sidecars[key] = payload
                    elif os.path.splitext(name)[1].lower() in MEDIA_EXTS:
                        if key in media:
                            continue          # same media in two parts
                        media[key] = (zip_path, member, info.file_size)
                        total += info.file_size
        except (zipfile.BadZipFile, OSError) as exc:
            skipped.append((os.path.basename(zip_path), str(exc)))
    return media, sidecars, total, skipped


def split_sidecars(sidecars):
    """Drop album/info JSON and unreadable ones; return (usable, albums, broken)."""
    usable, albums, broken = {}, [], []
    for key, payload in sidecars.items():
        if "__error__" in payload:
            broken.append((key, payload["__error__"]))
            continue
        ts = sidecar_timestamp(payload)
        if ts is None:
            for block in (payload.get("photoTakenTime"), payload.get("creationTime")):
                if isinstance(block, dict):
                    ts = parse_formatted(block)
                    if ts:
                        break
        if ts is None:
            albums.append(key)
            continue
        payload["ts"] = ts
        usable[key] = payload
    return usable, albums, broken


def plan_pairs(media, sidecars):
    """Global pairing across all parts: {(dir, media_name): (sidecar_name, via)}."""
    media_by_dir, sidecar_by_dir = defaultdict(list), defaultdict(list)
    for directory, name in media:
        media_by_dir[directory].append(name)
    for directory, name in sidecars:
        sidecar_by_dir[directory].append(name)

    pairs, unpaired = {}, []
    for directory in sorted(set(media_by_dir) | set(sidecar_by_dir)):
        matched, unmatched = pair_in_dir(media_by_dir[directory],
                                         sidecar_by_dir[directory])
        for json_name, media_name, via in matched:
            pairs[(directory, media_name)] = (json_name, via)
        unpaired.extend((directory, name, why) for name, why in unmatched)
    return pairs, unpaired


def entry_for(payload, path, rel, via):
    return {
        "path": path,
        "json": None,
        "rel": rel,
        "ts": payload["ts"],
        "gps": sidecar_gps(payload),
        "description": (payload.get("description") or "").strip() or None,
        "video": os.path.splitext(path)[1].lower() in VIDEO_EXTS,
        "via": via,
    }


def free_bytes(path):
    probe = path
    while not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    return shutil.disk_usage(probe).free


STAGING_MARGIN = 256 * 1024 * 1024


def default_cache_path(out_dir):
    override = os.environ.get("TAKEOUT_CACHE")
    if override:
        return os.path.expanduser(override)
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/TakeoutFix/sidecars.json")
    return os.path.join(out_dir, CACHE_NAME)


def autodetect_zips():
    """Folder with the biggest pile of takeout parts, where people usually leave them."""
    candidates = [os.path.expanduser(p) for p in
                  ("~/Downloads", "~/Desktop", "~/Downloads/Takeout", "~/Takeout")]
    best = None
    for folder in candidates:
        if not os.path.isdir(folder):
            continue
        try:
            names = [n for n in os.listdir(folder) if n.lower().endswith(".zip")]
        except OSError:
            continue
        takeout = [n for n in names if "takeout" in n.lower()]
        names = takeout or names
        if not names:
            continue
        total = sum(os.path.getsize(os.path.join(folder, n)) for n in names)
        if best is None or total > best[1]:
            best = (folder, total, len(names))
    return best


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="把 Google Takeout 分卷解出来，并把 JSON 元数据写进照片/视频本身，"
                    "直接拖进苹果“照片”即可。不带参数就是推荐用法。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""常用：
  %(prog)s                 自动找分卷、自动分批、空间不够时询问是否清理
  %(prog)s --dry-run       只看配对率和空间需求，不动任何文件
  %(prog)s --clean         处理完自动删除已完全解压的分卷（释放空间）
""",
    )
    ap.add_argument("--zips", help="分卷所在文件夹（默认自动在“下载/桌面”里找 takeout-*.zip）")
    ap.add_argument("--out", help="解出来的媒体放哪（默认 ~/Pictures/Takeout）")
    ap.add_argument("--staging", default=None,
                    help="单卷暂存目录（默认 <out>/.staging，同一磁盘，移动即改名）")
    ap.add_argument("--cache", default=None, metavar="FILE",
                    help="sidecar 缓存位置（默认 ~/Library/Application Support/TakeoutFix/）")
    ap.add_argument("--clean", "--prune-zips", "--delete-zips", dest="clean",
                    action="store_true",
                    help="删除已完全解压的分卷：媒体已在输出目录、元数据已在缓存，删了不丢东西")
    ap.add_argument("--cache-only", action="store_true",
                    help="只把各卷的 sidecar 收进缓存，不解压媒体（手动清理分卷前的保险步骤）")
    ap.add_argument("--tz", default="local",
                    help="抓拍时间的时区：local|UTC|+08:00|Asia/Shanghai（默认本机时区）")
    ap.add_argument("--overwrite", action="store_true",
                    help="用 sidecar 的时间/位置覆盖文件里已有的值（默认只补空缺）")
    ap.add_argument("--no-description", action="store_true", help="不写入 JSON 里的描述")
    ap.add_argument("--keep-file-times", action="store_true", help="不改文件修改时间")
    ap.add_argument("--skip-unpaired", action="store_true",
                    help="没有 sidecar 的媒体不解出来（省空间，但那些照片不会被导入）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只索引和配对，不解压、不写入")
    ap.add_argument("--report", metavar="FILE", help="输出 JSON 报告")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    if args.zips:
        zips_dir = os.path.abspath(os.path.expanduser(args.zips))
    else:
        detected = autodetect_zips()
        if not detected:
            raise SystemExit("没找到 Takeout 分卷（takeout-*.zip）。放到 ~/Downloads 后重试，"
                             "或用 --zips 指定目录。")
        zips_dir = detected[0]
    if not os.path.isdir(zips_dir):
        raise SystemExit(f"分卷目录不存在: {zips_dir}")
    out_dir = os.path.abspath(os.path.expanduser(args.out or "~/Pictures/Takeout"))
    if not args.dry_run:
        os.makedirs(out_dir, exist_ok=True)

    ctx = Ctx()
    exiftool = shutil.which("exiftool")
    if not exiftool and not args.dry_run:
        raise SystemExit("exiftool not found. Install it with:  brew install exiftool")
    ctx.exif = exiftool or "exiftool"
    ctx.tz = build_tz(args.tz)
    ctx.overwrite = args.overwrite
    ctx.description = not args.no_description
    ctx.file_times = not args.keep_file_times
    ctx.verbose = args.verbose

    zip_paths = sorted(os.path.join(zips_dir, n) for n in os.listdir(zips_dir)
                       if n.lower().endswith(".zip"))
    if not zip_paths:
        raise SystemExit(f"{zips_dir} 里没有 .zip 分卷")
    cache_path = os.path.abspath(os.path.expanduser(args.cache or default_cache_path(out_dir)))
    staging = os.path.abspath(os.path.expanduser(args.staging or os.path.join(out_dir, ".staging")))

    print(f"分卷目录 : {zips_dir}  ({len(zip_paths)} 个)")
    print(f"输出目录 : {out_dir}")
    print(f"元数据缓存: {cache_path}")
    media, sidecars, media_bytes, skipped = scan_parts(zip_paths)
    for name, why in skipped:
        print(f"  ! {name} 读不了（{why}），已跳过")
    cache = load_cache(cache_path)
    cached = sum(len(v) for v in cache.values())
    if cached:
        print(f"  已从缓存恢复 {cached} 条元数据（之前删掉的分卷）")
    sidecars = merge_cache(cache, sidecars)
    usable, albums, broken = split_sidecars(sidecars)
    pairs, unpaired = plan_pairs(media, usable)
    print(f"  {len(media)} 个媒体（{media_bytes / GB:.1f} GB）, {len(usable)} 条元数据, "
          f"{len(pairs)} 条配对成功")

    skipped_names = {s[0] for s in skipped}

    def reclaimable_parts():
        remaining = [z for z in zip_paths if os.path.exists(z)
                     and os.path.basename(z) not in skipped_names]
        return prune_parts(remaining, media, out_dir)

    redundant, kept = reclaimable_parts()
    print(f"  {len(redundant)} 个分卷已完全解压（可安全删除）, {len(kept)} 个还有媒体没解出")

    if args.dry_run or args.cache_only:
        if not args.dry_run:
            save_cache(cache_path, cache)
            print(f"  缓存已保存: {sum(len(v) for v in cache.values())} 条 -> {cache_path}")
        print("\n== 只预演，不解压 ==" if args.dry_run else "\n== 只收元数据，不解压 ==")
        print(f"  可写标签   : {len(media) - sum(1 for k in media if pairs.get(k) is None)}"
              f"（进 {READY_DIR}）")
        print(f"  无元数据   : {sum(1 for k in media if pairs.get(k) is None)}"
              f"（进 {PENDING_DIR}，已有相机 EXIF 的除外）")
        print(f"  孤儿元数据 : {len(unpaired)}")
        print(f"  可删分卷   : {len(redundant)}")
        legacy = scan_legacy(out_dir)
        if legacy:
            cached_n = sum(1 for _, rel in legacy
                           if cached_payload(cache, rel, os.path.basename(rel)))
            print(f"  旧结构待归位: {len(legacy)} 个（其中 {cached_n} 个已有缓存元数据）")
        if args.clean:
            freed = sum(os.path.getsize(z) for z in redundant)
            for zip_path in redundant:
                if not args.dry_run:
                    os.unlink(zip_path)
            print(f"  {'将释放' if args.dry_run else '已释放'}   : {freed / GB:.1f} GB")
        for directory, name, why in unpaired[:5]:
            print(f"    {os.path.join(directory, name)}  [{why}]")
        if len(unpaired) > 5:
            print(f"    … 另外 {len(unpaired) - 5} 条")
        return 0

    unwritable_path = os.path.join(out_dir, UNWRITABLE_NAME)
    try:
        with open(unwritable_path, encoding="utf-8") as fh:
            unwritable = set(json.load(fh))
    except (OSError, ValueError):
        unwritable = set()

    work = defaultdict(list)
    existing_items = []          # already on disk: (path, rel, payload, via)
    collisions = missing = 0
    for key, (zip_path, member, size) in media.items():
        rel = out_rel(member)
        if not rel:
            continue
        pair = pairs.get(key)
        payload = usable[(key[0], pair[0])] if pair is not None else None
        via = pair[1] if pair is not None else None
        ready_now = os.path.join(out_dir, READY_DIR, rel)
        if os.path.exists(ready_now):
            existing_items.append((ready_now, rel, payload, via))
            continue
        current = placed_at(out_dir, rel)   # 待匹配, or left behind by an older run
        if current:
            existing_items.append((current, rel, payload, via))
            continue
        if payload is None:
            missing += 1
            if args.skip_unpaired:
                continue
        work[zip_path].append((member, rel, key, size, payload, via))

    todo_bytes = sum(item[3] for items in work.values() for item in items)
    free = min(free_bytes(out_dir), free_bytes(staging))
    print(f"  需要写入 {todo_bytes / GB:.1f} GB，当前可用 {free / GB:.1f} GB"
          + ("（不够会自动分批并询问清理）" if todo_bytes > free else ""))

    save_cache(cache_path, cache)

    if os.path.isdir(staging) and os.listdir(staging):
        print(f"clearing leftover staging {staging}")
        shutil.rmtree(staging)

    started = time.time()
    written = moved = already = relocated = 0
    warnings: list[str] = []
    processed = []

    def importable(rec, payload):
        """Only files that end up with a capture date are offered for import."""
        return payload is not None or has_exact(
            rec, "ExifIFD:DateTimeOriginal", "ExifIFD:CreateDate"
        ) or has_tag(rec, "CreateDate", "CreationDate")

    def settle(states):
        """Move tagged media into 待导入 and the rest into 待匹配."""
        nonlocal relocated, already
        for src, rel, ok_to_import in states:
            root = READY_DIR if ok_to_import else PENDING_DIR
            dest = os.path.join(out_dir, root, rel)
            if os.path.abspath(dest) == os.path.abspath(src) or os.path.exists(dest):
                already += 1
                continue
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.move(src, dest)
            relocated += 1

    def tag_all(pairs_list):
        """pairs_list: [(path, rel, payload, via)] -> (states, failed_paths)."""
        nonlocal written
        recs = read_existing(ctx, [src for src, *_ in pairs_list])
        plan, states = [], []
        for src, rel, payload, via in pairs_list:
            rec = recs.get(src, {})
            ok = "Error" not in rec
            if not ok:
                warnings.append(f"{rel}: {rec['Error']}")
                states.append((src, rel, False))
                continue
            if payload is not None and rel in unwritable:
                states.append((src, rel, False))
                continue
            if payload is not None:
                entry = entry_for(payload, src, rel, via)
                tags = build_tags(ctx, entry, rec)
                if tags:
                    plan.append((entry, tags, rec))
            elif not has_exact(rec, "ExifIFD:DateTimeOriginal", "ExifIFD:CreateDate"):
                # HEIC/MOV from Takeout keep their date in QuickTime/Apple keys only;
                # mirror it into EXIF (images) and pin the file dates so Photos and
                # Finder both stop showing "today".
                ts = own_capture_ts(rec, ctx.tz)
                if ts:
                    entry = {"path": src, "rel": rel, "ts": ts, "gps": None,
                             "description": None, "via": "self",
                             "video": os.path.splitext(src)[1].lower() in VIDEO_EXTS}
                    tags = build_tags(ctx, entry, rec)
                    if tags:
                        plan.append((entry, tags, rec))
            else:
                # Date already in EXIF (or only file times left to fix): let
                # build_tags pin the file dates to that capture time.
                entry = {"path": src, "rel": rel, "ts": 0, "gps": None,
                         "description": None, "via": "self",
                         "video": os.path.splitext(src)[1].lower() in VIDEO_EXTS}
                tags = build_tags(ctx, entry, rec)
                if tags:
                    plan.append((entry, tags, rec))
            states.append((src, rel, importable(rec, payload)))
        failed = set()
        if plan:
            got, warns = apply_plan(ctx, plan)
            written += got
            warnings.extend(warns)
            failed = failed_paths(warns)
            rel_of = {os.path.abspath(src): rel for src, rel, *_ in pairs_list}
            for line in warns:
                if BAD_CONTENT.match(line):
                    bad = failed_paths([line])
                    for path in bad:
                        if path in rel_of:
                            unwritable.add(rel_of[path])
            if got != len(plan):
                print(f"  ! {len(plan) - got} 个文件写标签失败，留在待匹配", flush=True)
        return [(src, rel, ok and src not in failed) for src, rel, ok in states]

    def file_away(items):
        """Files already on disk: tag if a sidecar showed up, then re-file them."""
        for start in range(0, len(items), 500):
            settle(tag_all(items[start:start + 500]))

    if existing_items:
        print(f"整理 {len(existing_items)} 个已在磁盘上的媒体（补写/归位）…", flush=True)
        file_away(existing_items)

    def process_part(zip_path, items):
        nonlocal moved
        name = os.path.basename(zip_path)
        t0 = time.time()
        part_bytes = sum(item[3] for item in items)
        print(f"  {name}: {len(items)} 个文件, {part_bytes / GB:.2f} GB", flush=True)
        os.makedirs(staging, exist_ok=True)
        with zipfile.ZipFile(zip_path) as zf:
            for member, rel, key, size, payload, via in items:
                dest = os.path.join(staging, rel)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with zf.open(member) as src, open(dest, "wb") as dst:
                    shutil.copyfileobj(src, dst, 1 << 20)

        states = tag_all([(os.path.join(staging, rel), rel, payload, via)
                          for member, rel, key, size, payload, via in items])
        before = relocated
        settle(states)
        moved += relocated - before
        shutil.rmtree(staging, ignore_errors=True)
        processed.append({"zip": name, "files": len(items), "bytes": part_bytes,
                          "tagged": len(states), "seconds": round(time.time() - t0, 1)})
        print(f"    已归档 {moved} 个，已写入标签 {written} 个（本卷 {time.time() - t0:.0f}s）",
              flush=True)

    skipped_names = {s[0] for s in skipped}

    def reclaimable_parts():
        remaining = [z for z in zip_paths if os.path.exists(z)
                     and os.path.basename(z) not in skipped_names]
        return prune_parts(remaining, media, out_dir)

    pending = [z for z in zip_paths if work.get(z)]
    asked = False
    pruned: list[str] = []

    def reclaim(allow_prompt: bool) -> bool:
        """Delete parts that are fully unpacked; returns True if space was freed."""
        nonlocal asked
        redundant, kept = reclaimable_parts()
        if not redundant:
            return False
        freed = sum(os.path.getsize(z) for z in redundant)
        if args.clean:
            allowed = True
        elif not allow_prompt or asked or not sys.stdin.isatty():
            return False
        else:
            asked = True
            need = sum(sum(i[3] for i in work[z]) for z in pending) / GB
            print(f"\n磁盘剩余 {free_bytes(out_dir) / GB:.1f} GB，后面还需要约 {need:.1f} GB。")
            print(f"{len(redundant)} 个分卷已完全解压（媒体在输出目录、元数据已在缓存），"
                  f"删除可释放 {freed / GB:.1f} GB：")
            for zip_path in redundant[:5]:
                print(f"    {os.path.basename(zip_path)}")
            if len(redundant) > 5:
                print(f"    … 另外 {len(redundant) - 5} 个")
            allowed = input("现在删除它们？[Y/n] ").strip().lower() in ("", "y", "yes")
        if not allowed:
            return False
        for zip_path in redundant:
            try:
                os.unlink(zip_path)
                pruned.append(os.path.basename(zip_path))
            except OSError as exc:
                print(f"  无法删除 {zip_path}: {exc}", file=sys.stderr)
        print(f"  已删除 {len(redundant)} 个分卷，释放 {freed / GB:.1f} GB", flush=True)
        return True

    while pending:
        progressed = False
        for zip_path in list(pending):
            items = work[zip_path]
            need = sum(item[3] for item in items) + STAGING_MARGIN
            if need > min(free_bytes(out_dir), free_bytes(staging)):
                continue                      # try to fit a smaller part instead
            process_part(zip_path, items)
            pending.remove(zip_path)
            progressed = True
            if args.clean:
                try:
                    os.unlink(zip_path)
                    pruned.append(os.path.basename(zip_path))
                except OSError as exc:
                    print(f"  无法删除 {os.path.basename(zip_path)}: {exc}",
                          file=sys.stderr)
        if not pending:
            break
        if not reclaim(allow_prompt=True):
            break

    if args.clean:
        reclaim(allow_prompt=False)      # e.g. a metadata-only part left after this run

    leftovers = scan_legacy(out_dir)
    handled = {os.path.abspath(src) for src, *_ in existing_items}
    # Files whose part is already deleted still deserve a second look: a sidecar
    # may have arrived meanwhile, and old runs left QuickTime-only dates unwritten.
    marker = os.path.join(out_dir, ".takeout-own-dates")
    revisit = leftovers + scan_bucket(out_dir, PENDING_DIR)
    try:
        marker_state = open(marker, encoding="utf-8").read().strip()
    except OSError:
        marker_state = ""
    if marker_state != MARKER_VERSION:
        revisit += scan_bucket(out_dir, READY_DIR)
    revisit = [(full, rel) for full, rel in revisit if os.path.abspath(full) not in handled]
    if revisit:
        print(f"复查 {len(revisit)} 个已归档文件（补写/归位）…", flush=True)
        file_away([(full, rel, cached_payload(cache, rel, os.path.basename(rel)), None)
                   for full, rel in revisit])
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write(MARKER_VERSION + "\n")
    prune_empty_dirs(out_dir, {READY_DIR, PENDING_DIR, ".staging"})
    if unwritable:
        with open(unwritable_path, "w", encoding="utf-8") as fh:
            json.dump(sorted(unwritable), fh, ensure_ascii=False, indent=1)

    if pending:
        print(f"\n还剩 {len(pending)} 个分卷没处理（磁盘不够）。")
        print(f"先导入 {READY_DIR}，删掉它腾出空间，再运行同一条命令继续。")

    def count_files(root):
        return sum(len(files) for _, _, files in os.walk(root)) if os.path.isdir(root) else 0

    ready_n = count_files(os.path.join(out_dir, READY_DIR))
    pending_n = count_files(os.path.join(out_dir, PENDING_DIR))

    print()
    print(f"已完成分卷 : {len(processed)}/{len(zip_paths)}"
          + (f"（{len(skipped)} 个读不了已跳过）" if skipped else ""))
    print(f"可导入     : {ready_n} 个 -> {os.path.join(out_dir, READY_DIR)}")
    print(f"待匹配     : {pending_n} 个 -> {os.path.join(out_dir, PENDING_DIR)}"
          f"（等后续分卷的元数据，先别导入）")
    print(f"新解出     : {moved} 个")
    print(f"补写归位   : {relocated} 个")
    print(f"写入标签   : {written} 个")
    print(f"已有跳过   : {collisions + already} 个")
    if args.skip_unpaired and missing:
        print(f"暂不解出   : {missing} 个（无元数据，留在分卷里）")
    print(f"孤儿元数据 : {len(unpaired)} 条（导出里没有对应媒体）")
    if unwritable:
        print(f"无法写入   : {len(unwritable)} 个（后缀与内容不符，已在待匹配里，不再重试）")
    if pruned:
        print(f"已删分卷   : {len(pruned)} 个")
    if broken:
        print(f"损坏元数据 : {len(broken)} 条")
    if warnings:
        print(f"exiftool 提示 {len(warnings)} 条（不影响导入）")
        for line in warnings[:5]:
            print(f"    {line}")
    print(f"耗时       : {time.time() - started:.0f}s")
    print()
    print(f"下一步：只把“{READY_DIR}”拖进“照片”App；“{PENDING_DIR}”保持不动。")
    print(f"导入确认成功后，可以删掉 {READY_DIR} 腾空间。")
    if pending_n:
        print(f"等全部 27 个分卷到齐后再跑一次同一命令，{PENDING_DIR} 里能配上元数据的"
              f"会自动写标签并移到 {READY_DIR}。")
    elif not pending:
        print("全部处理完毕。")

    if args.report:
        report = {
            "zips_dir": zips_dir,
            "out_dir": out_dir,
            "staging": staging,
            "cache": cache_path,
            "tz": args.tz,
            "parts": processed,
            "skipped_parts": skipped,
            "pruned_parts": pruned,
            "pending_parts": [os.path.basename(z) for z in pending],
            "media_total": len(media),
            "media_bytes": media_bytes,
            "paired": len(pairs),
            "ready_files": ready_n,
            "pending_files": pending_n,
            "unpaired_json": [
                {"json": os.path.join(d, n), "reason": why}
                for d, n, why in unpaired
            ],
            "broken_sidecars": [
                {"json": os.path.join(d, n), "reason": why}
                for (d, n), why in broken
            ],
            "moved": moved,
            "relocated": relocated,
            "tagged": written,
            "already_present": collisions + already,
            "media_without_sidecar": missing,
            "warnings": warnings,
        }
        with open(os.path.expanduser(args.report), "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
        print(f"报告       : {args.report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
