import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from textwrap import dedent

from masa_cli.archive import detect_and_prepare_input
from masa_cli.exif_handler import build_exif_bytes, find_sidecar_json, parse_takeout_json
from masa_cli.manifest import ManifestManager, compute_sha256
from masa_cli.ui import print_error, print_warning, render_progress


VALID_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".tiff", ".tif", ".bmp", ".gif"}
LOSSLESS_SOURCE_FORMATS = {"PNG", "GIF", "WEBP", "TIFF", "TIF", "BMP"}

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


def _fallback_allowed(key: str, prompt: str, args: argparse.Namespace, decisions: dict[str, bool]) -> bool:
    if args.fail_on_fallback:
        decisions[key] = False
        return False
    if args.yes_fallbacks:
        decisions[key] = True
        return True
    if args.no_fallbacks:
        decisions[key] = False
        return False
    if key not in decisions:
        decisions[key] = _ask_yes_no(prompt)
    return decisions[key]


def _resolve_output_format(
    orig_format: str | None,
    can_save_format,
    fallback_decisions: dict[str, bool],
    args: argparse.Namespace | None = None,
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


def _save_json_atomic(path: str, data: dict | list) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    temp_path = f"{path}.tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(temp_path, path)


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
        final_path = _move_with_unique_name(src_path, dest_path, dry_run)
        actions.append({"kind": kind, "source": src_path, "quarantine_path": final_path})
    return actions


def _record_error(errors: list[dict], rel_path: str, stage: str, message: str) -> None:
    errors.append({"input_file_name": rel_path, "stage": stage, "message": message})


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=_style("MASA - Media Archive Structuring & Archival CLI", f"{BOLD}{MAGENTA}"),
        epilog=dedent(
            f"""\
            {_style("Examples", f"{BOLD}{CYAN}")}
              ./masa /path/to/takeout
              ./masa /path/to/takeout.zip --by-month --max-dim 2048 --quality 82 --yes-fallbacks
              ./masa /path/to/takeout -o /path/to/output --format yaml --report report.json
              ./masa /path/to/takeout -f --quarantine-dir /path/to/quarantine
              NO_COLOR=1 ./masa --help
            """
        ),
        formatter_class=ColorHelpFormatter,
    )
    parser.add_argument("input_path", help="Path to input folder, .zip, or .tar.gz file.")
    parser.add_argument("-o", "--output", help="Output directory (default: input + '-masa').")
    parser.add_argument("--by-month", action="store_true", help="Stratify directory layout by month (YYYY/MM/).")
    parser.add_argument("--max-dim", type=int, default=2048, help="Maximum dimension in pixels (default: 2048).")
    parser.add_argument("--quality", type=int, default=80, help="Compression quality for lossy formats (default: 80).")
    parser.add_argument(
        "-f",
        "--quarantine-originals",
        action="store_true",
        help="Move originals and sidecars to quarantine after verified output. Safer than deletion.",
    )
    parser.add_argument("--quarantine-dir", help="Quarantine directory (default: OUTPUT/.masa-quarantine).")
    parser.add_argument(
        "--keep-original",
        action="store_true",
        help="Compatibility no-op. Originals are kept unless -f/--quarantine-originals is set.",
    )
    fallback_group = parser.add_mutually_exclusive_group()
    fallback_group.add_argument("--yes-fallbacks", action="store_true", help="Accept JPEG/PNG encoder fallbacks.")
    fallback_group.add_argument("--no-fallbacks", action="store_true", help="Decline JPEG/PNG encoder fallbacks.")
    fallback_group.add_argument("--fail-on-fallback", action="store_true", help="Fail files that need encoder fallback.")
    parser.add_argument("--skip-if-larger", action="store_true", help="Discard outputs larger than their source files.")
    parser.add_argument("--report", help="Write a structured JSON run report.")
    parser.add_argument("--errors", help="Write failed-file details (default: OUTPUT/masa-errors.json when errors occur).")
    parser.add_argument("--quiet", action="store_true", help="Suppress per-file progress output.")
    parser.add_argument("--verbose", action="store_true", help="Print a final failed-file table when errors occur.")
    parser.add_argument("--dry-run", action="store_true", help="Perform a dry run without writing files.")
    parser.add_argument("--format", choices=["json", "yaml"], default="json", help="Manifest format (default: json).")
    return parser.parse_args(argv)


def _print_progress(args: argparse.Namespace, seq_num: int, verb: str, current: int, total: int, state: str = "running", error_msg: str = "") -> None:
    if not args.quiet:
        render_progress(seq_num, verb, current, total, state=state, error_msg=error_msg)


def _print_summary(report: dict, verbose: bool) -> None:
    totals = report["totals"]
    print("\n" + "=" * 50)
    print("               MASA SUMMARY REPORT              ")
    print("=" * 50)
    print(f"Total Input Files    : {totals['total_files']}")
    print(f"Processed            : {totals['processed']}")
    print(f"Skipped (Manifest)   : {totals['skipped_manifest']}")
    print(f"Skipped (Larger)     : {totals['skipped_larger']}")
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


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    try:
        from PIL import Image

        from masa_cli.image_processor import can_save_format, process_single_image
    except ImportError as e:
        print_error(f"Missing required dependency: {e.name}. Run: python -m pip install -e .")
        return 1

    input_path = os.path.abspath(args.input_path)
    output_dir = os.path.abspath(args.output) if args.output else input_path.rstrip("/\\") + "-masa"
    quarantine_dir = os.path.abspath(args.quarantine_dir) if args.quarantine_dir else os.path.join(output_dir, ".masa-quarantine")
    errors_path = os.path.abspath(args.errors) if args.errors else os.path.join(output_dir, "masa-errors.json")

    secure_tmp_base = tempfile.mkdtemp(prefix="masa_work_")
    os.chmod(secure_tmp_base, 0o700)

    try:
        working_dir, is_temp = detect_and_prepare_input(input_path, secure_tmp_base)
    except Exception as e:
        print_error(f"Failed to prepare input: {e}")
        shutil.rmtree(secure_tmp_base, ignore_errors=True)
        return 1

    report = {
        "input_path": input_path,
        "output_dir": output_dir,
        "quarantine_dir": quarantine_dir if args.quarantine_originals else None,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": None,
        "fallback_decisions": {},
        "processed": [],
        "errors": [],
        "quarantine_actions": [],
        "totals": {
            "total_files": 0,
            "processed": 0,
            "skipped_manifest": 0,
            "skipped_larger": 0,
            "errors": 0,
            "quarantined_files": 0,
            "original_bytes": 0,
            "output_bytes": 0,
        },
    }

    try:
        all_files = _collect_images(working_dir)
        report["totals"]["total_files"] = len(all_files)
        manifest = ManifestManager(output_dir, args.format)

        if args.quarantine_originals:
            print_warning("-f/--quarantine-originals is enabled. Originals will be moved to quarantine, not deleted.")

        for idx, img_path in enumerate(all_files, start=1):
            rel_path = os.path.relpath(img_path, working_dir)
            if manifest.is_processed(rel_path):
                report["totals"]["skipped_manifest"] += 1
                continue

            orig_size = os.path.getsize(img_path)
            report["totals"]["original_bytes"] += orig_size

            if not args.dry_run:
                check_disk_space(output_dir, 3 * orig_size)

            _print_progress(args, idx, "tagging", 1, 5, state="running")
            json_path = find_sidecar_json(img_path)
            taken_time, lat, lon, _ = parse_takeout_json(json_path) if json_path else (None, 0.0, 0.0, {})
            if not taken_time:
                taken_time = datetime.fromtimestamp(os.path.getmtime(img_path)).astimezone()

            try:
                with Image.open(img_path) as orig_img:
                    orig_img.verify()
                with Image.open(img_path) as orig_img:
                    exif_bytes = build_exif_bytes(orig_img, taken_time, lat, lon)
                    orig_format = orig_img.format
                    orig_size_px = orig_img.size
            except Exception as e:
                _print_progress(args, idx, "tagging", 1, 5, state="error", error_msg=f"Invalid image: {e}")
                _record_error(report["errors"], rel_path, "tagging", f"Invalid image: {e}")
                continue

            output_choice = _resolve_output_format(orig_format, can_save_format, report["fallback_decisions"], args)
            if output_choice is None:
                _print_progress(args, idx, "compressing", 3, 5, state="error", error_msg="No available output encoder")
                _record_error(report["errors"], rel_path, "format", "No available output encoder")
                continue
            out_ext, out_format = output_choice

            _print_progress(args, idx, "scaling", 2, 5, state="running")
            _print_progress(args, idx, "compressing", 3, 5, state="running")
            temp_out_file = None
            try:
                if not args.dry_run:
                    temp_out_file, out_ext, out_format = process_single_image(
                        img_path,
                        args.max_dim,
                        args.quality,
                        exif_bytes,
                        out_format,
                    )
            except Exception as e:
                _print_progress(args, idx, "compressing", 3, 5, state="error", error_msg=f"Processing failed: {e}")
                _record_error(report["errors"], rel_path, "compressing", f"Processing failed: {e}")
                continue

            _print_progress(args, idx, "copying", 4, 5, state="running")
            year_str = taken_time.strftime("%Y")
            dest_dir = (
                os.path.join(output_dir, year_str, taken_time.strftime("%m"))
                if args.by_month
                else os.path.join(output_dir, year_str)
            )
            dest_path = _unique_destination_path(
                os.path.join(dest_dir, f"{os.path.splitext(os.path.basename(img_path))[0]}{out_ext}"),
                args.dry_run,
            )

            try:
                if not args.dry_run and temp_out_file:
                    os.makedirs(dest_dir, exist_ok=True)
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
                    if args.skip_if_larger and out_size > orig_size:
                        os.remove(dest_path)
                        report["totals"]["skipped_larger"] += 1
                        _record_error(report["errors"], rel_path, "size-policy", "Output was larger than source")
                        continue
                    orig_sha256 = compute_sha256(img_path)
                else:
                    out_size = orig_size
                    out_sha256 = "dry-run-hash"
                    orig_sha256 = "dry-run-hash"
            except Exception as e:
                if temp_out_file and os.path.exists(temp_out_file):
                    os.remove(temp_out_file)
                if not args.dry_run and os.path.exists(dest_path):
                    os.remove(dest_path)
                _print_progress(args, idx, "copying", 4, 5, state="error", error_msg=str(e))
                _record_error(report["errors"], rel_path, "copying", str(e))
                continue

            report["totals"]["output_bytes"] += out_size

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
                "output_verified": not args.dry_run,
                "exif_tags_kept": bool(exif_bytes),
                "date_taken": taken_time.isoformat(),
            }

            if not args.dry_run:
                manifest.add_record(rel_path, record)

            _print_progress(args, idx, "cleaning", 5, 5, state="running")
            if args.quarantine_originals and not args.dry_run and not is_temp:
                try:
                    actions = _quarantine_originals(img_path, json_path, working_dir, quarantine_dir, args.dry_run)
                    report["quarantine_actions"].extend(actions)
                    report["totals"]["quarantined_files"] += len(actions)
                except Exception as e:
                    print_warning(f"Could not quarantine original {rel_path}: {e}")
                    _record_error(report["errors"], rel_path, "quarantine", str(e))

            report["processed"].append(record)
            report["totals"]["processed"] += 1
            _print_progress(args, idx, "cleaning", 5, 5, state="done")

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
    finally:
        shutil.rmtree(secure_tmp_base, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
