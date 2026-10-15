from __future__ import annotations

import io
import math
import os
import struct
import threading
import concurrent.futures
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

from PIL import Image, ImageChops, ImageEnhance, ImageFilter, ImageOps, features as pil_features
from pypdf import PdfWriter, filters as pypdf_filters
from pypdf.errors import LimitReachedError, PdfReadError

try:  # pypdf >= 6.20 reads its limits from Configuration
    from pypdf import overwrite_configuration as _overwrite_configuration
except ImportError:  # pragma: no cover - older pypdf reads the module constants
    _overwrite_configuration = None
from pypdf.generic import (
    ArrayObject,
    BooleanObject,
    DictionaryObject,
    NameObject,
    NumberObject,
    StreamObject,
    TextStringObject,
)


ProgressCallback = Callable[[str, int, int], None]
_LARGE_STREAM_LIMIT = 120_000_000
# pypdf caps decompressed stream output to guard against hostile files. Trusted
# local drawings exceed the 75MB default (one 5074x5166 8bit RGB Flate image is
# 78MB), so the ceiling is raised for the run. Which knob the installed version
# reads changed in 6.20 (module constants -> Configuration), so both are set.
_LEGACY_LIMIT_NAMES = (
    "ZLIB_MAX_OUTPUT_LENGTH",
    "FLATE_MAX_BUFFER_SIZE",
    "MAX_DECLARED_STREAM_LENGTH",
    "MAX_ARRAY_BASED_STREAM_OUTPUT_LENGTH",
    "LZW_MAX_OUTPUT_LENGTH",
    "RUN_LENGTH_MAX_OUTPUT_LENGTH",
    "JBIG2_MAX_OUTPUT_LENGTH",
)


def _raise_decompression_limits() -> None:
    """Raise pypdf's decompression ceilings for trusted local drawing files."""
    for name in _LEGACY_LIMIT_NAMES:
        current = getattr(pypdf_filters, name, None)
        if isinstance(current, int):
            setattr(pypdf_filters, name, max(current, _LARGE_STREAM_LIMIT))
    if _overwrite_configuration is not None:
        _overwrite_configuration(
            maximum_declared_stream_length=_LARGE_STREAM_LIMIT,
            array_based_stream_maximum_output_length=_LARGE_STREAM_LIMIT,
            zlib_maximum_output_length=_LARGE_STREAM_LIMIT,
            image_maximum_buffer_size=_LARGE_STREAM_LIMIT,
        )


@dataclass(frozen=True)
class OptimizeOptions:
    dpi: int = 300
    auto_contrast: bool = True
    contrast: float = 1.15
    sharpen: bool = True
    denoise: bool = False
    advanced_processing: bool = False
    threshold_offset: int = 0
    include_small_images: bool = False
    strip_metadata: bool = True
    # Backgrounds exported as horizontal strips can be narrower than 1200px.
    min_long_side: int = 512
    min_pixels: int = 1_000_000


@dataclass
class OptimizeResult:
    input_path: Path
    output_path: Path
    total_images: int = 0
    converted_images: int = 0
    skipped_small: int = 0
    skipped_inline: int = 0
    skipped_unsupported: int = 0
    bytes_before: int = 0
    bytes_after: int = 0
    metadata_stripped: bool = False
    warnings: list[str] = field(default_factory=list)


class CancelledError(RuntimeError):
    pass


def _a3_pixel_box(image: Image.Image, dpi: int) -> tuple[int, int]:
    short_side = round(11.6929 * dpi)
    long_side = round(16.5354 * dpi)
    if image.width >= image.height:
        return long_side, short_side
    return short_side, long_side


def _otsu_threshold(image: Image.Image) -> int:
    histogram = image.histogram()
    total = image.width * image.height
    weighted_total = sum(i * count for i, count in enumerate(histogram))
    background_weight = 0
    background_sum = 0
    best_variance = -1.0
    best_threshold = 127

    for threshold, count in enumerate(histogram):
        background_weight += count
        if background_weight == 0:
            continue
        foreground_weight = total - background_weight
        if foreground_weight == 0:
            break
        background_sum += threshold * count
        background_mean = background_sum / background_weight
        foreground_mean = (weighted_total - background_sum) / foreground_weight
        variance = background_weight * foreground_weight * (
            background_mean - foreground_mean
        ) ** 2
        if variance > best_variance:
            best_variance = variance
            best_threshold = threshold
    return best_threshold


def _prepare_image(image: Image.Image, options: OptimizeOptions) -> tuple[Image.Image, int]:
    image.load()
    if image.mode in ("RGBA", "LA") or "transparency" in image.info:
        rgba = image.convert("RGBA")
        white = Image.new("RGBA", rgba.size, "white")
        white.alpha_composite(rgba)
        image = white.convert("RGB")

    # Cyan/blue blueprint backgrounds become dark in luminance. The red
    # channel keeps white paper/lines bright while suppressing the blue dye;
    # for grayscale input it is identical to luminance.
    working = image.convert("RGB")
    max_size = _a3_pixel_box(working, options.dpi)
    if working.width > max_size[0] or working.height > max_size[1]:
        working.thumbnail(max_size, Image.Resampling.LANCZOS)
    gray = working.getchannel("R")

    # Remove slow paper/stain illumination changes before thresholding.
    # ponytail: fixed-radius local normalization; upgrade to adaptive windows
    # only if mixed-scale drawings still show measurable background loss.
    background = gray.filter(ImageFilter.GaussianBlur(radius=25))
    gray = ImageChops.subtract(gray, background, offset=128)
    if options.advanced_processing:
        red = working.getchannel("R")
        blue = working.getchannel("B")
        blue_score = ImageChops.subtract(blue, red, offset=128)
        blue_mask = blue_score.point(lambda value: 255 if value >= 145 else 0)
        blue_mask = blue_mask.filter(ImageFilter.MedianFilter(size=3))
        gray = ImageChops.subtract(gray, blue_mask.point(lambda value: 10 if value else 0))

    if options.auto_contrast:
        gray = ImageOps.autocontrast(gray, cutoff=0.5)
    if options.denoise:
        gray = gray.filter(ImageFilter.MedianFilter(size=3))
    if options.contrast != 1.0:
        gray = ImageEnhance.Contrast(gray).enhance(options.contrast)
    if options.sharpen:
        gray = gray.filter(ImageFilter.UnsharpMask(radius=1.0, percent=130, threshold=3))

    threshold = max(0, min(255, _otsu_threshold(gray) + options.threshold_offset))
    table = [0 if value <= threshold else 255 for value in range(256)]
    binary = gray.point(table, mode="1")
    # ponytail: line drawings should have a white majority; invert blueprints
    # whose blue paper was classified as the dark background by Otsu.
    histogram = binary.histogram()
    if histogram[0] > histogram[255]:
        binary = ImageOps.invert(binary.convert("L")).convert("1")
    return binary, threshold


def _advanced_profile(image: Image.Image) -> tuple[bool, float]:
    """Return (needs_denoise, ambiguity_score) from a small preview."""
    image.load()
    preview = image.convert("RGB").getchannel("R")
    preview.thumbnail((512, 512), Image.Resampling.BILINEAR)
    background = preview.filter(ImageFilter.GaussianBlur(radius=5))
    preview = ImageChops.subtract(preview, background, offset=128)
    preview = ImageOps.autocontrast(preview, cutoff=0.5)
    threshold = _otsu_threshold(preview)
    histogram = preview.histogram()
    total = preview.width * preview.height
    black_ratio = sum(histogram[: threshold + 1]) / max(total, 1)
    ambiguity = sum(histogram[max(0, threshold - 10) : min(256, threshold + 11)]) / max(total, 1)
    return black_ratio >= 0.20 or ambiguity >= 0.12, ambiguity


def _prepare_image_task(image: Image.Image, options: OptimizeOptions) -> tuple[Image.Image, int]:
    return _prepare_image(image, options)


def _encode_ccitt_g4(image: Image.Image) -> bytes | None:
    """Return a CCITT Group 4 stream for a 1bit image, or None when unavailable.

    Reuses Pillow's TIFF Group 4 encoder (libtiff) with a single strip — the same
    route Pillow's own PDF writer takes for 1bit images — and slices the strip out
    of the TIFF container. Reading the strip bounds from the produced tags keeps
    this correct if Pillow ever changes the TIFF layout.
    """
    if not pil_features.check("libtiff"):
        return None
    width, height = image.size
    buffer = io.BytesIO()
    try:
        image.save(
            buffer, "TIFF", compression="group4", strip_size=math.ceil(width / 8) * height
        )
    except (OSError, ValueError):
        return None
    raw = buffer.getvalue()
    if len(raw) < 8 or raw[:4] != b"II*\x00":
        return None
    ifd_offset = struct.unpack("<I", raw[4:8])[0]
    if ifd_offset + 2 > len(raw):
        return None
    tag_count = struct.unpack("<H", raw[ifd_offset : ifd_offset + 2])[0]
    tags: dict[int, int] = {}
    for index in range(tag_count):
        offset = ifd_offset + 2 + index * 12
        if offset + 12 > len(raw):
            return None
        tag = struct.unpack("<H", raw[offset : offset + 2])[0]
        tags[tag] = struct.unpack("<I", raw[offset + 8 : offset + 12])[0]
    strip_offset = tags.get(273)  # StripOffsets
    strip_bytes = tags.get(279)  # StripByteCounts
    if strip_offset != 8 or not strip_bytes or tags.get(278) != height:  # RowsPerStrip
        return None
    strip = raw[strip_offset : strip_offset + strip_bytes]
    return strip or None


def _replace_with_ccitt(image_file: object, binary: Image.Image) -> bool:
    """Write the prepared 1bit image into its existing XObject as CCITT G4.

    pypdf's ImageFile.replace() round-trips the image through a temporary PDF
    (Pillow write, parse, decode, re-encode). Writing the stream in place keeps
    the same bytes with a single encode. Returns False when the fast path is not
    usable, so the caller can fall back to replace().
    """
    reference = getattr(image_file, "indirect_reference", None)
    if reference is None:
        return False
    stream = _encode_ccitt_g4(binary)
    if stream is None:
        return False
    try:
        stream_object = reference.get_object()
    except (OSError, ValueError, TypeError):
        return False
    if not isinstance(stream_object, StreamObject) or stream_object.get("/Subtype") != "/Image":
        return False
    width, height = binary.size
    # pypdf compresses Flate through set_data(); assign the encoded bytes directly.
    stream_object._data = stream
    stream_object[NameObject("/Type")] = NameObject("/XObject")
    stream_object[NameObject("/Subtype")] = NameObject("/Image")
    stream_object[NameObject("/Width")] = NumberObject(width)
    stream_object[NameObject("/Height")] = NumberObject(height)
    stream_object[NameObject("/BitsPerComponent")] = NumberObject(1)
    stream_object[NameObject("/ColorSpace")] = NameObject("/DeviceGray")
    stream_object[NameObject("/Filter")] = ArrayObject([NameObject("/CCITTFaxDecode")])
    stream_object[NameObject("/DecodeParms")] = ArrayObject(
        [
            DictionaryObject(
                {
                    NameObject("/K"): NumberObject(-1),
                    NameObject("/BlackIs1"): BooleanObject(True),
                    NameObject("/Columns"): NumberObject(width),
                    NameObject("/Rows"): NumberObject(height),
                }
            )
        ]
    )
    # The prepared image is already composited on white; the old mask and decode
    # mapping belong to the replaced bitmap.
    for key in ("/SMask", "/Mask", "/Decode", "/Interpolate", "/Intent"):
        if key in stream_object:
            del stream_object[NameObject(key)]
    image_file.image = binary
    image_file.data = stream
    return True


def _image_key(image_file: object) -> tuple[int, int] | None:
    reference = getattr(image_file, "indirect_reference", None)
    if reference is None:
        return None
    return int(reference.idnum), int(reference.generation)


def _collect_images(writer: PdfWriter, result: OptimizeResult) -> list[tuple[int, object]]:
    images: list[tuple[int, object]] = []
    seen: set[tuple[int, int]] = set()
    for page_number, page in enumerate(writer.pages, start=1):
        try:
            keys = list(page.images.keys())
        except Exception as exc:
            raise RuntimeError(
                f"{page_number}ページの画像一覧を取得できないため、処理を中止しました: {exc}"
            ) from exc
        for key in keys:
            try:
                image_file = page.images[key]
                object_key = _image_key(image_file)
                if object_key is None:
                    result.skipped_inline += 1
                    continue
                if object_key in seen:
                    continue
                seen.add(object_key)
                images.append((page_number, image_file))
            except Exception as exc:
                raise RuntimeError(
                    f"{page_number}ページの画像 {key} を読み込めないため、"
                    f"混在を防ぐため処理を中止しました: {exc}"
                ) from exc
    return images




def _strip_pdf_metadata(writer: PdfWriter) -> bool:
    """Remove publication-risk metadata from the PDF container.

    This strips the trailer /Info dictionary (Title, Author, Creator,
    Producer, dates, etc.), the document catalog XMP /Metadata stream,
    and the trailer /ID. It does not remove visible text, annotations,
    embedded files, OCR text, or layer names.
    """
    changed = False

    if getattr(writer, "_info", None) is not None:
        writer._info = None
        changed = True

    if getattr(writer, "_ID", None) is not None:
        writer._ID = None
        changed = True

    try:
        if NameObject("/Metadata") in writer.root_object:
            del writer.root_object[NameObject("/Metadata")]
            changed = True
    except (AttributeError, KeyError, TypeError, ValueError):
        pass

    return changed


def _is_signature_present(writer: PdfWriter) -> bool:
    try:
        acroform = writer.root_object.get("/AcroForm")
        if acroform is None:
            return False
        fields = list(acroform.get_object().get("/Fields", []))
        while fields:
            field = fields.pop().get_object()
            if field.get("/FT") == "/Sig":
                return True
            fields.extend(field.get("/Kids", []))
    except (AttributeError, TypeError, ValueError, KeyError):
        return False
    return False


def optimize_pdf(
    input_path: str | os.PathLike[str],
    output_path: str | os.PathLike[str],
    options: OptimizeOptions,
    progress: ProgressCallback | None = None,
    cancel_event: threading.Event | None = None,
) -> OptimizeResult:
    source = Path(input_path)
    target = Path(output_path)
    result = OptimizeResult(source, target, bytes_before=source.stat().st_size)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".partial")
    if temporary.exists():
        temporary.unlink()

    try:
        _raise_decompression_limits()
        writer = PdfWriter(clone_from=source)
        if _is_signature_present(writer):
            result.warnings.append("電子署名は保存時に無効になります。")

        if options.strip_metadata:
            result.metadata_stripped = _strip_pdf_metadata(writer)

        images = _collect_images(writer, result)
        result.total_images = len(images) + result.skipped_inline
        page_profiles: dict[int, bool] = {}
        if options.advanced_processing:
            page_scores: dict[int, list[bool]] = {}
            for page_number, image_file in images:
                image = image_file.image
                if image is not None:
                    needs_denoise, _ = _advanced_profile(image)
                    page_scores.setdefault(page_number, []).append(needs_denoise)
            page_profiles = {
                page_number: sum(scores) > len(scores) / 2
                for page_number, scores in page_scores.items()
            }
        conversion_errors: list[str] = []
        work_items: list[tuple[int, object, Image.Image, OptimizeOptions]] = []

        for index, (page_number, image_file) in enumerate(images, start=1):
            if cancel_event is not None and cancel_event.is_set():
                raise CancelledError("処理を中止しました。")
            try:
                image = image_file.image
                if image is None:
                    raise ValueError("画像を復号できません")
                width, height = image.size
                bits = int(getattr(image, "bits", 8))
                # Treat tiled drawing backgrounds as processable raster images.
                # Many CAD/PDF exports split one scanned sheet into 1024x1024 JPEG tiles;
                # those tiles are smaller than min_long_side but still large enough by pixel count.
                # Skip only images that are small by both dimensions and area.
                if not options.include_small_images and (
                    max(width, height) < options.min_long_side
                    and width * height < options.min_pixels
                ):
                    result.skipped_small += 1
                    continue

                max_w, max_h = _a3_pixel_box(image, options.dpi)
                if image.mode == "1" and bits == 1 and width <= max_w and height <= max_h:
                    continue

                image_options = options
                if options.advanced_processing:
                    noisy_page = page_profiles.get(page_number, False)
                    image_options = replace(
                        options,
                        denoise=options.denoise or noisy_page,
                        threshold_offset=options.threshold_offset + (-10 if noisy_page else 0),
                    )
                work_items.append((index, image_file, image, image_options))
            except (
                OSError,
                ValueError,
                TypeError,
                MemoryError,
                LimitReachedError,
                PdfReadError,
            ) as exc:
                result.skipped_unsupported += 1
                message = (
                    f"画像 {index} ({getattr(image_file, 'name', '?')}): "
                    f"{type(exc).__name__} - {exc}"
                )
                result.warnings.append(message)
                conversion_errors.append(message)

        # PIL processing releases the GIL in its expensive C operations. Keep
        # pypdf writer mutation below serialized to avoid corrupting its object graph.
        worker_count = min(8, max(2, (os.cpu_count() or 4) // 2), len(work_items))
        with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count or 1) as pool:
            futures = [
                pool.submit(_prepare_image_task, image, image_options)
                for _, _, image, image_options in work_items
            ]
            for (index, image_file, _, _), future in zip(work_items, futures):
                if cancel_event is not None and cancel_event.is_set():
                    raise CancelledError("処理を中止しました。")
                if progress:
                    progress(f"画像 {index}/{len(images)} を処理中", index - 1, len(images))
                try:
                    binary, _ = future.result()
                    if not _replace_with_ccitt(image_file, binary):
                        image_file.replace(binary)
                    if image_file.image is None or image_file.image.mode != "1":
                        raise ValueError("2値画像への差し替え結果を検証できません")
                    if image_file.image.size != binary.size:
                        raise ValueError(
                            f"差し替え後の寸法が不一致です ({image_file.image.size} != {binary.size})"
                        )
                    result.converted_images += 1
                except (
                    OSError,
                    ValueError,
                    TypeError,
                    MemoryError,
                    LimitReachedError,
                    PdfReadError,
                ) as exc:
                    result.skipped_unsupported += 1
                    message = (
                        f"画像 {index} ({getattr(image_file, 'name', '?')}): "
                        f"{type(exc).__name__} - {exc}"
                    )
                    result.warnings.append(message)
                    conversion_errors.append(message)

        if progress:
            progress("PDFを保存中", len(images), len(images))
        if conversion_errors:
            raise RuntimeError(
                "画像変換に失敗したため、混在したPDFを保存せず中止しました。\n"
                + "\n".join(conversion_errors[:10])
                + (f"\n（ほか {len(conversion_errors) - 10}件）" if len(conversion_errors) > 10 else "")
            )
        # Compress identical objects to reduce file size. pypdf's clone_from +
        # replace leaves many duplicate objects (cloned dicts, repeated fonts,
        # identical CCITTFaxDecode parameters). compress_identical_objects()
        # deduplicates them without re-encoding content streams, so text and
        # vector graphics render identically to the pre-compression PDF.
        # A minimal /Info dict is required because compress_identical_objects()
        # accesses writer._info, which is None after _strip_pdf_metadata().
        if writer._info is None:
            writer._info = DictionaryObject({
                NameObject("/Title"): TextStringObject(""),
            })
        writer.compress_identical_objects()
        with temporary.open("wb") as stream:
            writer.write(stream)
        writer.close()

        os.replace(temporary, target)
        if progress:
            progress("保存完了", len(images), len(images))

        result.bytes_after = target.stat().st_size
        return result
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise
