import os
import tempfile

from PIL import Image, features

try:
    import pillow_avif  # noqa: F401
except ImportError:
    pass


def can_save_format(format_name: str) -> bool:
    Image.init()
    format_name = format_name.upper()
    if format_name == "WEBP":
        return bool(features.check("webp"))
    if format_name == "AVIF":
        return "AVIF" in Image.SAVE or Image.registered_extensions().get(".avif") == "AVIF"
    return format_name in Image.SAVE


def process_single_image(
    input_path: str,
    max_dim: int,
    quality: int,
    exif_bytes: bytes,
    output_format: str | None = None,
) -> tuple[str, str, str]:
    with Image.open(input_path) as img:
        img_format = (img.format or "").upper()

        if output_format:
            out_format = output_format.upper()
            out_ext = ".jpg" if out_format == "JPEG" else f".{out_format.lower()}"
        elif img_format in ("JPEG", "JPG"):
            out_format = "AVIF"
            out_ext = ".avif"
        else:
            out_format = "WEBP"
            out_ext = ".webp"

        if img.mode in ("RGBA", "P") and out_format in ("AVIF", "JPEG"):
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
        elif out_format == "JPEG":
            save_kwargs["quality"] = max(1, quality - 10)
            save_kwargs["optimize"] = True
            img.save(temp_file_path, format="JPEG", **save_kwargs)
        elif out_format == "PNG":
            save_kwargs.pop("exif", None)
            if img.mode in ("RGB", "RGBA", "L"):
                colors = max(16, min(256, int((quality / 100) * 256)))
                method = Image.Quantize.FASTOCTREE if img.mode == "RGBA" else Image.Quantize.MEDIANCUT
                img = img.quantize(colors=colors, method=method)
            save_kwargs["optimize"] = True
            img.save(temp_file_path, format="PNG", **save_kwargs)
        else:
            save_kwargs["lossless"] = True
            img.save(temp_file_path, format="WEBP", **save_kwargs)

        return temp_file_path, out_ext, out_format
