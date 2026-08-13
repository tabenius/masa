import importlib.resources
import json
import sys
import tarfile
import types
import zipfile
from pathlib import Path

import masa_cli.cli as cli
from masa_cli.cli import main
from PIL import Image


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
    record = json.loads((output_dir / "masa.json").read_text(encoding="utf-8"))["records"]["photo.png"]
    assert record["output_verified"]
    assert "date_matches" in record["metadata_verification"]
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["totals"]["processed"] == 1
    assert report["totals"]["errors"] == 0
    assert report["planned"][0]["output_file_name"] == "2021/01/photo.webp"


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
    for entry in cleanup_log:
        assert entry["source_sha256"] == entry["quarantine_sha256"]
        assert entry["size"] > 0


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


def test_min_savings_percent_discards_small_savings(tmp_path: Path, monkeypatch) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    _write_png_with_sidecar(input_dir / "photo.png")

    def fake_process_single_image(input_path, max_dim, quality, exif_bytes, output_format=None):
        temp_path = tmp_path / "same-ish.webp"
        temp_path.write_bytes(b"x" * 79)
        return str(temp_path), ".webp", "WEBP"

    monkeypatch.setattr("masa_cli.image_processor.process_single_image", fake_process_single_image)
    monkeypatch.setattr(cli, "_verify_output", lambda path, expected_format: (True, ""))

    result = main([str(input_dir), "-o", str(output_dir), "--min-savings-percent", "50", "--quiet"])

    assert result == 1
    errors = json.loads((output_dir / "masa-errors.json").read_text(encoding="utf-8"))["errors"]
    assert errors[0]["stage"] == "size-policy"


def test_resume_errors_only_reruns_listed_file(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    errors_path = tmp_path / "errors.json"
    _write_png_with_sidecar(input_dir / "a.png")
    _write_png_with_sidecar(input_dir / "b.png")
    errors_path.write_text(
        json.dumps({"errors": [{"input_file_name": "b.png", "stage": "tagging", "message": "old"}]}), encoding="utf-8"
    )

    result = main([str(input_dir), "-o", str(output_dir), "--resume-errors", str(errors_path), "--quiet"])

    assert result == 0
    manifest = json.loads((output_dir / "masa.json").read_text(encoding="utf-8"))
    assert sorted(manifest["records"]) == ["b.png"]


def test_workers_process_multiple_files(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    _write_png_with_sidecar(input_dir / "a.png")
    _write_png_with_sidecar(input_dir / "b.png")

    result = main([str(input_dir), "-o", str(output_dir), "--workers", "2", "--yes-fallbacks", "--quiet"])

    assert result == 0
    manifest = json.loads((output_dir / "masa.json").read_text(encoding="utf-8"))
    assert sorted(manifest["records"]) == ["a.png", "b.png"]


def test_dry_run_report_contains_plan_without_outputs(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    report_path = tmp_path / "dry-report.json"
    _write_png_with_sidecar(input_dir / "photo.png")

    result = main([str(input_dir), "-o", str(output_dir), "--dry-run", "--report", str(report_path), "--quiet"])

    assert result == 0
    assert not output_dir.exists()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["dry_run"] is True
    assert report["totals"]["planned"] == 1
    assert report["planned"][0]["output_file_name"] == "2021/photo.webp"


def test_subcommands_inspect_report_and_cleanup(tmp_path: Path, capsys) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    report_path = tmp_path / "report.json"
    quarantine_dir = tmp_path / "quarantine"
    _write_png_with_sidecar(input_dir / "photo.png")

    assert (
        main(
            [
                "process",
                str(input_dir),
                "-o",
                str(output_dir),
                "-f",
                "--quarantine-dir",
                str(quarantine_dir),
                "--report",
                str(report_path),
                "--quiet",
            ]
        )
        == 0
    )
    assert main(["inspect", str(output_dir)]) == 0
    assert "Records : 1" in capsys.readouterr().out
    assert main(["report", str(report_path)]) == 0
    assert "MASA SUMMARY REPORT" in capsys.readouterr().out
    cleanup_log = output_dir / "masa-cleanup-log.json"
    assert main(["cleanup", str(cleanup_log), "--dry-run"]) == 0
    assert (quarantine_dir / "photo.png").exists()
    assert main(["cleanup", str(cleanup_log), "--yes"]) == 0
    assert not (quarantine_dir / "photo.png").exists()


def test_restore_quarantined_files(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    quarantine_dir = tmp_path / "quarantine"
    _write_png_with_sidecar(input_dir / "photo.png")
    assert (
        main(
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
        == 0
    )
    cleanup_log = output_dir / "masa-cleanup-log.json"

    assert main(["restore", str(cleanup_log)]) == 0
    assert (input_dir / "photo.png").exists()
    assert (input_dir / "photo.png.json").exists()
    assert not (quarantine_dir / "photo.png").exists()


def test_restore_refuses_modified_quarantined_file(tmp_path: Path, capsys) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    quarantine_dir = tmp_path / "quarantine"
    _write_png_with_sidecar(input_dir / "photo.png")
    assert (
        main(
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
        == 0
    )
    cleanup_log = output_dir / "masa-cleanup-log.json"
    (quarantine_dir / "photo.png").write_bytes(b"modified")

    assert main(["restore", str(cleanup_log)]) == 1
    assert not (input_dir / "photo.png").exists()
    assert not (input_dir / "photo.png.json").exists()
    assert (quarantine_dir / "photo.png").exists()
    assert (quarantine_dir / "photo.png.json").exists()
    assert "Hash mismatches          : 1" in capsys.readouterr().out


def test_restore_dry_run_checks_hashes(tmp_path: Path, capsys) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    quarantine_dir = tmp_path / "quarantine"
    _write_png_with_sidecar(input_dir / "photo.png")
    assert (
        main(
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
        == 0
    )
    cleanup_log = output_dir / "masa-cleanup-log.json"
    (quarantine_dir / "photo.png").write_bytes(b"modified")

    assert main(["restore", str(cleanup_log), "--dry-run"]) == 1
    assert (quarantine_dir / "photo.png").exists()
    output = capsys.readouterr().out
    assert "Would restore files      : 0" in output
    assert "Hash mismatches          : 1" in output


def test_restore_existing_source_stops_batch(tmp_path: Path, capsys) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    quarantine_dir = tmp_path / "quarantine"
    _write_png_with_sidecar(input_dir / "photo.png")
    assert (
        main(
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
        == 0
    )
    cleanup_log = output_dir / "masa-cleanup-log.json"
    (input_dir / "photo.png").write_bytes(b"new file")

    assert main(["restore", str(cleanup_log)]) == 1
    assert (input_dir / "photo.png").read_bytes() == b"new file"
    assert not (input_dir / "photo.png.json").exists()
    assert (quarantine_dir / "photo.png").exists()
    assert (quarantine_dir / "photo.png.json").exists()
    assert "Skipped existing files   : 1" in capsys.readouterr().out


def test_cleanup_trash_uses_send2trash(tmp_path: Path, monkeypatch) -> None:
    quarantined = tmp_path / "quarantine" / "photo.png"
    quarantined.parent.mkdir()
    quarantined.write_bytes(b"data")
    cleanup_log = tmp_path / "masa-cleanup-log.json"
    cleanup_log.write_text(
        json.dumps([{"kind": "image", "source": "/source/photo.png", "quarantine_path": str(quarantined)}]),
        encoding="utf-8",
    )
    calls = []
    fake_module = types.SimpleNamespace(send2trash=lambda path: calls.append(path))
    monkeypatch.setitem(sys.modules, "send2trash", fake_module)

    assert main(["cleanup", str(cleanup_log), "--trash"]) == 0
    assert calls == [str(quarantined)]


def test_benchmark_writes_json(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    output_path = tmp_path / "benchmark.json"
    _write_png_with_sidecar(input_dir / "photo.png")

    result = main(["benchmark", str(input_dir), "--workers", "1,2", "--output", str(output_path)])

    assert result == 0
    data = json.loads(output_path.read_text(encoding="utf-8"))
    assert data["file_count"] == 1
    assert [row["workers"] for row in data["benchmarks"]] == [1, 2]


def test_doctor_reports_environment(capsys) -> None:
    result = main(["doctor", "--json"])

    assert result == 0
    data = json.loads(capsys.readouterr().out)
    assert "python" in data
    assert "encoders" in data


def test_validate_accepts_manifest(tmp_path: Path, capsys) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    _write_png_with_sidecar(input_dir / "photo.png")
    assert main([str(input_dir), "-o", str(output_dir), "--quiet"]) == 0

    result = main(["validate", str(output_dir / "masa.json")])

    assert result == 0
    assert "valid manifest" in capsys.readouterr().out


def test_validate_accepts_cleanup_log_with_hashes(tmp_path: Path, capsys) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    quarantine_dir = tmp_path / "quarantine"
    _write_png_with_sidecar(input_dir / "photo.png")
    assert (
        main(
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
        == 0
    )

    result = main(["validate", str(output_dir / "masa-cleanup-log.json")])

    assert result == 0
    assert "valid cleanup" in capsys.readouterr().out


def test_validate_rejects_invalid_errors_file(tmp_path: Path, capsys) -> None:
    target = tmp_path / "masa-errors.json"
    target.write_text(json.dumps({"errors": "not a list"}), encoding="utf-8")

    result = main(["validate", str(target)])

    assert result == 1
    assert "invalid errors" in capsys.readouterr().out


def test_verify_manifest_outputs(tmp_path: Path, capsys) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    _write_png_with_sidecar(input_dir / "photo.png")
    assert main([str(input_dir), "-o", str(output_dir), "--quiet"]) == 0

    assert main(["verify", str(output_dir)]) == 0
    assert "OK      : 1" in capsys.readouterr().out


def test_verify_detects_hash_mismatch(tmp_path: Path, capsys) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    _write_png_with_sidecar(input_dir / "photo.png")
    assert main([str(input_dir), "-o", str(output_dir), "--quiet"]) == 0
    output_file = output_dir / "2021" / "photo.webp"
    output_file.write_bytes(b"corrupted")

    assert main(["verify", str(output_dir)]) == 1
    assert "output sha256 mismatch" in capsys.readouterr().out


def test_packaged_schemas_are_available() -> None:
    schema = importlib.resources.files("masa_cli").joinpath("schemas", "manifest.schema.json")

    assert schema.is_file()


def test_unsafe_zip_path_is_rejected(tmp_path: Path) -> None:
    archive_path = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive_path, "w") as zf:
        zf.writestr("../escape.png", b"bad")

    result = main([str(archive_path), "-o", str(tmp_path / "out"), "--quiet"])

    assert result == 1


def test_unsafe_tar_path_is_rejected(tmp_path: Path) -> None:
    archive_path = tmp_path / "unsafe.tar"
    payload = tmp_path / "payload.png"
    payload.write_bytes(b"bad")
    with tarfile.open(archive_path, "w") as tf:
        tf.add(payload, "../escape.png")

    result = main([str(archive_path), "-o", str(tmp_path / "out"), "--quiet"])

    assert result == 1
