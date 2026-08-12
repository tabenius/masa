import os
import tempfile

from PIL import Image

try:
    import pillow_avif  # noqa: F401
except ImportError:
    pass


def process_single_image(
    input_path: str,
    max_dim: int,
    quality: int,
    exif_bytes: bytes,
) -> tuple[str, str, str]:
    with Image.open(input_path) as img:
        img_format = (img.format or "").upper()

        if img_format in ("JPEG", "JPG"):
            out_format = "AVIF"
            out_ext = ".avif"
        else:
            out_format = "WEBP"
            out_ext = ".webp"

        if img.mode in ("RGBA", "P") and out_format == "AVIF":
            img = img.convert("RGB")

        width, height = img.size
        if width > max_dim or height > max_dim:
            img.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)

        fd, temp_file_path = tempfile.mkstemp(prefix="masa_proc_", suffix=out_ext)
        os.close(fd)

        save_kwargs = {}
        if exif_bytes:
            save_kwargs["exif"] = exif_bytes

        if out_format == "AVIF":
            save_kwargs["quality"] = quality
            img.save(temp_file_path, format="AVIF", **save_kwargs)
        else:
            save_kwargs["lossless"] = True
            img.save(temp_file_path, format="WEBP", **save_kwargs)

        return temp_file_path, out_ext, out_format
