"""Secure intake of an uploaded image: decoded once, under limits, and re-encoded clean (I1).

Not reachable from the application. sanitize(data, declared_name) takes the raw bytes of one upload and
returns a new image Kalillac produced itself, or ImageError with a stable code that never echoes
content. Like intake.inspect(), it runs only inside the isolated file worker (worker.run_image), where a
hostile file meets the worker's memory, CPU, time and no-new-process limits.

Rules:
  * The format is decided from the content's signature, never from the name or a declared media type:
    PNG, JPEG, WebP and GIF only (sniff()). A declared extension that disagrees is refused, so nothing
    is smuggled under an image's name; SVG and every other format are refused (SVG is a document that
    can carry script and external references).
  * The decoder is restricted to the sniffed format. Dimensions are checked from the header before any
    pixel is decoded (MAX_SOURCE_PIXELS, MAX_SOURCE_SIDE), and Pillow's decompression-bomb warning is
    an error. Animated images (several frames) are refused rather than silently cut to one frame.
  * Nothing of the original file survives: the camera orientation is applied, colour is converted to
    sRGB when an ICC profile is present, the pixels are copied into a brand-new image with no metadata
    (EXIF, GPS, XMP, text chunks, comments, ICC profile), scaled down to fit MAX_OUTPUT_SIDE and encoded
    by Kalillac as PNG (transparency or lossless sources) or JPEG.
  * The output is bounded (MAX_OUTPUT_BYTES) and its own header is read back to confirm it.
"""
from __future__ import annotations

from dataclasses import dataclass
import io
import warnings

from .intake import MAX_UPLOAD_BYTES, declared_extension

MAX_SOURCE_PIXELS = 50_000_000
MAX_SOURCE_SIDE = 16_384
MAX_OUTPUT_SIDE = 2_048
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
JPEG_QUALITY = 90
# The format each signature identifies, and the declared extensions each may carry.
FORMATS = {"PNG": ("png",), "JPEG": ("jpg", "jpeg"), "WEBP": ("webp",), "GIF": ("gif",)}
EXTENSIONS = tuple(extension for names in FORMATS.values() for extension in names)
MEDIA = {"png": "image/png", "jpeg": "image/jpeg"}


class ImageError(ValueError):
    """A refused image. `code` is stable; the message is the code."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class SanitizedImage:
    kind: str            # "png" or "jpeg"
    data: bytes
    width: int
    height: int

    def __repr__(self):
        return f"SanitizedImage({self.kind}, {self.width}x{self.height}, {len(self.data)} bytes)"


def sniff(data):
    """The image format the content's signature names ("PNG", "JPEG", "WEBP", "GIF"), else None. Reads
    only the first bytes; safe outside the worker."""
    if type(data) is not bytes:
        return None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "PNG"
    if data.startswith(b"\xff\xd8\xff"):
        return "JPEG"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "WEBP"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "GIF"
    return None


def is_image_name(name):
    return declared_extension(name) in EXTENSIONS


def output_name(name, kind):
    """The stored name: the declared name's stem with the extension of what Kalillac encoded."""
    stem = name.rsplit(".", 1)[0] if type(name) is str and "." in name else (name or "image")
    return (stem.strip(" .") or "image")[:110] + (".png" if kind == "png" else ".jpg")


def _open(data, fmt):
    from PIL import Image
    try:
        image = Image.open(io.BytesIO(data), formats=[fmt])
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ImageError("image_too_large") from None   # Pillow's own header check, before any decoding.
    except Exception:
        raise ImageError("unreadable_image") from None
    width, height = image.size
    if not (1 <= width <= MAX_SOURCE_SIDE and 1 <= height <= MAX_SOURCE_SIDE) or width * height > MAX_SOURCE_PIXELS:
        raise ImageError("image_too_large")
    if getattr(image, "n_frames", 1) != 1 or getattr(image, "is_animated", False):
        raise ImageError("animated_image_unsupported")
    return image


def _decode(image, fmt):
    from PIL import ImageOps
    if fmt == "JPEG":
        # Let the decoder scale by 1/2, 1/4 or 1/8 while decoding: less memory, same final size.
        image.draft("RGB", (MAX_OUTPUT_SIDE, MAX_OUTPUT_SIDE))
    image.load()
    icc = image.info.get("icc_profile")
    try:
        image = ImageOps.exif_transpose(image)
    except Exception:
        pass                                    # Unreadable EXIF: the pixels as stored; the EXIF is dropped anyway.
    return image, icc if type(icc) is bytes and icc else None


def _to_srgb(image, icc):
    """sRGB pixels in RGB or RGBA. An ICC profile that cannot be used is dropped, never trusted."""
    from PIL import Image
    has_alpha = image.mode in ("RGBA", "LA", "PA") or (image.mode == "P" and "transparency" in image.info)
    target = "RGBA" if has_alpha else "RGB"
    if icc is not None and image.mode in ("RGB", "RGBA", "CMYK", "L"):
        try:
            from PIL import ImageCms
            source = ImageCms.ImageCmsProfile(io.BytesIO(icc))
            converted = ImageCms.profileToProfile(image, source, ImageCms.createProfile("sRGB"),
                                                  outputMode="RGBA" if image.mode == "RGBA" else "RGB")
            if converted is not None:
                image = converted
        except Exception:
            pass
    if image.mode != target:
        image = image.convert(target)
    if target == "RGBA" and image.getchannel("A").getextrema() == (255, 255):
        image = image.convert("RGB")           # Fully opaque: no transparency to keep.
    # A brand-new image from the pixels alone: nothing of the source's metadata can follow it.
    return Image.frombytes(image.mode, image.size, image.tobytes())


def _fit(image):
    from PIL import Image
    width, height = image.size
    scale = min(1.0, MAX_OUTPUT_SIDE / max(width, height))
    if scale < 1.0:
        size = (max(1, round(width * scale)), max(1, round(height * scale)))
        image = image.resize(size, Image.Resampling.LANCZOS)
    return image


def _encode(image, lossless):
    from PIL import Image
    if image.mode == "RGBA" or lossless:
        buffer = io.BytesIO()
        image.save(buffer, format="PNG", optimize=True)
        if buffer.tell() <= MAX_OUTPUT_BYTES:
            return "png", buffer.getvalue()
        if image.mode == "RGBA":                # Too large as PNG: flatten onto white for JPEG.
            flat = Image.new("RGB", image.size, (255, 255, 255))
            flat.paste(image, mask=image.getchannel("A"))
            image = flat
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    if buffer.tell() > MAX_OUTPUT_BYTES:
        raise ImageError("image_too_large")
    return "jpeg", buffer.getvalue()


def _confirm(kind, data):
    """Read Kalillac's own output back: the expected format, one frame, within the output bounds."""
    from PIL import Image
    try:
        with Image.open(io.BytesIO(data), formats=["PNG" if kind == "png" else "JPEG"]) as check:
            width, height = check.size
            if not (1 <= width <= MAX_OUTPUT_SIDE and 1 <= height <= MAX_OUTPUT_SIDE) \
                    or check.info.get("exif") or check.info.get("icc_profile"):
                raise ImageError("unreadable_image")
            check.load()
    except ImageError:
        raise
    except Exception:
        raise ImageError("unreadable_image") from None
    return width, height


def sanitize(data, declared_name=None):
    """SanitizedImage for one upload, or ImageError."""
    if type(data) is not bytes:
        raise ImageError("invalid_upload")
    if not data:
        raise ImageError("empty_file")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ImageError("file_too_large")
    fmt = sniff(data)
    if fmt is None:
        raise ImageError("unsupported_type")
    extension = declared_extension(declared_name)
    if declared_name is not None and extension not in FORMATS[fmt]:
        raise ImageError("type_mismatch" if extension in EXTENSIONS else "unsupported_type")
    from PIL import Image
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        try:
            image = _open(data, fmt)
            try:
                image, icc = _decode(image, fmt)
                clean = _fit(_to_srgb(image, icc))
            finally:
                image.close()
        except (ImageError, MemoryError):
            raise
        except Exception:
            raise ImageError("unreadable_image") from None
    kind, encoded = _encode(clean, lossless=fmt in ("PNG", "GIF"))
    width, height = _confirm(kind, encoded)
    return SanitizedImage(kind, encoded, width, height)
