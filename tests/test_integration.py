import json
import tarfile
import zipfile
from pathlib import Path

from PIL import Image

import masa_cli.cli as cli
from masa_cli.cli import main


def _write_png_with_sidecar(path: Path, color: tuple[int, int, int] = (40, 120, 200)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (16, 12), color=color).save(path)
    path.with_suffix(path.suffix + ".json").write_text(
        json.dumps(
            {
                "photoTakenTime": {"timestamp": "1609459200"},
                "geoData": {"latitude": 59.3293, "longitude": 18.0686},
            }
        ),
        encoding="utf-8",
    )


def _output_files(output_dir: Path) -> list[str]:
    return sorted(str(path.relative_to(output_dir)) for path in output_dir.rglob("*") if path.is_file())


def test_directory_run_keeps_originals_and_writes_report(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    report_path = tmp_path / "report.json"
    _write_png_with_sidecar(input_dir / "photo.png")

    result = main([str(input_dir), "-o", str(output_dir), "--by-month", "--report", str(report_path), "--quiet"])

    assert result == 0
    assert (input_dir / "photo.png").exists()
    assert (input_dir / "photo.png.json").exists()
    assert (output_dir / "2021" / "01" / "photo.webp").exists()
    assert json.loads((output_dir / "masa.json").read_text(encoding="utf-8"))["records"]["photo.png"][
        "output_verified"
    ]
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["totals"]["processed"] == 1
    assert report["totals"]["errors"] == 0


def test_quarantine_mode_moves_originals_and_sidecars(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    quarantine_dir = tmp_path / "quarantine"
    _write_png_with_sidecar(input_dir / "photo.png")

    result = main(
        [
            str(input_dir),
            "-o",
            str(output_dir),
            "-f",
            "--quarantine-dir",
            str(quarantine_dir),
            "--quiet",
        ]
    )

    assert result == 0
    assert not (input_dir / "photo.png").exists()
    assert not (input_dir / "photo.png.json").exists()
    assert (quarantine_dir / "photo.png").exists()
    assert (quarantine_dir / "photo.png.json").exists()
    cleanup_log = json.loads((output_dir / "masa-cleanup-log.json").read_text(encoding="utf-8"))
    assert {entry["kind"] for entry in cleanup_log} == {"image", "sidecar"}


def test_collision_safe_output_names(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    _write_png_with_sidecar(input_dir / "a" / "photo.png", (40, 120, 200))
    _write_png_with_sidecar(input_dir / "b" / "photo.png", (80, 40, 160))

    result = main([str(input_dir), "-o", str(output_dir), "--by-month", "--quiet"])

    assert result == 0
    files = _output_files(output_dir)
    assert "2021/01/photo.webp" in files
    assert "2021/01/photo-001.webp" in files


def test_zip_input_is_processed(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    output_dir = tmp_path / "output"
    archive_path = tmp_path / "takeout.zip"
    _write_png_with_sidecar(source_dir / "nested" / "photo.png")
    with zipfile.ZipFile(archive_path, "w") as zf:
        for path in source_dir.rglob("*"):
            if path.is_file():
                zf.write(path, path.relative_to(source_dir))

    result = main([str(archive_path), "-o", str(output_dir), "--quiet"])

    assert result == 0
    assert (output_dir / "2021" / "photo.webp").exists()


def test_tar_input_is_processed(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    output_dir = tmp_path / "output"
    archive_path = tmp_path / "takeout.tar.gz"
    _write_png_with_sidecar(source_dir / "nested" / "photo.png")
    with tarfile.open(archive_path, "w:gz") as tf:
        for path in source_dir.rglob("*"):
            if path.is_file():
                tf.add(path, path.relative_to(source_dir))

    result = main([str(archive_path), "-o", str(output_dir), "--quiet"])

    assert result == 0
    assert (output_dir / "2021" / "photo.webp").exists()


def test_invalid_image_writes_error_manifest(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    (input_dir / "broken.jpg").write_text("not an image", encoding="utf-8")

    result = main([str(input_dir), "-o", str(output_dir), "--quiet"])

    assert result == 1
    errors = json.loads((output_dir / "masa-errors.json").read_text(encoding="utf-8"))["errors"]
    assert errors[0]["input_file_name"] == "broken.jpg"
    assert errors[0]["stage"] == "tagging"


def test_skip_if_larger_discards_output(tmp_path: Path, monkeypatch) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    _write_png_with_sidecar(input_dir / "photo.png")

    def fake_process_single_image(input_path, max_dim, quality, exif_bytes, output_format=None):
        temp_path = tmp_path / "oversized.webp"
        temp_path.write_bytes(b"x" * 10_000)
        return str(temp_path), ".webp", "WEBP"

    monkeypatch.setattr("masa_cli.image_processor.process_single_image", fake_process_single_image)
    monkeypatch.setattr(cli, "_verify_output", lambda path, expected_format: (True, ""))

    result = main([str(input_dir), "-o", str(output_dir), "--skip-if-larger", "--quiet"])

    assert result == 1
    assert not (output_dir / "2021" / "photo.webp").exists()
    errors = json.loads((output_dir / "masa-errors.json").read_text(encoding="utf-8"))["errors"]
    assert errors[0]["stage"] == "size-policy"
