from pathlib import Path

from manifest import ManifestManager, compute_sha256


def test_manifest_json_roundtrip(tmp_path: Path) -> None:
    manager = ManifestManager(str(tmp_path), "json")
    manager.add_record("photo.jpg", {"output_file_name": "2020/photo.avif"})

    reloaded = ManifestManager(str(tmp_path), "json")

    assert reloaded.is_processed("photo.jpg")
    assert reloaded.records["photo.jpg"]["output_file_name"] == "2020/photo.avif"


def test_compute_sha256(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("masa\n", encoding="utf-8")

    assert compute_sha256(str(target)) == "4467d32a82ff8262d407a6aba61844df3d7a271b4060c49f003cd25722e79665"
