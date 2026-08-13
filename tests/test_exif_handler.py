import json
from datetime import datetime, timezone
from pathlib import Path

from masa_cli.exif_handler import find_sidecar_json, parse_takeout_json


def test_find_sidecar_json_google_duplicate_pattern(tmp_path: Path) -> None:
    image = tmp_path / "IMG_0001(1).jpg"
    sidecar = tmp_path / "IMG_0001.jpg(1).json"
    image.write_bytes(b"")
    sidecar.write_text("{}", encoding="utf-8")

    assert find_sidecar_json(str(image)) == str(sidecar)


def test_parse_takeout_json(tmp_path: Path) -> None:
    sidecar = tmp_path / "IMG_0001.jpg.json"
    sidecar.write_text(
        json.dumps(
            {
                "photoTakenTime": {"timestamp": "1609459200"},
                "geoData": {"latitude": 59.3293, "longitude": 18.0686},
            }
        ),
        encoding="utf-8",
    )

    taken_time, lat, lon, data = parse_takeout_json(str(sidecar))

    assert taken_time == datetime.fromtimestamp(1609459200, tz=timezone.utc)
    assert lat == 59.3293
    assert lon == 18.0686
    assert data["photoTakenTime"]["timestamp"] == "1609459200"
