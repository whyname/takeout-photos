"""Behavioural tests for restore_gp_metadata.py.

Each test builds a real (tiny) Takeout-style tree and checks the metadata that
exiftool reads back from the media, so the assertions are about what Apple
Photos would actually see.
"""

import base64
import contextlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

import restore_gp_metadata as R  # noqa: E402

EXIFTOOL = shutil.which("exiftool")
FFMPEG = shutil.which("ffmpeg")

# Smallest valid baseline JPEG (1x1, no metadata at all).
JPEG_1PX = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0a"
    "HBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAABAAAAAAAA"
    "AAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AVN//2Q=="
)
TS = 1565605931              # 2019-08-12 10:32:11 UTC


def read_tags(path: Path, *names: str) -> dict:
    """Tag map for one file, keys as observed by exiftool (family-1 groups)."""
    args = [EXIFTOOL, "-json", "-G1", "-n", "-charset", "filename=UTF8"]
    args += names or ["-all:all"]
    res = subprocess.run(args + [str(path)], capture_output=True, text=True, check=True)
    records = json.loads(res.stdout)
    return records[0] if records else {}


@unittest.skipUnless(EXIFTOOL, "exiftool is required")
class TakeoutCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="gpmeta-"))
        self.addCleanup(shutil.rmtree, self.dir, True)

    # -- fixture helpers ---------------------------------------------------
    def media(self, name: str, data: bytes = JPEG_1PX) -> Path:
        path = self.dir / name
        path.write_bytes(data)
        return path

    def sidecar(self, name: str, **kw) -> Path:
        ts = kw.pop("ts", TS)
        album = kw.pop("album", False)
        formatted = kw.pop("formatted", None)
        if album:
            data = {"title": "2019", "date": {"timestamp": "1565605931"},
                    "enrichment": {"people": []}}
        else:
            geo = {"latitude": kw.pop("lat", 0.0), "longitude": kw.pop("lon", 0.0),
                   "altitude": kw.pop("alt", 0.0)}
            data = {
                "title": name[:-5],
                "description": kw.pop("desc", ""),
                "photoTakenTime": {"timestamp": str(ts) if ts else "0",
                                   "formatted": formatted or "Aug 12, 2019"},
                "creationTime": {"timestamp": str(ts) if ts else "0",
                                 "formatted": formatted or "Aug 12, 2019"},
                "geoData": geo,
                "geoDataExif": geo,
            }
        path = self.dir / name
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return path

    def run_script(self, *args) -> str:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(R.main(["--root", str(self.dir), *args]), 0)
        return buf.getvalue()

    # -- pairing -----------------------------------------------------------
    def test_pairs_all_takeout_layouts_and_reports_rest(self) -> None:
        direct = self.media("IMG_0001.JPG")
        self.sidecar("IMG_0001.JPG.json", desc="hello")
        dup = self.media("IMG_0002(1).JPG")
        self.sidecar("IMG_0002.JPG(1).json")
        edited = self.media("IMG_0003-edited.JPG")
        self.sidecar("IMG_0003-edited.JPG.json")
        self.media("IMG_0004.JPG")                      # no sidecar
        self.sidecar("IMG_0005.JPG.json")               # orphan
        self.sidecar("2019.json", album=True)           # album, must be ignored
        broken = self.dir / "IMG_0006.JPG.json"
        broken.write_text('{"title": "trunc')
        self.media("IMG_0006.JPG")

        out = self.run_script("--tz", "+08:00")

        for path in (direct, dup, edited):
            self.assertIn("ExifIFD:DateTimeOriginal",
                          read_tags(path, "-EXIF:DateTimeOriginal"))
        self.assertIn("unmatched JSON  : 1", out)     # IMG_0005 has no media
        self.assertIn("broken          : 1", out)     # IMG_0006 sidecar is truncated
        self.assertIn("album/info JSON skipped", out)
        # no-sidecar media untouched
        self.assertEqual(read_tags(self.dir / "IMG_0004.JPG", "-EXIF:DateTimeOriginal"),
                         {"SourceFile": str(self.dir / "IMG_0004.JPG")})

    # -- tag writing -------------------------------------------------------
    def test_writes_date_offset_gps_and_description(self) -> None:
        photo = self.media("IMG_0010.JPG")
        self.sidecar("IMG_0010.JPG.json", lat=31.2304, lon=121.4737, alt=12.5,
                     desc="上海外滩 夜景 🌃")
        self.run_script("--tz", "+08:00")

        tags = read_tags(photo, "-EXIF:DateTimeOriginal", "-EXIF:OffsetTimeOriginal",
                         "-EXIF:GPSLatitude", "-EXIF:GPSLongitude", "-EXIF:GPSLatitudeRef",
                         "-EXIF:ImageDescription", "-XMP:Description")
        self.assertEqual(tags["ExifIFD:DateTimeOriginal"], "2019:08:12 18:32:11")
        self.assertEqual(tags["ExifIFD:OffsetTimeOriginal"], "+08:00")
        self.assertAlmostEqual(float(tags["GPS:GPSLatitude"]), 31.2304, places=4)
        self.assertAlmostEqual(float(tags["GPS:GPSLongitude"]), 121.4737, places=4)
        self.assertEqual(tags["GPS:GPSLatitudeRef"], "N")
        self.assertEqual(tags["IFD0:ImageDescription"], "上海外滩 夜景 🌃")
        self.assertEqual(tags["XMP-dc:Description"], "上海外滩 夜景 🌃")

    def test_tz_utc_and_negative_gps(self) -> None:
        photo = self.media("IMG_0011.JPG")
        self.sidecar("IMG_0011.JPG.json", lat=-33.8688, lon=-70.6693)
        self.run_script("--tz", "UTC")
        tags = read_tags(photo, "-EXIF:DateTimeOriginal", "-EXIF:GPSLatitudeRef",
                         "-EXIF:GPSLongitudeRef")
        self.assertEqual(tags["ExifIFD:DateTimeOriginal"], "2019:08:12 10:32:11")
        self.assertEqual(tags["GPS:GPSLatitudeRef"], "S")
        self.assertEqual(tags["GPS:GPSLongitudeRef"], "W")

    def test_keeps_camera_exif_but_fills_gaps_and_pins_mtime(self) -> None:
        photo = self.media("IMG_0012.JPG")
        subprocess.run([EXIFTOOL, "-overwrite_original",
                        "-EXIF:DateTimeOriginal=2001:06:15 09:00:00", str(photo)],
                       check=True, capture_output=True)
        self.sidecar("IMG_0012.JPG.json", ts=1600000000, lat=48.8584, lon=2.2945)

        self.run_script("--tz", "+08:00")

        tags = read_tags(photo, "-EXIF:DateTimeOriginal", "-EXIF:GPSLatitude", "-FileModifyDate")
        self.assertEqual(tags["ExifIFD:DateTimeOriginal"], "2001:06:15 09:00:00")
        self.assertAlmostEqual(float(tags["GPS:GPSLatitude"]), 48.8584, places=4)
        self.assertEqual(R.parse_stamp(tags["System:FileModifyDate"]),
                         R.stamp_epoch("2001:06:15 09:00:00", "+08:00"))

    def test_second_run_writes_nothing(self) -> None:
        self.media("IMG_0013.JPG")
        self.sidecar("IMG_0013.JPG.json", lat=1.0, lon=2.0, desc="once")
        self.run_script("--tz", "+08:00")
        out = self.run_script("--tz", "+08:00")
        self.assertIn("to write        : 0", out)
        self.assertIn("already set     : 1", out)

    def test_overwrite_replaces_existing_date(self) -> None:
        photo = self.media("IMG_0014.JPG")
        subprocess.run([EXIFTOOL, "-overwrite_original",
                        "-EXIF:DateTimeOriginal=2001:06:15 09:00:00", str(photo)],
                       check=True, capture_output=True)
        self.sidecar("IMG_0014.JPG.json", ts=1600000000)
        self.run_script("--tz", "+08:00", "--overwrite")
        tags = read_tags(photo, "-EXIF:DateTimeOriginal")
        self.assertEqual(tags["ExifIFD:DateTimeOriginal"], "2020:09:13 20:26:40")

    def test_supplemental_metadata_names_and_edited_fallback(self) -> None:
        plain = self.media("IMG_0020.JPG")
        self.sidecar("IMG_0020.JPG.supplemental-metadata.json")
        dup = self.media("IMG_0021(1).JPG")
        self.sidecar("IMG_0021.JPG.supplemental-metadata(1).json")
        render = self.media("IMG_0022-edited.JPG")
        own = self.media("IMG_0023-edited.JPG")
        self.sidecar("IMG_0023-edited.JPG.supplemental-metadata.json")
        # original never exported: only its -edited render is present
        self.sidecar("IMG_0022.JPG.supplemental-metadata.json")

        out = self.run_script("--tz", "+08:00")

        for path in (plain, dup, own, render):
            self.assertIn("ExifIFD:DateTimeOriginal",
                          read_tags(path, "-EXIF:DateTimeOriginal"))
        self.assertIn("1 via -edited copies", out)
        # the edited file keeps the metadata of its own sidecar, not the original's
        self.assertEqual(read_tags(own, "-EXIF:DateTimeOriginal")["ExifIFD:DateTimeOriginal"],
                         "2019:08:12 18:32:11")

    def test_numbered_sidecar_matches_plain_media(self) -> None:
        photo = self.media("IMG_0030.JPG")
        self.sidecar("IMG_0030.JPG.supplemental-metadata(1).json")
        out = self.run_script("--tz", "+08:00")
        self.assertIn("1 via unnumbered media", out)
        self.assertIn("ExifIFD:DateTimeOriginal", read_tags(photo, "-EXIF:DateTimeOriginal"))

    def test_dry_run_touches_nothing(self) -> None:
        photo = self.media("IMG_0015.JPG")
        self.sidecar("IMG_0015.JPG.json")
        out = self.run_script("--dry-run")
        self.assertIn("dry run", out)
        self.assertEqual(read_tags(photo, "-EXIF:DateTimeOriginal"),
                         {"SourceFile": str(photo)})

    @unittest.skipUnless(FFMPEG, "ffmpeg is required to build a video fixture")
    def test_video_gets_quicktime_date_and_gps(self) -> None:
        video = self.dir / "VID_0016.MP4"
        subprocess.run([FFMPEG, "-y", "-f", "lavfi", "-i", "color=c=red:s=32x32:d=1",
                        "-pix_fmt", "yuv420p", str(video)], check=True, capture_output=True)
        self.sidecar("VID_0016.MP4.json", ts=1610000000, lat=51.5007, lon=-0.1246, alt=5.0)
        self.run_script("--tz", "+08:00")

        tags = read_tags(video, "-QuickTime:CreateDate", "-QuickTime:GPSCoordinates")
        self.assertEqual(tags["QuickTime:CreateDate"], "2021:01:07 14:13:20")
        lat, lon, alt = (float(v) for v in tags["ItemList:GPSCoordinates"].split())
        self.assertAlmostEqual(lat, 51.5007, places=3)
        self.assertAlmostEqual(lon, -0.1246, places=3)
        self.assertAlmostEqual(alt, 5.0, places=2)
        # no EXIF block may be invented inside a video container
        self.assertNotIn("EXIF:DateTimeOriginal",
                         read_tags(video, "-EXIF:DateTimeOriginal"))


class PureLogic(unittest.TestCase):
    def test_parse_sidecar_name(self) -> None:
        self.assertEqual(R.parse_sidecar_name("IMG_1.JPG.json"), ("IMG_1.JPG", None))
        self.assertEqual(R.parse_sidecar_name("IMG_1.JPG.supplemental-metadata.json"),
                         ("IMG_1.JPG", None))
        self.assertEqual(R.parse_sidecar_name("IMG_1.JPG(2).json"), ("IMG_1.JPG", 2))
        self.assertEqual(R.parse_sidecar_name("IMG_1.JPG.supplemental-metadata(2).json"),
                         ("IMG_1.JPG", 2))
        self.assertEqual(R.parse_sidecar_name("IMG_1(2).JPG.json"), ("IMG_1(2).JPG", None))
        self.assertIsNone(R.parse_sidecar_name("IMG_1.JPG"))
        # sidecar keys must line up with the keys built from real media names
        self.assertEqual(R.sidecar_key("IMG_1.JPG", 2), R.media_key("IMG_1(2).JPG"))
        self.assertEqual(R.sidecar_key("IMG_1.JPG", None), R.media_key("IMG_1.JPG"))

    def test_iso6709_padding(self) -> None:
        self.assertEqual(R.iso6709(51.5007, -0.1246, 5.0), "+51.5007-000.1246+5.000/")
        self.assertEqual(R.iso6709(-33.8688, 151.2093, None), "-33.8688+151.2093/")

    def test_zero_dates_count_as_missing(self) -> None:
        self.assertFalse(R._filled("0000:00:00 00:00:00"))
        self.assertTrue(R._filled("2019:08:12 18:32:11"))

    def test_parse_stamp_handles_offsets_and_subseconds(self) -> None:
        base = R.parse_stamp("2019:08:12 18:32:11+08:00")
        self.assertEqual(base, TS)
        self.assertEqual(R.parse_stamp("2019:08:12 10:32:11.123+00:00"), TS)
        self.assertEqual(R.parse_stamp("2019:08:12 18:32:11+0800"), TS)
        self.assertIsNone(R.parse_stamp("garbage"))


if __name__ == "__main__":
    unittest.main()
