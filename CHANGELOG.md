# Changelog

## 0.3.0 - Unreleased

- Add subcommands: `process`, `inspect`, `cleanup`, and `report`.
- Add local CI and release-check scripts instead of GitHub Actions.
- Add JSON Schemas for manifest, errors, reports, and cleanup logs.
- Add parallel processing with `--workers`.
- Add `--resume-errors`, `--min-savings-percent`, and richer dry-run planning.
- Add optional `cleanup --trash` support through the `trash` extra.
- Add `benchmark` subcommand for worker throughput checks.
- Add expected/actual metadata verification details for EXIF date and GPS.
- Add `doctor` command for dependency and encoder diagnostics.
- Add `validate` command for MASA JSON files, backed by bundled schemas.
- Add `verify` command for manifest output hash/readability audits.
- Add `restore` command for undoing quarantine moves from cleanup logs.

## 0.2.0

- Move originals to quarantine instead of deleting them.
- Verify outputs before manifesting and quarantine.
- Add fallback controls, reports, errors, and integration tests.

## 0.1.0

- Initial MASA CLI implementation from the generated project spec.
