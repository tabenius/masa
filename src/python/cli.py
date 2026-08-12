import argparse
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime
from textwrap import dedent

from archive import detect_and_prepare_input
from exif_handler import build_exif_bytes, find_sidecar_json, parse_takeout_json
from manifest import ManifestManager, compute_sha256
from ui import print_error, print_warning, render_progress


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


def _output_extension(orig_format: str | None) -> tuple[str, str]:
    if (orig_format or "").upper() in ("JPEG", "JPG"):
        return ".avif", "AVIF"
    return ".webp", "WEBP"


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


def _resolve_output_format(
    orig_format: str | None,
    can_save_format,
    fallback_decisions: dict[str, bool],
) -> tuple[str, str] | None:
    normalized = (orig_format or "").upper()
    if normalized == "JPG":
        normalized = "JPEG"

    if normalized == "JPEG":
        if can_save_format("AVIF"):
            return ".avif", "AVIF"
        if "avif_to_jpeg" not in fallback_decisions:
            print_warning("AVIF output is unavailable because pillow-avif-plugin is not installed or not registered.")
            fallback_decisions["avif_to_jpeg"] = _ask_yes_no(
                "Use JPEG instead at slightly lower quality for JPEG/JPG inputs?"
            )
        return (".jpg", "JPEG") if fallback_decisions["avif_to_jpeg"] else None

    if can_save_format("WEBP"):
        return ".webp", "WEBP"

    if normalized in LOSSLESS_SOURCE_FORMATS:
        if "webp_to_png" not in fallback_decisions:
            print_warning("WEBP output is unavailable in this Pillow build.")
            fallback_decisions["webp_to_png"] = _ask_yes_no(
                "Use PNG instead for lossless inputs such as GIF or PNG?"
            )
        return (".png", "PNG") if fallback_decisions["webp_to_png"] else None

    return None


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=_style("MASA - Media Archive Structuring & Archival CLI", f"{BOLD}{MAGENTA}"),
        epilog=dedent(
            f"""\
            {_style("Examples", f"{BOLD}{CYAN}")}
              ./masa /path/to/takeout
              ./masa /path/to/takeout.zip --by-month --max-dim 2048 --quality 82
              ./masa /path/to/takeout -o /path/to/output --format yaml
              ./masa /path/to/takeout -f
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
        "--delete-originals",
        action="store_true",
        help="Delete original files and sidecars after successful processing. Dangerous; keep backups.",
    )
    parser.add_argument(
        "--keep-original",
        action="store_true",
        help="Compatibility no-op. Originals are kept unless -f/--delete-originals is set.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Perform a dry run without writing files.")
    parser.add_argument("--format", choices=["json", "yaml"], default="json", help="Manifest format (default: json).")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    try:
        from PIL import Image

        from image_processor import can_save_format, process_single_image
    except ImportError as e:
        print_error(f"Missing required dependency: {e.name}. Run: python -m pip install -e .")
        return 1

    input_path = os.path.abspath(args.input_path)
    output_dir = os.path.abspath(args.output) if args.output else input_path.rstrip("/\\") + "-masa"

    secure_tmp_base = tempfile.mkdtemp(prefix="masa_work_")
    os.chmod(secure_tmp_base, 0o700)

    try:
        working_dir, is_temp = detect_and_prepare_input(input_path, secure_tmp_base)
    except Exception as e:
        print_error(f"Failed to prepare input: {e}")
        shutil.rmtree(secure_tmp_base, ignore_errors=True)
        return 1

    try:
        all_files = _collect_images(working_dir)
        total_files = len(all_files)
        manifest = ManifestManager(output_dir, args.format)

        processed_count = 0
        skipped_count = 0
        error_count = 0
        total_orig_bytes = 0
        total_out_bytes = 0
        fallback_decisions = {}

        if args.delete_originals:
            print_warning(
                "-f/--delete-originals is enabled. MASA will remove directory input originals after successful output."
            )

        for idx, img_path in enumerate(all_files, start=1):
            rel_path = os.path.relpath(img_path, working_dir)
            if manifest.is_processed(rel_path):
                skipped_count += 1
                continue

            orig_size = os.path.getsize(img_path)
            total_orig_bytes += orig_size

            if not args.dry_run:
                check_disk_space(output_dir, 3 * orig_size)

            render_progress(idx, "tagging", 1, 5, state="running")
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
                render_progress(idx, "tagging", 1, 5, state="error", error_msg=f"Invalid image: {e}")
                error_count += 1
                continue

            output_choice = _resolve_output_format(orig_format, can_save_format, fallback_decisions)
            if output_choice is None:
                render_progress(idx, "compressing", 3, 5, state="error", error_msg="No available output encoder")
                error_count += 1
                continue
            out_ext, out_format = output_choice

            render_progress(idx, "scaling", 2, 5, state="running")
            render_progress(idx, "compressing", 3, 5, state="running")
            try:
                if not args.dry_run:
                    temp_out_file, out_ext, out_format = process_single_image(
                        img_path,
                        args.max_dim,
                        args.quality,
                        exif_bytes,
                        out_format,
                    )
                else:
                    temp_out_file = None
            except Exception as e:
                render_progress(idx, "compressing", 3, 5, state="error", error_msg=f"Processing failed: {e}")
                error_count += 1
                continue

            render_progress(idx, "copying", 4, 5, state="running")
            year_str = taken_time.strftime("%Y")
            dest_dir = (
                os.path.join(output_dir, year_str, taken_time.strftime("%m"))
                if args.by_month
                else os.path.join(output_dir, year_str)
            )
            dest_path = os.path.join(dest_dir, f"{os.path.splitext(os.path.basename(img_path))[0]}{out_ext}")
            dest_path = _unique_destination_path(dest_path, args.dry_run)

            if not args.dry_run:
                os.makedirs(dest_dir, exist_ok=True)
                shutil.copy2(temp_out_file, dest_path)
                os.remove(temp_out_file)
                out_size = os.path.getsize(dest_path)
                out_sha256 = compute_sha256(dest_path)
                orig_sha256 = compute_sha256(img_path)
            else:
                out_size = orig_size
                out_sha256 = "dry-run-hash"
                orig_sha256 = "dry-run-hash"

            total_out_bytes += out_size

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
                "exif_tags_kept": bool(exif_bytes),
                "date_taken": taken_time.isoformat(),
            }

            if not args.dry_run:
                manifest.add_record(rel_path, record)

            render_progress(idx, "cleaning", 5, 5, state="running")
            if args.delete_originals and not args.dry_run and not is_temp:
                try:
                    os.remove(img_path)
                    if json_path and os.path.exists(json_path):
                        os.remove(json_path)
                except Exception as e:
                    print_warning(f"Could not remove original {rel_path}: {e}")

            processed_count += 1
            render_progress(idx, "cleaning", 5, 5, state="done")

        print("\n" + "=" * 50)
        print("               MASA SUMMARY REPORT              ")
        print("=" * 50)
        print(f"Total Input Files    : {total_files}")
        print(f"Processed            : {processed_count}")
        print(f"Skipped (In Manifest): {skipped_count}")
        print(f"Errors               : {error_count}")
        if total_orig_bytes > 0:
            saved_bytes = total_orig_bytes - total_out_bytes
            total_pct = (saved_bytes / total_orig_bytes) * 100
            print(f"Original Total Size  : {total_orig_bytes / (1024 * 1024):.2f} MB")
            print(f"Output Total Size    : {total_out_bytes / (1024 * 1024):.2f} MB")
            print(f"Space Reclaimed      : {saved_bytes / (1024 * 1024):.2f} MB ({total_pct:.1f}%)")
        print("=" * 50 + "\n")
        return 0
    finally:
        shutil.rmtree(secure_tmp_base, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
