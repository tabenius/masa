import argparse
import importlib.metadata
import importlib.resources
import json
import os
import shutil
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from textwrap import dedent
from threading import Lock

from masa_cli.archive import detect_and_prepare_input
from masa_cli.exif_handler import build_exif_bytes, find_sidecar_json, parse_takeout_json
from masa_cli.manifest import ManifestManager, compute_sha256
from masa_cli.ui import print_error, print_warning, render_progress

VALID_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".tiff", ".tif", ".bmp", ".gif"}
LOSSLESS_SOURCE_FORMATS = {"PNG", "GIF", "WEBP", "TIFF", "TIF", "BMP"}
SUBCOMMANDS = {"process", "inspect", "cleanup", "restore", "report", "benchmark", "doctor", "validate", "verify"}

BOLD = "\033[1m"
CYAN = "\033[36m"
GREEN = "\033[32m"
MAGENTA = "\033[35m"
YELLOW = "\033[33m"
RESET = "\033[0m"


def _should_color() -> bool:
    if os.environ.get("FORCE_COLOR") is not None:
        return True
    return os.environ.get("NO_COLOR") is None


def _style(text: str, color: str) -> str:
    if not _should_color():
        return text
    return f"{color}{text}{RESET}"


class ColorHelpFormatter(argparse.RawTextHelpFormatter):
    def start_section(self, heading: str) -> None:
        super().start_section(_style(heading, f"{BOLD}{CYAN}"))

    def _format_usage(self, usage, actions, groups, prefix):
        return super()._format_usage(
            usage,
            actions,
            groups,
            _style("usage: ", f"{BOLD}{GREEN}") if prefix is None else prefix,
        )

    def _format_action_invocation(self, action):
        invocation = super()._format_action_invocation(action)
        if not action.option_strings:
            return _style(invocation, YELLOW)

        pieces = []
        for token in invocation.split(", "):
            if " " in token:
                option, metavar = token.split(" ", 1)
                pieces.append(f"{_style(option, GREEN)} {_style(metavar, YELLOW)}")
            else:
                pieces.append(_style(token, GREEN))
        return ", ".join(pieces)


def _disk_usage_path(target_dir: str) -> str:
    path = os.path.abspath(target_dir)
    while not os.path.exists(path):
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return path


def check_disk_space(target_dir: str, required_bytes: int) -> None:
    usage_path = _disk_usage_path(target_dir)
    while True:
        stat = shutil.disk_usage(usage_path)
        if stat.free >= required_bytes:
            break
        print_warning(f"Low disk space on {usage_path}. Waiting for user to free up room...")
        time.sleep(5)


def _collect_images(working_dir: str) -> list[str]:
    all_files = []
    for root, _, files in os.walk(working_dir):
        for filename in files:
            if os.path.splitext(filename)[1].lower() in VALID_EXTENSIONS:
                all_files.append(os.path.join(root, filename))
    all_files.sort()
    return all_files


def _unique_destination_path(dest_path: str, dry_run: bool) -> str:
    if dry_run or not os.path.exists(dest_path):
        return dest_path

    base, ext = os.path.splitext(dest_path)
    counter = 1
    while True:
        candidate = f"{base}-{counter:03d}{ext}"
        if not os.path.exists(candidate):
            return candidate
        counter += 1


def _ask_yes_no(prompt: str) -> bool:
    sys.stdout.write(f"{_style('?', YELLOW)} {prompt} [y/N] ")
    sys.stdout.flush()
    try:
        answer = input().strip().lower()
    except EOFError:
        sys.stdout.write("\n")
        return False
    return answer in {"y", "yes"}


def _fallback_allowed(
    key: str, prompt: str, args: argparse.Namespace, decisions: dict[str, bool], lock: Lock | None = None
) -> bool:
    if args.fail_on_fallback:
        decisions[key] = False
        return False
    if args.yes_fallbacks:
        decisions[key] = True
        return True
    if args.no_fallbacks:
        decisions[key] = False
        return False
    if lock:
        with lock:
            if key not in decisions:
                decisions[key] = _ask_yes_no(prompt)
            return decisions[key]
    if key not in decisions:
        decisions[key] = _ask_yes_no(prompt)
    return decisions[key]


def _resolve_output_format(
    orig_format: str | None,
    can_save_format,
    fallback_decisions: dict[str, bool],
    args: argparse.Namespace | None = None,
    lock: Lock | None = None,
) -> tuple[str, str] | None:
    normalized = (orig_format or "").upper()
    if normalized == "JPG":
        normalized = "JPEG"

    if args is None:
        args = argparse.Namespace(yes_fallbacks=False, no_fallbacks=False, fail_on_fallback=False)

    if normalized == "JPEG":
        if can_save_format("AVIF"):
            return ".avif", "AVIF"
        print_warning("AVIF output is unavailable because pillow-avif-plugin is not installed or not registered.")
        if _fallback_allowed(
            "avif_to_jpeg",
            "Use JPEG instead at slightly lower quality for JPEG/JPG inputs?",
            args,
            fallback_decisions,
            lock,
        ):
            return ".jpg", "JPEG"
        return None

    if can_save_format("WEBP"):
        return ".webp", "WEBP"

    if normalized in LOSSLESS_SOURCE_FORMATS:
        print_warning("WEBP output is unavailable in this Pillow build.")
        if _fallback_allowed(
            "webp_to_png",
            "Use optimized palette PNG instead for lossless inputs such as GIF or PNG?",
            args,
            fallback_decisions,
            lock,
        ):
            return ".png", "PNG"
        return None

    return None


def _format_matches(actual: str | None, expected: str) -> bool:
    actual = (actual or "").upper()
    expected = expected.upper()
    if expected == "JPEG":
        return actual in {"JPEG", "JPG"}
    return actual == expected


def _verify_output(path: str, expected_format: str) -> tuple[bool, str]:
    try:
        from PIL import Image

        with Image.open(path) as img:
            actual_format = img.format
            img.verify()
        if not _format_matches(actual_format, expected_format):
            return False, f"expected {expected_format}, got {actual_format or 'unknown'}"
        with Image.open(path) as img:
            img.load()
        return True, ""
    except Exception as e:
        return False, str(e)


def _ratio_to_float(value) -> float:
    try:
        return float(value[0]) / float(value[1])
    except TypeError:
        return float(value)


def _dms_to_decimal(dms, ref: bytes | str) -> float:
    degrees = _ratio_to_float(dms[0])
    minutes = _ratio_to_float(dms[1])
    seconds = _ratio_to_float(dms[2])
    decimal = degrees + minutes / 60 + seconds / 3600
    ref_text = ref.decode("ascii", errors="ignore") if isinstance(ref, bytes) else str(ref)
    return -decimal if ref_text in {"S", "W"} else decimal


def _decode_exif_date(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _verify_metadata(path: str, expected_time: datetime | None, expected_lat: float, expected_lon: float) -> dict:
    result = {
        "checked": False,
        "date_present": False,
        "date_matches": False,
        "expected_date": expected_time.astimezone().strftime("%Y:%m:%d %H:%M:%S") if expected_time else None,
        "actual_date": None,
        "gps_present": False,
        "gps_matches": False,
        "expected_gps": [expected_lat, expected_lon] if expected_lat or expected_lon else None,
        "actual_gps": None,
        "message": "",
    }
    try:
        import piexif
        from PIL import Image

        with Image.open(path) as img:
            exif = img.info.get("exif")
        if not exif:
            result["message"] = "output has no EXIF block"
            return result
        result["checked"] = True
        exif_dict = piexif.load(exif)
        date_value = exif_dict.get("Exif", {}).get(piexif.ExifIFD.DateTimeOriginal) or exif_dict.get("0th", {}).get(
            piexif.ImageIFD.DateTime
        )
        result["actual_date"] = _decode_exif_date(date_value)
        result["date_present"] = bool(date_value) if expected_time else False
        result["date_matches"] = bool(result["expected_date"] and result["actual_date"] == result["expected_date"])
        gps = exif_dict.get("GPS", {})
        result["gps_present"] = (
            bool(gps.get(piexif.GPSIFD.GPSLatitude) and gps.get(piexif.GPSIFD.GPSLongitude))
            if expected_lat or expected_lon
            else False
        )
        if result["gps_present"]:
            actual_lat = _dms_to_decimal(gps[piexif.GPSIFD.GPSLatitude], gps.get(piexif.GPSIFD.GPSLatitudeRef, b"N"))
            actual_lon = _dms_to_decimal(gps[piexif.GPSIFD.GPSLongitude], gps.get(piexif.GPSIFD.GPSLongitudeRef, b"E"))
            result["actual_gps"] = [actual_lat, actual_lon]
            result["gps_matches"] = (
                abs(actual_lat - expected_lat) <= 0.0003 and abs(actual_lon - expected_lon) <= 0.0003
            )
        return result
    except Exception as e:
        result["message"] = str(e)
        return result


def _save_json_atomic(path: str, data: dict | list) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    temp_path = f"{path}.tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(temp_path, path)


def _load_json(path: str) -> dict | list:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _move_with_unique_name(src_path: str, dest_path: str, dry_run: bool) -> str:
    final_path = _unique_destination_path(dest_path, dry_run)
    if dry_run:
        return final_path
    os.makedirs(os.path.dirname(final_path), exist_ok=True)
    shutil.move(src_path, final_path)
    return final_path


def _quarantine_originals(
    img_path: str,
    json_path: str | None,
    working_dir: str,
    quarantine_dir: str,
    dry_run: bool,
) -> list[dict]:
    actions = []
    for src_path, kind in ((img_path, "image"), (json_path, "sidecar")):
        if not src_path or not os.path.exists(src_path):
            continue
        rel_path = os.path.relpath(src_path, working_dir)
        dest_path = os.path.join(quarantine_dir, rel_path)
        source_sha256 = compute_sha256(src_path)
        source_size = os.path.getsize(src_path)
        final_path = _move_with_unique_name(src_path, dest_path, dry_run)
        actions.append(
            {
                "kind": kind,
                "source": src_path,
                "quarantine_path": final_path,
                "source_sha256": source_sha256,
                "quarantine_sha256": source_sha256 if dry_run else compute_sha256(final_path),
                "size": source_size,
            }
        )
    return actions


def _record_error(errors: list[dict], rel_path: str, stage: str, message: str) -> None:
    errors.append({"input_file_name": rel_path, "stage": stage, "message": message})


def _add_process_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("input_path", help="Path to input folder, .zip, or .tar.gz file.")
    parser.add_argument("-o", "--output", help="Output directory (default: input + '-masa').")
    parser.add_argument("--by-month", action="store_true", help="Stratify directory layout by month (YYYY/MM/).")
    parser.add_argument("--max-dim", type=int, default=2048, help="Maximum dimension in pixels (default: 2048).")
    parser.add_argument("--quality", type=int, default=80, help="Compression quality for lossy formats (default: 80).")
    parser.add_argument(
        "-f",
        "--quarantine-originals",
        action="store_true",
        help="Move originals and sidecars to quarantine after verified output.",
    )
    parser.add_argument("--quarantine-dir", help="Quarantine directory (default: OUTPUT/.masa-quarantine).")
    parser.add_argument(
        "--keep-original", action="store_true", help="Compatibility no-op. Originals are kept unless -f is set."
    )
    fallback_group = parser.add_mutually_exclusive_group()
    fallback_group.add_argument("--yes-fallbacks", action="store_true", help="Accept JPEG/PNG encoder fallbacks.")
    fallback_group.add_argument("--no-fallbacks", action="store_true", help="Decline JPEG/PNG encoder fallbacks.")
    fallback_group.add_argument(
        "--fail-on-fallback", action="store_true", help="Fail files that need encoder fallback."
    )
    parser.add_argument("--skip-if-larger", action="store_true", help="Discard outputs larger than their source files.")
    parser.add_argument("--keep-if-larger", action="store_true", help="Alias for --skip-if-larger.")
    parser.add_argument(
        "--min-savings-percent", type=float, default=0.0, help="Skip outputs that save less than this percentage."
    )
    parser.add_argument("--report", help="Write a structured JSON run report.")
    parser.add_argument(
        "--errors", help="Write failed-file details (default: OUTPUT/masa-errors.json when errors occur)."
    )
    parser.add_argument("--resume-errors", help="Only rerun files listed in an earlier masa-errors.json.")
    parser.add_argument("--workers", type=int, default=1, help="Number of worker threads for processing (default: 1).")
    parser.add_argument("--quiet", action="store_true", help="Suppress per-file progress output.")
    parser.add_argument("--verbose", action="store_true", help="Print a final failed-file table when errors occur.")
    parser.add_argument(
        "--dry-run", action="store_true", help="Plan a run without writing outputs/manifests/quarantine logs."
    )
    parser.add_argument("--format", choices=["json", "yaml"], default="json", help="Manifest format (default: json).")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    if argv is None:
        argv = sys.argv[1:]
    argv = list(argv)
    if argv and argv[0] not in SUBCOMMANDS and argv[0] not in {"-h", "--help"}:
        argv.insert(0, "process")

    parser = argparse.ArgumentParser(
        prog="masa",
        description=_style("MASA - Media Archive Structuring & Archival CLI", f"{BOLD}{MAGENTA}"),
        epilog=dedent(
            f"""\
            {_style("Examples", f"{BOLD}{CYAN}")}
              masa process /path/to/takeout --yes-fallbacks
              masa /path/to/takeout.zip --by-month --workers 4 --yes-fallbacks
              masa inspect /path/to/output
              masa cleanup /path/to/output/masa-cleanup-log.json --yes
              masa restore /path/to/output/masa-cleanup-log.json
              masa verify /path/to/output
              masa benchmark /path/to/takeout --workers 1,2,4
              masa doctor
              masa validate /path/to/output/masa.json
            """
        ),
        formatter_class=ColorHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command")

    process_parser = subparsers.add_parser(
        "process", help="Process a Takeout folder or archive.", formatter_class=ColorHelpFormatter
    )
    _add_process_args(process_parser)

    inspect_parser = subparsers.add_parser(
        "inspect", help="Inspect an output directory or manifest.", formatter_class=ColorHelpFormatter
    )
    inspect_parser.add_argument("path", help="Output directory, masa.json, or masa.yaml.")

    cleanup_parser = subparsers.add_parser(
        "cleanup", help="Inspect or delete quarantined files from a cleanup log.", formatter_class=ColorHelpFormatter
    )
    cleanup_parser.add_argument("cleanup_log", help="Path to masa-cleanup-log.json.")
    cleanup_parser.add_argument(
        "--yes", action="store_true", help="Actually delete quarantined files listed in the log."
    )
    cleanup_parser.add_argument(
        "--trash", action="store_true", help="Move quarantined files to OS trash via Send2Trash."
    )
    cleanup_parser.add_argument("--dry-run", action="store_true", help="Show what would be deleted.")
    cleanup_parser.add_argument("--json", action="store_true", help="Write cleanup results as JSON.")

    restore_parser = subparsers.add_parser(
        "restore", help="Restore quarantined files from a cleanup log.", formatter_class=ColorHelpFormatter
    )
    restore_parser.add_argument("cleanup_log", help="Path to masa-cleanup-log.json.")
    restore_parser.add_argument("--dry-run", action="store_true", help="Show what would be restored.")
    restore_parser.add_argument("--overwrite", action="store_true", help="Overwrite existing source paths.")
    restore_parser.add_argument("--json", action="store_true", help="Write restore results as JSON.")

    report_parser = subparsers.add_parser(
        "report", help="Summarize a MASA report, errors file, or cleanup log.", formatter_class=ColorHelpFormatter
    )
    report_parser.add_argument("path", help="Path to a report JSON, masa-errors.json, or masa-cleanup-log.json.")

    benchmark_parser = subparsers.add_parser(
        "benchmark", help="Benchmark archive scanning and image metadata reads.", formatter_class=ColorHelpFormatter
    )
    benchmark_parser.add_argument("input_path", help="Path to input folder, .zip, or .tar.gz file.")
    benchmark_parser.add_argument("--workers", default="1,2,4", help="Comma-separated worker counts (default: 1,2,4).")
    benchmark_parser.add_argument("--limit", type=int, help="Maximum number of images to benchmark.")
    benchmark_parser.add_argument("--output", help="Write benchmark results as JSON.")

    doctor_parser = subparsers.add_parser(
        "doctor", help="Show runtime dependency and encoder diagnostics.", formatter_class=ColorHelpFormatter
    )
    doctor_parser.add_argument("--json", action="store_true", help="Write diagnostics as JSON.")

    validate_parser = subparsers.add_parser(
        "validate", help="Validate MASA JSON files against bundled schemas.", formatter_class=ColorHelpFormatter
    )
    validate_parser.add_argument("path", help="Path to masa.json, masa-errors.json, report JSON, or cleanup log.")
    validate_parser.add_argument(
        "--kind", choices=["auto", "manifest", "errors", "report", "cleanup"], default="auto", help="Schema kind."
    )

    verify_parser = subparsers.add_parser(
        "verify", help="Verify manifest output files, hashes, and readability.", formatter_class=ColorHelpFormatter
    )
    verify_parser.add_argument("path", help="Output directory, masa.json, or masa.yaml.")
    verify_parser.add_argument("--json", action="store_true", help="Write verification results as JSON.")

    if not argv:
        parser.print_help()
        raise SystemExit(0)
    return parser.parse_args(argv)


def _print_progress(
    args: argparse.Namespace,
    seq_num: int,
    verb: str,
    current: int,
    total: int,
    state: str = "running",
    error_msg: str = "",
) -> None:
    if not args.quiet:
        render_progress(seq_num, verb, current, total, state=state, error_msg=error_msg)


def _print_summary(report: dict, verbose: bool) -> None:
    totals = report["totals"]
    print("\n" + "=" * 50)
    print("               MASA SUMMARY REPORT              ")
    print("=" * 50)
    print(f"Total Input Files    : {totals['total_files']}")
    print(f"Processed            : {totals['processed']}")
    print(f"Planned              : {totals['planned']}")
    print(f"Skipped (Manifest)   : {totals['skipped_manifest']}")
    print(f"Skipped (Policy)     : {totals['skipped_policy']}")
    print(f"Errors               : {totals['errors']}")
    print(f"Quarantined Files    : {totals['quarantined_files']}")
    if totals["original_bytes"] > 0:
        saved_bytes = totals["original_bytes"] - totals["output_bytes"]
        total_pct = (saved_bytes / totals["original_bytes"]) * 100
        print(f"Original Total Size  : {totals['original_bytes'] / (1024 * 1024):.2f} MB")
        print(f"Output Total Size    : {totals['output_bytes'] / (1024 * 1024):.2f} MB")
        print(f"Space Reclaimed      : {saved_bytes / (1024 * 1024):.2f} MB ({total_pct:.1f}%)")
    if verbose and report["errors"]:
        print("\nFailed Files")
        for err in report["errors"]:
            print(f"- {err['input_file_name']} [{err['stage']}]: {err['message']}")
    print("=" * 50 + "\n")


def _new_report(input_path: str, output_dir: str, quarantine_dir: str | None, dry_run: bool) -> dict:
    return {
        "input_path": input_path,
        "output_dir": output_dir,
        "quarantine_dir": quarantine_dir,
        "dry_run": dry_run,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": None,
        "fallback_decisions": {},
        "planned": [],
        "processed": [],
        "errors": [],
        "quarantine_actions": [],
        "totals": {
            "total_files": 0,
            "planned": 0,
            "processed": 0,
            "skipped_manifest": 0,
            "skipped_policy": 0,
            "errors": 0,
            "quarantined_files": 0,
            "original_bytes": 0,
            "output_bytes": 0,
        },
    }


def _load_resume_filter(path: str | None) -> set[str] | None:
    if not path:
        return None
    data = _load_json(path)
    errors = data.get("errors", []) if isinstance(data, dict) else data
    return {entry["input_file_name"] for entry in errors if isinstance(entry, dict) and "input_file_name" in entry}


def _process_one(
    idx: int,
    img_path: str,
    working_dir: str,
    output_dir: str,
    quarantine_dir: str,
    is_temp: bool,
    args: argparse.Namespace,
    locks: dict[str, Lock],
    manifest: ManifestManager,
    can_save_format,
    process_single_image,
    fallback_decisions: dict[str, bool],
) -> dict:
    rel_path = os.path.relpath(img_path, working_dir)
    result = {
        "record": None,
        "planned": None,
        "errors": [],
        "quarantine_actions": [],
        "skipped_manifest": False,
        "skipped_policy": False,
    }

    with locks["manifest"]:
        if manifest.is_processed(rel_path):
            result["skipped_manifest"] = True
            return result

    orig_size = os.path.getsize(img_path)
    if not args.dry_run:
        check_disk_space(output_dir, 3 * orig_size)

    _print_progress(args, idx, "tagging", 1, 5, state="running")
    json_path = find_sidecar_json(img_path)
    taken_time, lat, lon, _ = parse_takeout_json(json_path) if json_path else (None, 0.0, 0.0, {})
    if not taken_time:
        taken_time = datetime.fromtimestamp(os.path.getmtime(img_path)).astimezone()

    try:
        from PIL import Image

        with Image.open(img_path) as orig_img:
            orig_img.verify()
        with Image.open(img_path) as orig_img:
            exif_bytes = build_exif_bytes(orig_img, taken_time, lat, lon)
            orig_format = orig_img.format
            orig_size_px = orig_img.size
    except Exception as e:
        _print_progress(args, idx, "tagging", 1, 5, state="error", error_msg=f"Invalid image: {e}")
        _record_error(result["errors"], rel_path, "tagging", f"Invalid image: {e}")
        return result

    output_choice = _resolve_output_format(orig_format, can_save_format, fallback_decisions, args, locks["fallback"])
    if output_choice is None:
        _print_progress(args, idx, "compressing", 3, 5, state="error", error_msg="No available output encoder")
        _record_error(result["errors"], rel_path, "format", "No available output encoder")
        return result
    out_ext, out_format = output_choice

    year_str = taken_time.strftime("%Y")
    dest_dir = (
        os.path.join(output_dir, year_str, taken_time.strftime("%m"))
        if args.by_month
        else os.path.join(output_dir, year_str)
    )
    with locks["destination"]:
        dest_path = _unique_destination_path(
            os.path.join(dest_dir, f"{os.path.splitext(os.path.basename(img_path))[0]}{out_ext}"), args.dry_run
        )
        if not args.dry_run:
            os.makedirs(dest_dir, exist_ok=True)
            Path(dest_path).touch(exist_ok=False)
            os.remove(dest_path)

    planned = {
        "input_file_name": rel_path,
        "output_file_name": os.path.relpath(dest_path, output_dir),
        "output_format": out_format,
        "quarantine_planned": bool(args.quarantine_originals and not is_temp),
    }
    result["planned"] = planned
    if args.dry_run:
        return result

    _print_progress(args, idx, "scaling", 2, 5, state="running")
    _print_progress(args, idx, "compressing", 3, 5, state="running")
    temp_out_file = None
    try:
        temp_out_file, out_ext, out_format = process_single_image(
            img_path, args.max_dim, args.quality, exif_bytes, out_format
        )
    except Exception as e:
        _print_progress(args, idx, "compressing", 3, 5, state="error", error_msg=f"Processing failed: {e}")
        _record_error(result["errors"], rel_path, "compressing", f"Processing failed: {e}")
        return result

    _print_progress(args, idx, "copying", 4, 5, state="running")
    try:
        temp_sha256 = compute_sha256(temp_out_file)
        shutil.copy2(temp_out_file, dest_path)
        out_sha256 = compute_sha256(dest_path)
        if temp_sha256 != out_sha256:
            raise RuntimeError("copied output hash does not match temporary output hash")
        verified, verify_error = _verify_output(dest_path, out_format)
        if not verified:
            raise RuntimeError(f"output verification failed: {verify_error}")
        os.remove(temp_out_file)
        temp_out_file = None
        out_size = os.path.getsize(dest_path)
        savings_percent = ((orig_size - out_size) / orig_size) * 100 if orig_size else 0.0
        if (args.skip_if_larger or args.keep_if_larger) and out_size > orig_size:
            os.remove(dest_path)
            result["skipped_policy"] = True
            _record_error(result["errors"], rel_path, "size-policy", "Output was larger than source")
            return result
        if args.min_savings_percent and savings_percent < args.min_savings_percent:
            os.remove(dest_path)
            result["skipped_policy"] = True
            _record_error(
                result["errors"],
                rel_path,
                "size-policy",
                f"Output saved {savings_percent:.1f}%, below {args.min_savings_percent:.1f}%",
            )
            return result
        orig_sha256 = compute_sha256(img_path)
    except Exception as e:
        if temp_out_file and os.path.exists(temp_out_file):
            os.remove(temp_out_file)
        if os.path.exists(dest_path):
            os.remove(dest_path)
        _print_progress(args, idx, "copying", 4, 5, state="error", error_msg=str(e))
        _record_error(result["errors"], rel_path, "copying", str(e))
        return result

    metadata_verification = (
        _verify_metadata(dest_path, taken_time, lat, lon)
        if exif_bytes
        else {"checked": False, "date_present": False, "gps_present": False, "message": "no EXIF bytes embedded"}
    )
    record = {
        "input_file_name": rel_path,
        "original_sha256": orig_sha256,
        "original_size": orig_size,
        "original_format": orig_format,
        "original_dimensions": orig_size_px,
        "output_file_name": os.path.relpath(dest_path, output_dir),
        "output_sha256": out_sha256,
        "output_size": out_size,
        "output_format": out_format,
        "output_verified": True,
        "metadata_verification": metadata_verification,
        "exif_tags_kept": bool(exif_bytes),
        "date_taken": taken_time.isoformat(),
    }

    with locks["manifest"]:
        manifest.add_record(rel_path, record)

    _print_progress(args, idx, "cleaning", 5, 5, state="running")
    if args.quarantine_originals and not is_temp:
        try:
            result["quarantine_actions"] = _quarantine_originals(
                img_path, json_path, working_dir, quarantine_dir, args.dry_run
            )
        except Exception as e:
            print_warning(f"Could not quarantine original {rel_path}: {e}")
            _record_error(result["errors"], rel_path, "quarantine", str(e))

    result["record"] = record
    _print_progress(args, idx, "cleaning", 5, 5, state="done")
    return result


def _run_process(args: argparse.Namespace) -> int:
    if args.workers < 1:
        print_error("--workers must be at least 1")
        return 2
    if args.workers > 1 and not (args.yes_fallbacks or args.no_fallbacks or args.fail_on_fallback):
        print_error("--workers greater than 1 requires --yes-fallbacks, --no-fallbacks, or --fail-on-fallback")
        return 2

    try:
        from masa_cli.image_processor import can_save_format, process_single_image
    except ImportError as e:
        print_error(f"Missing required dependency: {e.name}. Run: python -m pip install -e .")
        return 1

    input_path = os.path.abspath(args.input_path)
    output_dir = os.path.abspath(args.output) if args.output else input_path.rstrip("/\\") + "-masa"
    quarantine_dir = (
        os.path.abspath(args.quarantine_dir) if args.quarantine_dir else os.path.join(output_dir, ".masa-quarantine")
    )
    errors_path = os.path.abspath(args.errors) if args.errors else os.path.join(output_dir, "masa-errors.json")
    resume_filter = _load_resume_filter(args.resume_errors)

    secure_tmp_base = tempfile.mkdtemp(prefix="masa_work_")
    os.chmod(secure_tmp_base, 0o700)
    try:
        working_dir, is_temp = detect_and_prepare_input(input_path, secure_tmp_base)
        all_files = _collect_images(working_dir)
        if resume_filter is not None:
            all_files = [path for path in all_files if os.path.relpath(path, working_dir) in resume_filter]
        manifest = ManifestManager(output_dir, args.format)
        report = _new_report(
            input_path, output_dir, quarantine_dir if args.quarantine_originals else None, args.dry_run
        )
        report["totals"]["total_files"] = len(all_files)

        if args.quarantine_originals:
            print_warning("-f/--quarantine-originals is enabled. Originals will be moved to quarantine, not deleted.")

        locks = {"manifest": Lock(), "destination": Lock(), "fallback": Lock()}
        worker_args = (
            working_dir,
            output_dir,
            quarantine_dir,
            is_temp,
            args,
            locks,
            manifest,
            can_save_format,
            process_single_image,
            report["fallback_decisions"],
        )

        if args.workers == 1:
            results = [_process_one(idx, path, *worker_args) for idx, path in enumerate(all_files, start=1)]
        else:
            with ThreadPoolExecutor(max_workers=args.workers) as executor:
                futures = {
                    executor.submit(_process_one, idx, path, *worker_args): path
                    for idx, path in enumerate(all_files, start=1)
                }
                results = [future.result() for future in as_completed(futures)]

        for item in results:
            if item["skipped_manifest"]:
                report["totals"]["skipped_manifest"] += 1
            if item["skipped_policy"]:
                report["totals"]["skipped_policy"] += 1
            if item["planned"]:
                report["planned"].append(item["planned"])
                report["totals"]["planned"] += 1
            if item["record"]:
                report["processed"].append(item["record"])
                report["totals"]["processed"] += 1
                report["totals"]["original_bytes"] += item["record"]["original_size"]
                report["totals"]["output_bytes"] += item["record"]["output_size"]
            report["errors"].extend(item["errors"])
            report["quarantine_actions"].extend(item["quarantine_actions"])
            report["totals"]["quarantined_files"] += len(item["quarantine_actions"])

        report["totals"]["errors"] = len(report["errors"])
        report["finished_at"] = datetime.now(timezone.utc).isoformat()

        if report["errors"] and not args.dry_run:
            _save_json_atomic(errors_path, {"errors": report["errors"]})
        if report["quarantine_actions"] and not args.dry_run:
            _save_json_atomic(os.path.join(output_dir, "masa-cleanup-log.json"), report["quarantine_actions"])
        if args.report:
            _save_json_atomic(os.path.abspath(args.report), report)

        _print_summary(report, args.verbose or bool(report["errors"]))
        return 1 if report["errors"] else 0
    except Exception as e:
        print_error(str(e))
        return 1
    finally:
        shutil.rmtree(secure_tmp_base, ignore_errors=True)


def _find_manifest(path: str) -> tuple[str, str]:
    if os.path.isdir(path):
        for name in ("masa.json", "masa.yaml", "masa.yml"):
            candidate = os.path.join(path, name)
            if os.path.exists(candidate):
                return candidate, "yaml" if candidate.endswith((".yaml", ".yml")) else "json"
    return path, "yaml" if path.endswith((".yaml", ".yml")) else "json"


def _run_inspect(args: argparse.Namespace) -> int:
    manifest_path, format_type = _find_manifest(os.path.abspath(args.path))
    output_dir = os.path.dirname(manifest_path)
    manager = ManifestManager(output_dir, format_type)
    records = list(manager.records.values())
    original_bytes = sum(record.get("original_size", 0) for record in records)
    output_bytes = sum(record.get("output_size", 0) for record in records)
    print(f"Manifest: {manifest_path}")
    print(f"Records : {len(records)}")
    print(f"Original: {original_bytes / (1024 * 1024):.2f} MB")
    print(f"Output  : {output_bytes / (1024 * 1024):.2f} MB")
    if original_bytes:
        print(f"Savings : {((original_bytes - output_bytes) / original_bytes) * 100:.1f}%")
    return 0


def _preflight_cleanup_log(actions: list) -> dict:
    result = {"entries": [], "missing": 0, "hash_mismatches": 0, "invalid": 0}
    for entry in actions:
        if not isinstance(entry, dict) or not entry.get("quarantine_path"):
            result["invalid"] += 1
            continue
        quarantine_path = entry["quarantine_path"]
        if not os.path.exists(quarantine_path):
            result["missing"] += 1
            continue
        expected_sha256 = entry.get("quarantine_sha256") or entry.get("source_sha256")
        if expected_sha256 and compute_sha256(quarantine_path) != expected_sha256:
            result["hash_mismatches"] += 1
            continue
        result["entries"].append(entry)
    return result


def _print_json_summary(summary: dict) -> None:
    print(json.dumps(summary, indent=2))


def _run_cleanup(args: argparse.Namespace) -> int:
    if args.yes and args.trash:
        print_error("--yes and --trash are mutually exclusive")
        return 2
    actions = _load_json(os.path.abspath(args.cleanup_log))
    if not isinstance(actions, list):
        print_error("cleanup log must be a JSON list")
        return 1
    preflight = _preflight_cleanup_log(actions)
    entries = preflight["entries"]
    blocked = bool(preflight["missing"] or preflight["hash_mismatches"] or preflight["invalid"])
    summary = {
        "cleanup_log": os.path.abspath(args.cleanup_log),
        "mode": "trash" if args.trash else "delete" if args.yes else "inspect",
        "dry_run": args.dry_run,
        "quarantined_files": len(actions),
        "verified_files": len(entries),
        "missing_quarantined_files": preflight["missing"],
        "hash_mismatches": preflight["hash_mismatches"],
        "invalid_log_entries": preflight["invalid"],
        "changed_files": 0,
        "blocked": blocked,
    }
    if not args.json:
        print(f"Quarantined files in log : {summary['quarantined_files']}")
        print(f"Verified files           : {summary['verified_files']}")
        print(f"Missing quarantined files: {summary['missing_quarantined_files']}")
        print(f"Hash mismatches          : {summary['hash_mismatches']}")
        print(f"Invalid log entries      : {summary['invalid_log_entries']}")
    if (not args.yes and not args.trash) or args.dry_run:
        if args.json:
            _print_json_summary(summary)
        else:
            print("No files changed. Pass --yes to permanently delete or --trash to move files to OS trash.")
        return 1 if blocked else 0
    if blocked:
        if args.json:
            _print_json_summary(summary)
        else:
            print_warning("Cleanup preflight failed. No files deleted or trashed.")
        return 1
    changed = 0
    if args.trash:
        try:
            from send2trash import send2trash
        except ImportError:
            print_error("Send2Trash is not installed. Install with: python -m pip install '.[trash]'")
            return 1
        for entry in entries:
            send2trash(entry["quarantine_path"])
            changed += 1
        summary["changed_files"] = changed
        if args.json:
            _print_json_summary(summary)
        else:
            print(f"Trashed files            : {changed}")
        return 0
    for entry in entries:
        os.remove(entry["quarantine_path"])
        changed += 1
    summary["changed_files"] = changed
    if args.json:
        _print_json_summary(summary)
    else:
        print(f"Deleted files            : {changed}")
    return 0


def _run_restore(args: argparse.Namespace) -> int:
    actions = _load_json(os.path.abspath(args.cleanup_log))
    if not isinstance(actions, list):
        print_error("cleanup log must be a JSON list")
        return 1

    preflight = _preflight_cleanup_log(actions)
    restorable = [entry for entry in preflight["entries"] if entry.get("source")]
    invalid = preflight["invalid"] + (len(preflight["entries"]) - len(restorable))
    summary = {
        "cleanup_log": os.path.abspath(args.cleanup_log),
        "dry_run": args.dry_run,
        "overwrite": args.overwrite,
        "quarantined_files": len(actions),
        "restorable_files": len(restorable),
        "missing_quarantined_files": preflight["missing"],
        "invalid_log_entries": invalid,
        "skipped_existing_files": 0,
        "hash_mismatches": preflight["hash_mismatches"],
        "failed_restores": 0,
        "would_restore_files": 0,
        "restored_files": 0,
        "blocked": False,
    }
    if not args.json:
        print(f"Quarantined files in log : {summary['quarantined_files']}")
        print(f"Restorable files         : {summary['restorable_files']}")
        print(f"Missing quarantined files: {summary['missing_quarantined_files']}")
        print(f"Invalid log entries      : {summary['invalid_log_entries']}")

    restore_candidates = []
    skipped_existing = 0
    hash_mismatches = preflight["hash_mismatches"]
    failed = 0
    for entry in restorable:
        source_path = entry["source"]
        quarantine_path = entry["quarantine_path"]
        if os.path.exists(source_path) and not args.overwrite:
            skipped_existing += 1
            continue
        if os.path.exists(source_path) and args.overwrite and os.path.isdir(source_path):
            failed += 1
            continue
        restore_candidates.append(entry)

    restored = 0
    preflight_failed = bool(preflight["missing"] or invalid or skipped_existing or hash_mismatches or failed)
    summary["skipped_existing_files"] = skipped_existing
    summary["hash_mismatches"] = hash_mismatches
    summary["failed_restores"] = failed
    summary["blocked"] = preflight_failed
    if args.dry_run:
        summary["would_restore_files"] = len(restore_candidates) if not preflight_failed else 0
        if args.json:
            _print_json_summary(summary)
        else:
            print(f"Would restore files      : {summary['would_restore_files']}")
            print("No files restored.")
    elif preflight_failed:
        if args.json:
            _print_json_summary(summary)
        else:
            print_warning("Restore preflight failed. No files restored.")
    else:
        for entry in restore_candidates:
            source_path = entry["source"]
            quarantine_path = entry["quarantine_path"]
            try:
                os.makedirs(os.path.dirname(source_path), exist_ok=True)
                if os.path.exists(source_path) and args.overwrite:
                    os.remove(source_path)
                shutil.move(quarantine_path, source_path)
                restored += 1
            except Exception as e:
                if not args.json:
                    print_warning(f"Could not restore {quarantine_path}: {e}")
                failed += 1
        summary["restored_files"] = restored
        summary["failed_restores"] = failed
        summary["blocked"] = bool(failed)
        if args.json:
            _print_json_summary(summary)
    if not args.json:
        print(f"Restored files           : {summary['restored_files']}")
        print(f"Skipped existing files   : {summary['skipped_existing_files']}")
        print(f"Hash mismatches          : {summary['hash_mismatches']}")
        print(f"Failed restores          : {summary['failed_restores']}")
    return 1 if preflight["missing"] or invalid or skipped_existing or hash_mismatches or failed else 0


def _run_report(args: argparse.Namespace) -> int:
    data = _load_json(os.path.abspath(args.path))
    if isinstance(data, dict) and "totals" in data:
        _print_summary(data, True)
    elif isinstance(data, dict) and "errors" in data:
        print(f"Errors: {len(data['errors'])}")
        for err in data["errors"]:
            print(f"- {err.get('input_file_name')} [{err.get('stage')}]: {err.get('message')}")
    elif isinstance(data, list):
        print(f"Cleanup actions: {len(data)}")
        kinds = {}
        for entry in data:
            kinds[entry.get("kind", "unknown")] = kinds.get(entry.get("kind", "unknown"), 0) + 1
        for kind, count in sorted(kinds.items()):
            print(f"- {kind}: {count}")
    else:
        print_error("Unrecognized MASA report file")
        return 1
    return 0


def _verify_manifest_record(output_dir: str, rel_input_path: str, record: dict) -> dict:
    result = {
        "input_file_name": rel_input_path,
        "output_file_name": record.get("output_file_name"),
        "ok": False,
        "errors": [],
    }
    output_file_name = record.get("output_file_name")
    if not output_file_name:
        result["errors"].append("missing output_file_name")
        return result

    output_path = os.path.join(output_dir, output_file_name)
    if not os.path.exists(output_path):
        result["errors"].append("output file missing")
        return result

    expected_sha256 = record.get("output_sha256")
    if expected_sha256:
        actual_sha256 = compute_sha256(output_path)
        if actual_sha256 != expected_sha256:
            result["errors"].append("output sha256 mismatch")

    expected_format = record.get("output_format")
    if expected_format:
        readable, error = _verify_output(output_path, expected_format)
        if not readable:
            result["errors"].append(f"output unreadable: {error}")

    result["ok"] = not result["errors"]
    return result


def _run_verify(args: argparse.Namespace) -> int:
    manifest_path, format_type = _find_manifest(os.path.abspath(args.path))
    output_dir = os.path.dirname(manifest_path)
    manager = ManifestManager(output_dir, format_type)
    results = [
        _verify_manifest_record(output_dir, rel_input_path, record)
        for rel_input_path, record in sorted(manager.records.items())
    ]
    summary = {
        "manifest": manifest_path,
        "records": len(results),
        "ok": sum(1 for item in results if item["ok"]),
        "errors": sum(1 for item in results if not item["ok"]),
        "results": results,
    }
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"Manifest: {manifest_path}")
        print(f"Records : {summary['records']}")
        print(f"OK      : {summary['ok']}")
        print(f"Errors  : {summary['errors']}")
        for item in results:
            if not item["ok"]:
                print(f"- {item['input_file_name']}: {', '.join(item['errors'])}")
    return 1 if summary["errors"] else 0


def _benchmark_one(path: str) -> dict:
    started = time.perf_counter()
    try:
        from PIL import Image

        sidecar = find_sidecar_json(path)
        parse_takeout_json(sidecar) if sidecar else None
        with Image.open(path) as img:
            img.verify()
            image_format = img.format
            size = img.size
        return {
            "path": path,
            "ok": True,
            "format": image_format,
            "dimensions": size,
            "elapsed_seconds": time.perf_counter() - started,
        }
    except Exception as e:
        return {"path": path, "ok": False, "error": str(e), "elapsed_seconds": time.perf_counter() - started}


def _parse_worker_list(value: str) -> list[int]:
    workers = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        worker_count = int(part)
        if worker_count < 1:
            raise ValueError("worker counts must be at least 1")
        workers.append(worker_count)
    return workers or [1]


def _run_benchmark(args: argparse.Namespace) -> int:
    input_path = os.path.abspath(args.input_path)
    secure_tmp_base = tempfile.mkdtemp(prefix="masa_bench_")
    os.chmod(secure_tmp_base, 0o700)
    try:
        working_dir, _ = detect_and_prepare_input(input_path, secure_tmp_base)
        all_files = _collect_images(working_dir)
        if args.limit is not None:
            all_files = all_files[: args.limit]
        worker_counts = _parse_worker_list(args.workers)
        result = {
            "input_path": input_path,
            "file_count": len(all_files),
            "benchmarks": [],
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        for worker_count in worker_counts:
            started = time.perf_counter()
            if worker_count == 1:
                items = [_benchmark_one(path) for path in all_files]
            else:
                with ThreadPoolExecutor(max_workers=worker_count) as executor:
                    items = list(executor.map(_benchmark_one, all_files))
            elapsed = time.perf_counter() - started
            ok_count = sum(1 for item in items if item["ok"])
            files_per_second = len(all_files) / elapsed if elapsed else 0.0
            row = {
                "workers": worker_count,
                "files": len(all_files),
                "ok": ok_count,
                "errors": len(all_files) - ok_count,
                "elapsed_seconds": elapsed,
                "files_per_second": files_per_second,
            }
            result["benchmarks"].append(row)
            print(
                f"workers={worker_count} files={len(all_files)} ok={ok_count} "
                f"elapsed={elapsed:.3f}s rate={files_per_second:.1f}/s"
            )
        result["finished_at"] = datetime.now(timezone.utc).isoformat()
        if args.output:
            _save_json_atomic(os.path.abspath(args.output), result)
        return 0 if all(row["errors"] == 0 for row in result["benchmarks"]) else 1
    except Exception as e:
        print_error(str(e))
        return 1
    finally:
        shutil.rmtree(secure_tmp_base, ignore_errors=True)


def _version_for_package(package_name: str) -> str | None:
    try:
        return importlib.metadata.version(package_name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _run_doctor(args: argparse.Namespace) -> int:
    diagnostics = {
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "packages": {
            "masa-google-takeout-compressor": _version_for_package("masa-google-takeout-compressor"),
            "Pillow": _version_for_package("Pillow"),
            "piexif": _version_for_package("piexif"),
            "pillow-avif-plugin": _version_for_package("pillow-avif-plugin"),
            "PyYAML": _version_for_package("PyYAML"),
            "Send2Trash": _version_for_package("Send2Trash"),
            "jsonschema": _version_for_package("jsonschema"),
        },
        "encoders": {},
    }
    try:
        from masa_cli.image_processor import can_save_format

        diagnostics["encoders"] = {
            "AVIF": can_save_format("AVIF"),
            "WEBP": can_save_format("WEBP"),
            "JPEG": can_save_format("JPEG"),
            "PNG": can_save_format("PNG"),
        }
    except Exception as e:
        diagnostics["encoder_error"] = str(e)

    if args.json:
        print(json.dumps(diagnostics, indent=2))
    else:
        print(f"Python    : {diagnostics['python']}")
        print(f"Executable: {diagnostics['executable']}")
        print("Packages")
        for name, version in diagnostics["packages"].items():
            print(f"- {name}: {version or 'missing'}")
        print("Encoders")
        for name, available in diagnostics["encoders"].items():
            print(f"- {name}: {'yes' if available else 'no'}")
    missing_required = [
        name
        for name in ("Pillow", "piexif", "pillow-avif-plugin", "PyYAML")
        if diagnostics["packages"].get(name) is None
    ]
    return 1 if missing_required else 0


def _schema_resource_name(kind: str) -> str:
    return {
        "manifest": "manifest.schema.json",
        "errors": "errors.schema.json",
        "report": "report.schema.json",
        "cleanup": "cleanup-log.schema.json",
    }[kind]


def _load_schema(kind: str) -> dict:
    schema_file = importlib.resources.files("masa_cli").joinpath("schemas", _schema_resource_name(kind))
    return json.loads(schema_file.read_text(encoding="utf-8"))


def _detect_schema_kind(path: str, data: dict | list) -> str:
    basename = os.path.basename(path)
    if isinstance(data, list):
        return "cleanup"
    if isinstance(data, dict):
        if "totals" in data:
            return "report"
        if "records" in data:
            return "manifest"
        if "errors" in data:
            return "errors"
    if "cleanup" in basename:
        return "cleanup"
    raise ValueError("Could not infer schema kind; pass --kind")


def _validate_builtin(kind: str, data: dict | list) -> list[str]:
    errors = []
    if kind == "cleanup":
        if not isinstance(data, list):
            return ["cleanup log must be a JSON list"]
        required = {"kind", "source", "quarantine_path"}
        for idx, entry in enumerate(data):
            if not isinstance(entry, dict):
                errors.append(f"cleanup[{idx}] must be an object")
            elif missing := required - set(entry):
                errors.append(f"cleanup[{idx}] missing: {', '.join(sorted(missing))}")
        return errors

    if not isinstance(data, dict):
        return [f"{kind} must be a JSON object"]
    if kind == "manifest":
        if not isinstance(data.get("records"), dict):
            return ["manifest records must be an object"]
    elif kind == "errors":
        if not isinstance(data.get("errors"), list):
            return ["errors must be an array"]
    elif kind == "report":
        if not isinstance(data.get("totals"), dict):
            return ["report totals must be an object"]
    return errors


def _run_validate(args: argparse.Namespace) -> int:
    path = os.path.abspath(args.path)
    data = _load_json(path)
    try:
        kind = _detect_schema_kind(path, data) if args.kind == "auto" else args.kind
    except ValueError as e:
        print_error(str(e))
        return 2

    schema = _load_schema(kind)
    try:
        import jsonschema

        jsonschema.validate(instance=data, schema=schema)
        errors = []
        engine = "jsonschema"
    except ImportError:
        errors = _validate_builtin(kind, data)
        engine = "builtin"
    except Exception as e:
        errors = [str(e)]
        engine = "jsonschema"

    if errors:
        print(f"{path}: invalid {kind} ({engine})")
        for error in errors:
            print(f"- {error}")
        return 1
    print(f"{path}: valid {kind} ({engine})")
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parse_args(argv)
    except SystemExit as e:
        return int(e.code or 0)
    if args.command == "process":
        return _run_process(args)
    if args.command == "inspect":
        return _run_inspect(args)
    if args.command == "cleanup":
        return _run_cleanup(args)
    if args.command == "restore":
        return _run_restore(args)
    if args.command == "report":
        return _run_report(args)
    if args.command == "benchmark":
        return _run_benchmark(args)
    if args.command == "doctor":
        return _run_doctor(args)
    if args.command == "validate":
        return _run_validate(args)
    if args.command == "verify":
        return _run_verify(args)
    print_error("No command provided")
    return 2


if __name__ == "__main__":
    sys.exit(main())
