# MASA - Media Archive Structuring & Archival

MASA is a Python CLI for processing Google Takeout photo archives from a
directory, ZIP file, or TAR/TAR.GZ file. It reads Google Photos JSON sidecars,
adds available date/GPS metadata, downscales images, converts outputs to modern
formats, verifies the written files, and records cryptographic manifests.

MASA keeps originals by default. `-f/--quarantine-originals` moves originals and
sidecars to a quarantine folder after verified output; it does not permanently
delete them.

## Features

- Accepts Takeout folders, `.zip`, `.tar`, `.tar.gz`, and other tar-compatible
  archives.
- Extracts archives into private temporary workspaces and rejects unsafe archive
  paths.
- Finds common Google Photos JSON sidecars, including duplicate filename
  patterns such as `IMG_0001.jpg(1).json`.
- Uses timezone-aware UTC `photoTakenTime.timestamp` values from sidecars, with
  file modification time as a fallback.
- Embeds EXIF date fields and GPS coordinates when `piexif` can encode them.
- Downscales images to a configurable maximum dimension.
- Converts JPEG/JPG to AVIF when AVIF support is available.
- Converts PNG, GIF, WEBP, TIFF/TIF, and BMP to lossless WEBP when WEBP support
  is available.
- Prompts for JPEG fallback at slightly lower quality when AVIF is unavailable.
- Prompts for optimized palette PNG fallback when WEBP is unavailable for
  lossless-style inputs.
- Supports `--yes-fallbacks`, `--no-fallbacks`, and `--fail-on-fallback` for
  noninteractive runs.
- Avoids output filename collisions by appending numeric suffixes such as
  `-001`.
- Verifies copied outputs by comparing temp/destination hashes and reopening the
  final image before manifesting or quarantining originals.
- Optionally skips outputs larger than the source with `--skip-if-larger`.
- Writes resumable JSON or YAML manifests.
- Writes `masa-errors.json` for failed files and `masa-cleanup-log.json` for
  quarantined originals.
- Writes structured run reports with `--report`.
- Provides colored progress/help output, plus `NO_COLOR=1` and `FORCE_COLOR=1`.

## Install

Use Python 3.10 or newer.

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
```

For development and tests:

```bash
python -m pip install -e ".[dev]"
python -m pytest
```

AVIF output depends on `pillow-avif-plugin`, which is listed in the project
dependencies. If AVIF is unavailable at runtime, MASA can fall back to JPEG.

## Usage

Show help:

```bash
./masa --help
```

Process a Takeout directory. Originals are kept:

```bash
./masa /path/to/takeout
```

Process a ZIP archive by year/month and accept fallback encoders automatically:

```bash
./masa /path/to/takeout.zip --by-month --max-dim 2048 --quality 82 --yes-fallbacks
```

Write to an explicit output directory and create a structured report:

```bash
./masa /path/to/takeout -o /path/to/output --report /path/to/report.json
```

Write a YAML manifest:

```bash
./masa /path/to/takeout --format yaml
```

Move originals and sidecars to quarantine after successful verification:

```bash
./masa /path/to/takeout -f --quarantine-dir /path/to/quarantine
```

Preview without writing output, manifest records, error files, or quarantine
logs:

```bash
./masa /path/to/takeout --dry-run
```

Disable or force color output:

```bash
NO_COLOR=1 ./masa --help
FORCE_COLOR=1 ./masa --help
```

## Options

`input_path`
: Required. Path to a folder, ZIP file, or TAR/TAR.GZ archive.

`-o, --output`
: Output directory. Defaults to the input path with `-masa` appended.

`--by-month`
: Store output as `YYYY/MM/file.ext` instead of `YYYY/file.ext`.

`--max-dim`
: Maximum width or height in pixels. Defaults to `2048`.

`--quality`
: AVIF quality for JPEG/JPG inputs. JPEG fallback uses `quality - 10`. PNG
  fallback uses quality to choose a palette size. Defaults to `80`.

`-f, --quarantine-originals`
: Move original files and JSON sidecars to quarantine after output verification.
  This only applies to directory inputs. Archive inputs are extracted into
  temporary space, so the original archive is left untouched.

`--quarantine-dir`
: Quarantine directory. Defaults to `OUTPUT/.masa-quarantine`.

`--keep-original`
: Compatibility no-op. Originals are kept unless `-f/--quarantine-originals` is
  set.

`--yes-fallbacks`
: Accept JPEG/PNG fallbacks without prompting.

`--no-fallbacks`
: Decline JPEG/PNG fallbacks without prompting.

`--fail-on-fallback`
: Treat files that need encoder fallback as failures.

`--skip-if-larger`
: Remove a converted output and record an error if the output is larger than the
  source.

`--report`
: Write a structured JSON run report with totals, processed records, fallback
  decisions, quarantine actions, and errors.

`--errors`
: Error manifest path. Defaults to `OUTPUT/masa-errors.json` when failures
  occur.

`--quiet`
: Suppress per-file progress output.

`--verbose`
: Print a final failed-file table. Error runs print the table automatically.

`--dry-run`
: Read inputs and show a summary without writing outputs, manifests, errors, or
  quarantine logs.

`--format {json,yaml}`
: Manifest format. Defaults to `json`.

## Output Layout

Without `--by-month`:

```text
output/
  masa.json
  masa-errors.json
  masa-cleanup-log.json
  2024/
    IMG_0001.avif
    Screenshot.webp
```

With `--by-month`:

```text
output/
  masa.json
  2024/
    01/
      IMG_0001.avif
    02/
      Screenshot.webp
```

## Manifest And Reports

Each processed file is recorded under its relative input path. Records include:

- original filename, size, format, dimensions, and SHA-256 hash
- output filename, size, format, and SHA-256 hash
- `output_verified`
- whether EXIF bytes were embedded
- date used for output organization

`masa-errors.json` records failed files with the input path, stage, and message.
`masa-cleanup-log.json` records every original or sidecar moved to quarantine.
`--report` writes a full run report for scripts and auditing.

## Metadata Notes

MASA records whether EXIF bytes were embedded, but metadata preservation is not
guaranteed across every encoder and fallback path. AVIF/JPEG/PNG/WEBP support
varies by Pillow build, and `piexif` can reject malformed or unsupported EXIF
payloads. Treat `exif_tags_kept: true` as "metadata was embedded" and
`exif_tags_kept: false` as "conversion succeeded without EXIF bytes."

## Safety Notes

MASA no longer hard-deletes originals. `-f/--quarantine-originals` moves
directory input originals and matching sidecars into quarantine only after:

- the converted temporary file has been copied
- the temporary and final SHA-256 hashes match
- the final image can be reopened and decoded
- the manifest record is ready to be written

Quarantine is still a migration action. Before large archival runs, prefer:

- run once without `-f`
- inspect a sample of outputs
- keep a separate backup
- use `--report` and review errors
- use `--skip-if-larger` if space savings are mandatory

Permanent deletion should be a separate manual cleanup after reviewing the
quarantine folder and `masa-cleanup-log.json`.

## Project Structure

```text
.
├── masa
├── pyproject.toml
├── requirements.txt
├── src/
│   └── python/
│       └── masa_cli/
│           ├── archive.py
│           ├── cli.py
│           ├── exif_handler.py
│           ├── image_processor.py
│           ├── manifest.py
│           └── ui.py
└── tests/
    ├── test_cli.py
    ├── test_exif_handler.py
    ├── test_integration.py
    └── test_manifest.py
```

## Remaining Improvements

- Add an explicit cleanup command that can permanently remove quarantined files
  after a reviewed report.
- Add a richer metadata audit that compares source and output EXIF fields when
  both formats support them.
- Add optional OS trash integration through a dependency such as `send2trash`.
- Add concurrent processing with bounded disk-space checks.
