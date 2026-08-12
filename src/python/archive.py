import os
import tarfile
import tempfile
import zipfile
from pathlib import Path


def _is_within_directory(base_dir: str, target_path: str) -> bool:
    base = Path(base_dir).resolve()
    target = Path(target_path).resolve()
    try:
        target.relative_to(base)
        return True
    except ValueError:
        return False


def _safe_extract_zip(zip_file: zipfile.ZipFile, destination: str) -> None:
    for member in zip_file.infolist():
        target = os.path.join(destination, member.filename)
        if not _is_within_directory(destination, target):
            raise ValueError(f"Unsafe ZIP member path: {member.filename}")
    zip_file.extractall(destination)


def _safe_extract_tar(tar_file: tarfile.TarFile, destination: str) -> None:
    for member in tar_file.getmembers():
        target = os.path.join(destination, member.name)
        if not _is_within_directory(destination, target):
            raise ValueError(f"Unsafe TAR member path: {member.name}")
    tar_file.extractall(destination)


def detect_and_prepare_input(input_path: str, temp_base_dir: str) -> tuple[str, bool]:
    if not os.path.exists(input_path):
        raise FileNotFoundError(f"Input path does not exist: {input_path}")

    if os.path.isdir(input_path):
        return os.path.abspath(input_path), False

    if zipfile.is_zipfile(input_path):
        secure_temp = tempfile.mkdtemp(prefix="masa_zip_", dir=temp_base_dir)
        os.chmod(secure_temp, 0o700)
        with zipfile.ZipFile(input_path, "r") as zf:
            _safe_extract_zip(zf, secure_temp)
        return secure_temp, True

    if tarfile.is_tarfile(input_path):
        secure_temp = tempfile.mkdtemp(prefix="masa_tar_", dir=temp_base_dir)
        os.chmod(secure_temp, 0o700)
        with tarfile.open(input_path, "r:*") as tf:
            _safe_extract_tar(tf, secure_temp)
        return secure_temp, True

    raise ValueError(f"Unsupported file format or invalid directory: {input_path}")
