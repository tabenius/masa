# Changelog

## 0.3.0 - Unreleased

- Add subcommands: `process`, `inspect`, `cleanup`, and `report`.
- Add local CI and release-check scripts instead of GitHub Actions.
- Add JSON Schemas for manifest, errors, reports, and cleanup logs.
- Add parallel processing with `--workers`.
- Add `--resume-errors`, `--min-savings-percent`, and richer dry-run planning.

## 0.2.0

- Move originals to quarantine instead of deleting them.
- Verify outputs before manifesting and quarantine.
- Add fallback controls, reports, errors, and integration tests.

## 0.1.0

- Initial MASA CLI implementation from the generated project spec.
