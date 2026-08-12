from pathlib import Path

from cli import _resolve_output_format, _unique_destination_path


def test_unique_destination_path_adds_suffix(tmp_path: Path) -> None:
    existing = tmp_path / "2024" / "photo.webp"
    existing.parent.mkdir()
    existing.write_text("already here", encoding="utf-8")

    assert _unique_destination_path(str(existing), dry_run=False).endswith("photo-001.webp")


def test_unique_destination_path_dry_run_does_not_add_suffix(tmp_path: Path) -> None:
    existing = tmp_path / "photo.webp"
    existing.write_text("already here", encoding="utf-8")

    assert _unique_destination_path(str(existing), dry_run=True) == str(existing)


def test_avif_missing_can_fall_back_to_jpeg() -> None:
    decisions = {"avif_to_jpeg": True}

    result = _resolve_output_format("JPEG", lambda format_name: False, decisions)

    assert result == (".jpg", "JPEG")


def test_webp_missing_can_fall_back_to_png_for_lossless_source() -> None:
    decisions = {"webp_to_png": True}

    result = _resolve_output_format("PNG", lambda format_name: False, decisions)

    assert result == (".png", "PNG")
