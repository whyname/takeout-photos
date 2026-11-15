"""End-to-end tests for restore_from_zips.py (media and sidecar in different zip parts)."""

import base64
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

import restore_from_zips as Z  # noqa: E402
import restore_gp_metadata as R  # noqa: E402

EXIFTOOL = shutil.which("exiftool")
JPEG_1PX = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0a"
    "HBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAABAAAAAAAA"
    "AAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AVN//2Q=="
)
TS = 1565605931          # 2019-08-12 10:32:11 UTC


def sidecar(ts=TS, lat=0.0, lon=0.0, desc=""):
    geo = {"latitude": lat, "longitude": lon, "altitude": 0.0}
    return json.dumps({
        "description": desc,
        "photoTakenTime": {"timestamp": str(ts)},
        "creationTime": {"timestamp": str(ts)},
        "geoData": geo,
        "geoDataExif": geo,
    }).encode()


def read_tags(path, *names):
    args = [EXIFTOOL, "-json", "-G1", "-n", "-charset", "filename=UTF8", *names, str(path)]
    return json.loads(subprocess.run(args, capture_output=True, text=True, check=True).stdout)[0]


@unittest.skipUnless(EXIFTOOL, "exiftool is required")
class ZipFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="gpzips-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.zips = self.tmp / "zips"
        self.out = self.tmp / "out"
        # keep the per-user cache out of the real Application Support folder
        os.environ["TAKEOUT_CACHE"] = str(self.tmp / "cache.json")
        self.addCleanup(os.environ.pop, "TAKEOUT_CACHE", None)
        self.zips.mkdir()

        # media only, plus an orphan that has no sidecar anywhere
        with zipfile.ZipFile(self.zips / "takeout-1-001.zip", "w") as zf:
            zf.writestr("Takeout/Google Photos/Photos from 2019/IMG_1.JPG", JPEG_1PX)
            zf.writestr("Takeout/Google Photos/Photos from 2019/IMG_9.JPG", JPEG_1PX)

        # sidecars for part 1's media live here; also a same-part media+sidecar pair
        with zipfile.ZipFile(self.zips / "takeout-1-002.zip", "w") as zf:
            zf.writestr("Takeout/Google Photos/Photos from 2019/IMG_1.JPG.supplemental-metadata.json",
                        sidecar(lat=31.2304, lon=121.4737, desc="跨分卷"))
            zf.writestr("Takeout/Google Photos/Photos from 2020/IMG_2-edited.JPG", JPEG_1PX)
            zf.writestr("Takeout/Google Photos/Photos from 2020/IMG_2.JPG.supplemental-metadata.json",
                        sidecar())
            zf.writestr("Takeout/Google Photos/Photos from 2020/2020.json",
                        json.dumps({"title": "2020", "date": {"timestamp": str(TS)}}).encode())

    def test_pairs_across_parts_and_keeps_orphans(self):
        Z.main(["--zips", str(self.zips), "--out", str(self.out), "--tz", "+08:00"])

        cross = self.out / Z.READY_DIR / "Photos from 2019" / "IMG_1.JPG"
        self.assertTrue(cross.is_file())
        tags = read_tags(cross, "-EXIF:DateTimeOriginal", "-EXIF:GPSLatitude", "-XMP-dc:Description")
        self.assertEqual(tags["ExifIFD:DateTimeOriginal"], "2019:08:12 18:32:11")
        self.assertAlmostEqual(float(tags["GPS:GPSLatitude"]), 31.2304, places=4)
        self.assertEqual(tags["XMP-dc:Description"], "跨分卷")

        # -edited fallback for a sidecar whose original was never exported
        edited = self.out / Z.READY_DIR / "Photos from 2020" / "IMG_2-edited.JPG"
        self.assertIn("ExifIFD:DateTimeOriginal", read_tags(edited, "-EXIF:DateTimeOriginal"))

        # media without any sidecar is still unpacked, untouched
        orphan = self.out / Z.PENDING_DIR / "Photos from 2019" / "IMG_9.JPG"
        self.assertTrue(orphan.is_file())
        self.assertEqual(read_tags(orphan, "-EXIF:DateTimeOriginal"),
                         {"SourceFile": str(orphan)})

        # album JSON is never unpacked, staging is cleaned up
        self.assertFalse((self.out / Z.READY_DIR / "Photos from 2020" / "2020.json").exists())
        self.assertFalse(list(self.out.rglob("*.supplemental-metadata.json")))
        self.assertFalse((self.out / ".staging").exists())

    def test_second_run_resumes_without_reprocessing(self):
        Z.main(["--zips", str(self.zips), "--out", str(self.out), "--tz", "+08:00"])
        out = io.StringIO()
        real = sys.stdout
        sys.stdout = out
        try:
            Z.main(["--zips", str(self.zips), "--out", str(self.out), "--tz", "+08:00"])
        finally:
            sys.stdout = real
        self.assertIn("写入标签   : 0", out.getvalue())
        # nothing moved or duplicated on a resume
        self.assertTrue((self.out / Z.READY_DIR / "Photos from 2019" / "IMG_1.JPG").is_file())
        self.assertTrue((self.out / Z.PENDING_DIR / "Photos from 2019" / "IMG_9.JPG").is_file())
        self.assertFalse((self.out / Z.PENDING_DIR / "Photos from 2019" / "IMG_1.JPG").exists())

    def test_skip_unpaired_leaves_orphans_behind(self):
        Z.main(["--zips", str(self.zips), "--out", str(self.out), "--tz", "+08:00",
                "--skip-unpaired"])
        self.assertTrue((self.out / Z.READY_DIR / "Photos from 2019" / "IMG_1.JPG").is_file())
        self.assertFalse((self.out / Z.READY_DIR / "Photos from 2019" / "IMG_9.JPG").exists())

    def test_dry_run_extracts_nothing(self):
        Z.main(["--zips", str(self.zips), "--out", str(self.out), "--dry-run"])
        self.assertFalse(self.out.exists())        # dry run creates nothing at all

    def test_later_batch_backfills_media_already_moved(self):
        """Parts arrive over days: re-running must tag what is already in --out."""
        media_only = self.tmp / "zips_second"
        media_only.mkdir()
        shutil.move(str(self.zips / "takeout-1-002.zip"), str(media_only / "b.zip"))
        Z.main(["--zips", str(self.zips), "--out", str(self.out), "--tz", "+08:00"])
        waiting = self.out / Z.PENDING_DIR / "Photos from 2019" / "IMG_1.JPG"
        self.assertTrue(waiting.is_file())
        self.assertNotIn("ExifIFD:DateTimeOriginal",
                         read_tags(waiting, "-EXIF:DateTimeOriginal"))

        shutil.move(str(media_only / "b.zip"), str(self.zips / "takeout-1-002.zip"))
        Z.main(["--zips", str(self.zips), "--out", str(self.out), "--tz", "+08:00"])
        promoted = self.out / Z.READY_DIR / "Photos from 2019" / "IMG_1.JPG"
        self.assertTrue(promoted.is_file(), "补写后应移入待导入")
        self.assertFalse(waiting.exists())
        self.assertEqual(read_tags(promoted, "-EXIF:DateTimeOriginal")["ExifIFD:DateTimeOriginal"],
                         "2019:08:12 18:32:11")
        self.assertTrue((self.out / Z.READY_DIR / "Photos from 2020" / "IMG_2-edited.JPG").is_file())

    def test_sidecar_cache_survives_deleted_parts(self):
        """Metadata parts can be deleted first; media arriving later still gets tagged."""
        zb = self.tmp / "zb"
        zb.mkdir()
        with zipfile.ZipFile(zb / "a-sidecars.zip", "w") as zf:
            zf.writestr("Takeout/Google Photos/Photos from 2019/IMG_7.JPG"
                        ".supplemental-metadata.json", sidecar(lat=10.0, lon=20.0))
        out = self.tmp / "out_b"

        Z.main(["--zips", str(zb), "--out", str(out), "--tz", "+08:00", "--delete-zips"])
        self.assertFalse((zb / "a-sidecars.zip").exists())
        self.assertTrue(Path(os.environ["TAKEOUT_CACHE"]).is_file())

        with zipfile.ZipFile(zb / "b-media.zip", "w") as zf:
            zf.writestr("Takeout/Google Photos/Photos from 2019/IMG_7.JPG", JPEG_1PX)
        Z.main(["--zips", str(zb), "--out", str(out), "--tz", "+08:00"])

        tags = read_tags(out / Z.READY_DIR / "Photos from 2019" / "IMG_7.JPG",
                         "-EXIF:DateTimeOriginal", "-EXIF:GPSLatitude")
        self.assertEqual(tags["ExifIFD:DateTimeOriginal"], "2019:08:12 18:32:11")
        self.assertAlmostEqual(float(tags["GPS:GPSLatitude"]), 10.0, places=4)

    def test_prune_and_external_cache_support_rolling_disk(self):
        """Download a batch -> process -> prune -> import -> wipe --out -> next batch."""
        zp = self.tmp / "zp"
        zp.mkdir()
        cache = self.tmp / "cache.json"
        out = self.tmp / "out_p"
        with zipfile.ZipFile(zp / "p1.zip", "w") as zf:
            zf.writestr("Takeout/Google Photos/Photos from 2019/IMG_11.JPG", JPEG_1PX)
        with zipfile.ZipFile(zp / "p2.zip", "w") as zf:      # metadata-only part
            zf.writestr("Takeout/Google Photos/Photos from 2019/IMG_11.JPG"
                        ".supplemental-metadata.json", sidecar())
            zf.writestr("Takeout/Google Photos/Photos from 2019/IMG_12.JPG"
                        ".supplemental-metadata.json", sidecar(lat=10.0, lon=20.0))

        Z.main(["--zips", str(zp), "--out", str(out), "--cache", str(cache), "--tz", "+08:00"])
        self.assertTrue((out / Z.READY_DIR / "Photos from 2019" / "IMG_11.JPG").is_file())

        Z.main(["--zips", str(zp), "--out", str(out), "--cache", str(cache), "--prune-zips"])
        self.assertEqual(list(zp.glob("*.zip")), [])
        self.assertTrue(cache.is_file())

        # user imports into Photos, then frees the staging volume entirely
        shutil.rmtree(out)
        with zipfile.ZipFile(zp / "p3.zip", "w") as zf:
            zf.writestr("Takeout/Google Photos/Photos from 2019/IMG_12.JPG", JPEG_1PX)
        Z.main(["--zips", str(zp), "--out", str(out), "--cache", str(cache), "--tz", "+08:00"])

        tags = read_tags(out / Z.READY_DIR / "Photos from 2019" / "IMG_12.JPG",
                         "-EXIF:DateTimeOriginal", "-EXIF:GPSLatitude")
        self.assertEqual(tags["ExifIFD:DateTimeOriginal"], "2019:08:12 18:32:11")
        self.assertAlmostEqual(float(tags["GPS:GPSLatitude"]), 10.0, places=4)

    def test_prune_keeps_parts_with_unpacked_media(self):
        zp = self.tmp / "zq"
        zp.mkdir()
        with zipfile.ZipFile(zp / "q1.zip", "w") as zf:
            zf.writestr("Takeout/Google Photos/Photos from 2019/IMG_21.JPG", JPEG_1PX)
        out = self.tmp / "out_q"
        # no sidecar anywhere, and unpaired media are skipped -> nothing unpacked
        Z.main(["--zips", str(zp), "--out", str(out), "--skip-unpaired", "--tz", "+08:00"])
        Z.main(["--zips", str(zp), "--out", str(out), "--skip-unpaired",
                "--prune-zips", "--tz", "+08:00"])
        self.assertEqual(len(list(zp.glob("*.zip"))), 1)

    def test_cache_only_harvests_then_prunes(self):
        zp = self.tmp / "zr"
        zp.mkdir()
        cache = self.tmp / "cache_r.json"
        with zipfile.ZipFile(zp / "r1.zip", "w") as zf:
            zf.writestr("Takeout/Google Photos/Photos from 2019/IMG_31.JPG", JPEG_1PX)
        with zipfile.ZipFile(zp / "r2.zip", "w") as zf:
            zf.writestr("Takeout/Google Photos/Photos from 2019/IMG_31.JPG"
                        ".supplemental-metadata.json", sidecar())
        out = self.tmp / "out_r"
        Z.main(["--zips", str(zp), "--out", str(out), "--cache", str(cache),
                "--cache-only", "--prune-zips"])
        # metadata-only part is redundant, the media part is not
        self.assertEqual([p.name for p in sorted(zp.glob("*.zip"))], ["r1.zip"])
        self.assertTrue(cache.is_file())

    def test_stops_when_disk_is_tight_instead_of_failing(self):
        """A part that does not fit must be left for the next run, without data loss."""
        zp = self.tmp / "zt"
        zp.mkdir()
        with zipfile.ZipFile(zp / "t1.zip", "w") as zf:
            zf.writestr("Takeout/Google Photos/Photos from 2019/IMG_41.JPG", JPEG_1PX)
        with zipfile.ZipFile(zp / "t2.zip", "w") as zf:
            zf.writestr("Takeout/Google Photos/Photos from 2019/IMG_42.JPG",
                        JPEG_1PX + b"\0" * 5000)
        out = self.tmp / "out_t"
        real_free, real_margin = Z.free_bytes, Z.STAGING_MARGIN
        Z.free_bytes = lambda path: 2000
        Z.STAGING_MARGIN = 0
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                Z.main(["--zips", str(zp), "--out", str(out), "--tz", "+08:00"])
        finally:
            Z.free_bytes, Z.STAGING_MARGIN = real_free, real_margin
        text = buf.getvalue()
        self.assertIn("还剩 1 个分卷没处理", text)
        self.assertTrue((out / Z.PENDING_DIR / "Photos from 2019" / "IMG_41.JPG").is_file())
        self.assertFalse((out / Z.PENDING_DIR / "Photos from 2019" / "IMG_42.JPG").exists())
        self.assertEqual(len(list(zp.glob("*.zip"))), 2)      # nothing deleted

    def test_leftover_flat_files_are_split_by_importability(self):
        """Files unpacked by an older version get archived into the right bucket."""
        out = self.tmp / "out_l"
        (out / "Photos from 2019").mkdir(parents=True)
        (out / "Photos from 2019" / "IMG_51.JPG").write_bytes(JPEG_1PX)
        dated = out / "Photos from 2019" / "IMG_52.JPG"
        dated.write_bytes(JPEG_1PX)
        subprocess.run([EXIFTOOL, "-overwrite_original",
                        "-EXIF:DateTimeOriginal=2018:01:01 00:00:00", str(dated)],
                       check=True, capture_output=True)
        z = self.tmp / "zl"
        z.mkdir()
        with zipfile.ZipFile(z / "l1.zip", "w") as zf:      # metadata-only part
            zf.writestr("Takeout/Google Photos/Photos from 2019/IMG_53.JPG"
                        ".supplemental-metadata.json", sidecar())

        Z.main(["--zips", str(z), "--out", str(out), "--tz", "+08:00"])

        self.assertTrue((out / Z.PENDING_DIR / "Photos from 2019" / "IMG_51.JPG").is_file())
        self.assertTrue((out / Z.READY_DIR / "Photos from 2019" / "IMG_52.JPG").is_file())

    def test_file_dates_are_pinned_even_without_a_sidecar(self):
        out = self.tmp / "out_pin"
        bucket = out / Z.READY_DIR / "Photos from 2019"
        bucket.mkdir(parents=True)
        photo = bucket / "IMG_61.JPG"
        photo.write_bytes(JPEG_1PX)
        subprocess.run([EXIFTOOL, "-overwrite_original",
                        "-EXIF:DateTimeOriginal=2018:03:04 05:06:07", str(photo)],
                       check=True, capture_output=True)
        os.utime(photo, (1_700_000_000, 1_700_000_000))       # "today"-ish mtime
        z = self.tmp / "zp2"
        z.mkdir()
        with zipfile.ZipFile(z / "m.zip", "w") as zf:          # metadata-only part
            zf.writestr("Takeout/Google Photos/Photos from 2019/IMG_62.JPG"
                        ".supplemental-metadata.json", sidecar())

        Z.main(["--zips", str(z), "--out", str(out), "--tz", "+08:00"])

        tags = read_tags(photo, "-EXIF:DateTimeOriginal", "-FileModifyDate")
        self.assertEqual(tags["ExifIFD:DateTimeOriginal"], "2018:03:04 05:06:07")
        self.assertEqual(R.parse_stamp(tags["System:FileModifyDate"]),
                         R.stamp_epoch("2018:03:04 05:06:07", "+08:00"))

    def test_incomplete_part_is_skipped(self):
        (self.zips / "takeout-1-003.zip").write_bytes(b"PK\x03\x04 truncated")
        out = io.StringIO()
        real = sys.stdout
        sys.stdout = out
        try:
            Z.main(["--zips", str(self.zips), "--out", str(self.out), "--tz", "+08:00"])
        finally:
            sys.stdout = real
        self.assertTrue((self.out / Z.READY_DIR / "Photos from 2019" / "IMG_1.JPG").is_file())


class SelfDate(unittest.TestCase):
    """Media whose only date lives in QuickTime/Apple keys must not show up as "today"."""

    def test_own_capture_ts(self):
        tz = R.build_tz("+08:00")
        self.assertEqual(Z.own_capture_ts({"QuickTime:CreateDate": "2021:01:07 13:33:20"}, tz),
                         R.stamp_epoch("2021:01:07 13:33:20", "+08:00"))
        self.assertEqual(
            Z.own_capture_ts({"Keys:CreationDate": "2021:01:07 05:33:20+00:00"}, tz),
            R.stamp_epoch("2021:01:07 13:33:20", "+08:00"))
        self.assertIsNone(Z.own_capture_ts({"QuickTime:CreateDate": "0000:00:00 00:00:00"}, tz))
        self.assertIsNone(Z.own_capture_ts({}, tz))

    def test_image_with_quicktime_date_only_gets_exif_date(self):
        ctx = R.Ctx()
        ctx.tz = R.build_tz("+08:00")
        rec = {"QuickTime:CreateDate": "2021:01:07 13:33:20"}
        entry = {"path": "x.HEIC", "rel": "x.HEIC", "ts": R.stamp_epoch("2021:01:07 13:33:20", "+08:00"),
                 "gps": None, "description": None, "video": False, "via": None}
        tags = R.build_tags(ctx, entry, rec)
        self.assertEqual(tags["EXIF:DateTimeOriginal"], "2021:01:07 13:33:20")
        self.assertEqual(tags["FileModifyDate"], "2021:01:07 13:33:20+08:00")


class PathMapping(unittest.TestCase):
    def test_out_rel_strips_takeout_root(self):
        self.assertEqual(Z.out_rel("Takeout/Google Photos/Photos from 2019/a.jpg"),
                         "Photos from 2019/a.jpg")
        self.assertEqual(Z.out_rel("Takeout/Google Photos/Archive/a-b.JPG"), "Archive/a-b.JPG")
        self.assertEqual(Z.out_rel("Takeout/other/x.jpg"), "other/x.jpg")


if __name__ == "__main__":
    unittest.main()
