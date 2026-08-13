# MASA - Media Archive Structuring & Archival

MASA is a Python CLI for processing Google Takeout photo archives from a
directory, ZIP file, or TAR/TAR.GZ file. It reads Google Photos JSON sidecars,
adds available date/GPS metadata, downscales images, converts outputs to modern
formats, verifies the written files, and records cryptographic manifests.

MASA keeps originals by default. `-f/--quarantine-originals` moves originals and
sidecars to a quarantine folder after verified output; it does not permanently
delete them. Use `masa cleanup` later if you decide to delete quarantined files.

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
- Verifies final image readability and performs best-effort EXIF presence checks
  where output formats support EXIF.
- Downscales images to a configurable maximum dimension.
- Converts JPEG/JPG to AVIF when AVIF support is available.
- Converts PNG, GIF, WEBP, TIFF/TIF, and BMP to lossless WEBP when WEBP support
  is available.
- Supports interactive fallback prompts plus `--yes-fallbacks`,
  `--no-fallbacks`, and `--fail-on-fallback` for automation.
- Supports `--workers N` for parallel processing. Worker mode requires one of
  the noninteractive fallback flags.
- Avoids output filename collisions by appending numeric suffixes such as
  `-001`.
- Supports `--resume-errors` to rerun only files listed in a previous
  `masa-errors.json`.
- Supports `--skip-if-larger`, `--keep-if-larger`, and
  `--min-savings-percent` policies.
- Writes JSON or YAML manifests, `masa-errors.json`, `masa-cleanup-log.json`,
  structured `--report` output, and JSON Schemas under `schemas/`.
- Provides subcommands: `process`, `inspect`, `cleanup`, and `report`.
- Provides colored progress/help output, plus `NO_COLOR=1` and `FORCE_COLOR=1`.

## Install

Use Python 3.10 or newer.

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
```

For development:

```bash
python -m pip install -e ".[dev]"
./scripts/ci.sh
./scripts/release-check.sh
```

There are intentionally no GitHub Actions in this repository right now, to avoid
using billed GitHub CI minutes. The scripts above are the local CI/release gate.

AVIF output depends on `pillow-avif-plugin`, which is listed in the project
dependencies. If AVIF is unavailable at runtime, MASA can fall back to JPEG.

## Usage

Show help:

```bash
./masa --help
./masa process --help
```

Process a Takeout directory. Originals are kept:

```bash
./masa process /path/to/takeout
```

The legacy flat form is still accepted:

```bash
./masa /path/to/takeout
```

Process a ZIP archive by year/month using four workers:

```bash
./masa process /path/to/takeout.zip --by-month --workers 4 --yes-fallbacks
```

Write to an explicit output directory and create a structured report:

```bash
./masa process /path/to/takeout -o /path/to/output --report /path/to/report.json
```

Move originals and sidecars to quarantine after successful verification:

```bash
./masa process /path/to/takeout -f --quarantine-dir /path/to/quarantine
```

Preview planned outputs, fallback decisions, and quarantine intent:

```bash
./masa process /path/to/takeout --dry-run --report dry-run.json
```

Rerun only files from a previous error manifest:

```bash
./masa process /path/to/takeout --resume-errors /path/to/output/masa-errors.json
```

Inspect an output directory or manifest:

```bash
./masa inspect /path/to/output
./masa inspect /path/to/output/masa.json
```

Summarize a run report, errors file, or cleanup log:

```bash
./masa report /path/to/report.json
./masa report /path/to/output/masa-errors.json
```

Review or permanently delete quarantined files:

```bash
./masa cleanup /path/to/output/masa-cleanup-log.json --dry-run
./masa cleanup /path/to/output/masa-cleanup-log.json --yes
```

Disable or force color output:

```bash
NO_COLOR=1 ./masa --help
FORCE_COLOR=1 ./masa --help
```

## Process Options

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

`--yes-fallbacks`, `--no-fallbacks`, `--fail-on-fallback`
: Control JPEG/PNG fallback behavior without interactive prompts.

`--workers`
: Number of worker threads. Values above `1` require one of the noninteractive
  fallback flags.

`--skip-if-larger`, `--keep-if-larger`
: Remove a converted output and record an error if the output is larger than the
  source.

`--min-savings-percent`
: Remove a converted output and record an error if it saves less than the
  requested percentage.

`--resume-errors`
: Only process relative input paths listed in a previous `masa-errors.json`.

`--report`
: Write a structured JSON run report with totals, planned records, processed
  records, fallback decisions, quarantine actions, and errors.

`--errors`
: Error manifest path. Defaults to `OUTPUT/masa-errors.json` when failures
  occur.

`--quiet`, `--verbose`
: Suppress per-file progress or print a final failed-file table.

`--dry-run`
: Plan a run without writing outputs, manifests, error files, or quarantine
  logs. `--report` is still written if explicitly requested.

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

## Manifest, Reports, And Schemas

Each processed file is recorded under its relative input path. Records include:

- original filename, size, format, dimensions, and SHA-256 hash
- output filename, size, format, and SHA-256 hash
- `output_verified`
- `metadata_verification`
- whether EXIF bytes were embedded
- date used for output organization

Schema files:

- `schemas/manifest.schema.json`
- `schemas/errors.schema.json`
- `schemas/cleanup-log.schema.json`
- `schemas/report.schema.json`

## Safety Notes

MASA does not hard-delete originals during processing. `-f/--quarantine-originals`
moves directory input originals and matching sidecars into quarantine only after:

- the converted temporary file has been copied
- the temporary and final SHA-256 hashes match
- the final image can be reopened and decoded
- the manifest record is written

Quarantine is still a migration action. Before large archival runs:

- run once without `-f`
- inspect a sample of outputs
- keep a separate backup
- use `--report` and review errors
- use `--skip-if-larger` or `--min-savings-percent` if space savings are
  mandatory

Permanent deletion is handled by `masa cleanup` after reviewing the quarantine
folder and `masa-cleanup-log.json`.

## Project Structure

```text
.
├── masa
├── pyproject.toml
├── requirements.txt
├── scripts/
│   ├── ci.sh
│   └── release-check.sh
├── schemas/
│   ├── cleanup-log.schema.json
│   ├── errors.schema.json
│   ├── manifest.schema.json
│   └── report.schema.json
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

- Add optional OS trash integration through a dependency such as `send2trash`.
- Add process-level benchmarks for large archives and tune `--workers` defaults.
- Add deeper metadata comparison for formats/Pillow builds that preserve EXIF
  consistently.
