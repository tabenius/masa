# MASA - Media Archive Structuring & Archival

MASA is a Python CLI tool for processing Google Takeout photo archives from a
directory, ZIP file, or TAR/TAR.GZ file. It reads Google Photos JSON sidecars,
embeds available date and GPS metadata, downscales images, converts output to
AVIF or WEBP, writes a cryptographic manifest, and can optionally remove
processed originals after successful output when `-f/--delete-originals` is set.

## Features

- Accepts a Takeout folder, `.zip`, `.tar`, `.tar.gz`, or other tar-compatible
  archive.
- Safely extracts archives into a private temporary workspace.
- Finds common Google Photos JSON sidecar names, including duplicate filename
  patterns such as `IMG_0001.jpg(1).json`.
- Uses timezone-aware UTC `photoTakenTime.timestamp` values from sidecars when
  available, falling back to the source file modification time.
- Embeds EXIF date fields and GPS coordinates when metadata is available.
- Downscales images to a configurable maximum dimension.
- Converts JPEG/JPG sources to AVIF when AVIF support is available.
- Converts PNG, GIF, WEBP, TIFF/TIF, and BMP sources to lossless WEBP when WEBP
  support is available.
- If AVIF support is missing, asks whether to use JPEG instead at slightly lower
  quality.
- If WEBP support is missing for lossless-style inputs such as GIF or PNG, asks
  whether to use optimized palette PNG instead.
- Keeps source files by default. `-f/--delete-originals` is required to remove
  directory input originals and sidecars after successful output.
- Avoids output filename collisions by appending numeric suffixes such as
  `-001`.
- Writes year-based output folders, with optional `YYYY/MM` month stratification.
- Writes a resumable manifest in JSON or YAML with original and output hashes,
  sizes, formats, dimensions, output path, and date taken.
- Skips files already present in the manifest on later runs.
- Polls for free disk space before processing each file.
- Supports dry runs for a non-writing preview.
- Uses colored terminal progress and a colored `--help` screen. Set
  `NO_COLOR=1` to disable ANSI color output or `FORCE_COLOR=1` to force it in
  environments where color has been disabled.

## Install

Use Python 3.10 or newer.

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
```

AVIF output requires `pillow-avif-plugin`, which is included in the project
dependencies.

For development and tests:

```bash
python -m pip install -e ".[dev]"
python -m pytest
```

## Usage

Show help:

```bash
./masa --help
```

Process a Takeout directory. Originals are kept by default:

```bash
./masa /path/to/takeout
```

Process a ZIP archive, group output by year and month, and tune image quality:

```bash
./masa /path/to/takeout.zip --by-month --max-dim 2048 --quality 82
```

Write to an explicit output directory:

```bash
./masa /path/to/takeout -o /path/to/output
```

Write a YAML manifest:

```bash
./masa /path/to/takeout --format yaml
```

Delete originals and sidecars after successful conversion of a directory input:

```bash
./masa /path/to/takeout -f
```

Preview without writing output, deleting originals, or saving manifest records:

```bash
./masa /path/to/takeout --dry-run
```

Disable color output:

```bash
NO_COLOR=1 ./masa --help
```

Force color output:

```bash
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
: AVIF quality for JPEG/JPG inputs. If AVIF is unavailable and you accept the
  JPEG fallback, the fallback JPEG is saved at `quality - 10`. If WEBP is
  unavailable and you accept the PNG fallback, MASA writes an optimized
  palette-based PNG. Defaults to `80`.

`-f, --delete-originals`
: Delete original files and JSON sidecars after successful processing when the
  input is a plain directory. Archive inputs are extracted into temporary space,
  so the original archive is not deleted.

`--keep-original`
: Compatibility no-op. Originals are kept unless `-f/--delete-originals` is set.

`--dry-run`
: Read inputs and show the summary without writing output, writing manifest
  records, or deleting files.

`--format {json,yaml}`
: Manifest format. Defaults to `json`.

## Output Layout

Without `--by-month`:

```text
output/
  masa.json
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

## Manifest

Each processed file is recorded under its relative input path. A record includes:

- original filename, size, format, dimensions, and SHA-256 hash
- output filename, size, format, and SHA-256 hash
- whether EXIF bytes were embedded
- date used for output organization

The manifest is used for resumability. Re-running the same command against the
same output directory skips records that are already present.

## Important Safety Notes

MASA keeps originals by default. Passing `-f/--delete-originals` changes that:
after an output file is written, hashed, and recorded, MASA removes the source
image and matching JSON sidecar for directory inputs.

Default deletion would be dangerous for this kind of tool because archival photo
processing has several failure modes that are hard to detect immediately:

- encoder bugs or unsupported metadata can produce a readable but lower-value
  output file
- AVIF/JPEG/PNG fallbacks may not preserve every property from the source
- EXIF embedding can silently fail when `piexif` is missing or rejects metadata
- a bad output directory choice can put the only copy on the wrong disk
- user expectations differ: some people want compression copies, not migration

Further mitigations worth adding before trusting `-f` for large archives:

- move originals to a quarantine folder or OS trash instead of deleting them
- add a `--confirm-delete` prompt summarizing file count and total bytes
- require `--backup-dir` for destructive runs
- verify each output can be reopened after copying and before removing input
- write a deletion log or undo script with every removed path
- keep originals when the output is larger than the source unless explicitly
  overridden

## Project Structure

```text
.
├── masa
├── pyproject.toml
├── requirements.txt
├── src/
│   └── python/
│       ├── archive.py
│       ├── cli.py
│       ├── exif_handler.py
│       ├── image_processor.py
│       ├── manifest.py
│       └── ui.py
└── tests/
    ├── test_exif_handler.py
    └── test_manifest.py
```

## Improvement Suggestions

- Add integration tests with small JPEG, PNG, sidecar JSON, ZIP, and TAR
  fixtures.
- Add a structured `--report` option that writes the summary as JSON for scripts.
- Add support for preserving selected original files when conversion output is
  larger than the source.
- Add a quarantine/trash deletion mode before using `-f` on large archives.
- Add a package namespace such as `masa_cli` to avoid top-level module names like
  `archive.py` and `cli.py` when installed into shared environments.
