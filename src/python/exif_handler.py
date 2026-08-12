from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING

try:
    import piexif
except ImportError:
    piexif = None

if TYPE_CHECKING:
    from PIL import Image


def find_sidecar_json(image_path: str) -> str | None:
    candidates = [
        f"{image_path}.json",
        f"{os.path.splitext(image_path)[0]}.json",
    ]

    base_dir = os.path.dirname(image_path)
    file_name = os.path.basename(image_path)
    match = re.search(r"^(.*?)(\(\d+\))(\.[^.]+)$", file_name)
    if match:
        alt_name = f"{match.group(1)}{match.group(3)}{match.group(2)}.json"
        candidates.append(os.path.join(base_dir, alt_name))

    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate
    return None


def parse_takeout_json(json_path: str) -> tuple[datetime | None, float, float, dict]:
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        taken_time = None
        timestamp = data.get("photoTakenTime", {}).get("timestamp")
        if timestamp:
            taken_time = datetime.fromtimestamp(int(timestamp), tz=timezone.utc)

        geo_data = data.get("geoData", {}) or data.get("geoDataExif", {})
        lat = float(geo_data.get("latitude", 0.0) or 0.0)
        lon = float(geo_data.get("longitude", 0.0) or 0.0)

        return taken_time, lat, lon, data
    except Exception:
        return None, 0.0, 0.0, {}


def _deg_to_dms(deg: float) -> tuple[tuple[int, int], tuple[int, int], tuple[int, int]]:
    degrees = int(abs(deg))
    minutes_float = (abs(deg) - degrees) * 60
    minutes = int(minutes_float)
    seconds = int(round((minutes_float - minutes) * 60 * 100))
    return ((degrees, 1), (minutes, 1), (seconds, 100))


def build_exif_bytes(
    original_img: Image.Image,
    taken_time: datetime | None,
    lat: float,
    lon: float,
) -> bytes:
    if piexif is None:
        return b""

    exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}

    try:
        if "exif" in original_img.info:
            exif_dict = piexif.load(original_img.info["exif"])
    except Exception:
        pass

    if taken_time:
        exif_time = taken_time.astimezone().replace(tzinfo=None) if taken_time.tzinfo else taken_time
        date_str = exif_time.strftime("%Y:%m:%d %H:%M:%S")
        exif_dict["0th"][piexif.ImageIFD.DateTime] = date_str.encode("utf-8")
        exif_dict["Exif"][piexif.ExifIFD.DateTimeOriginal] = date_str.encode("utf-8")
        exif_dict["Exif"][piexif.ExifIFD.DateTimeDigitized] = date_str.encode("utf-8")

    if lat != 0.0 or lon != 0.0:
        gps = exif_dict.get("GPS", {})
        gps[piexif.GPSIFD.GPSLatitudeRef] = b"N" if lat >= 0 else b"S"
        gps[piexif.GPSIFD.GPSLatitude] = _deg_to_dms(lat)
        gps[piexif.GPSIFD.GPSLongitudeRef] = b"E" if lon >= 0 else b"W"
        gps[piexif.GPSIFD.GPSLongitude] = _deg_to_dms(lon)
        exif_dict["GPS"] = gps

    try:
        return piexif.dump(exif_dict)
    except Exception:
        return b""
