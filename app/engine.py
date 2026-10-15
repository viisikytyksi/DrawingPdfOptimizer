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
try:  # private helper: decoding one image XObject ourselves
    from pypdf.generic._image_xobject import _xobj_to_image
except ImportError:  # pragma: no cover - fall back to pypdf's page.images accessor
    _xobj_to_image = None
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


@dataclass(frozen=True)
class ImageJob:
    """One image to convert. Decoding happens in the worker pool, not here."""

    page_number: int
    page: object
    key: object
    stream_object: object | None
    reference: tuple[int, int] | None


@dataclass(frozen=True)
class ImageOutcome:
    """What a worker decided for one image."""

    skip: str = ""  # "" | "small" | "already" | "inline"
    reference: tuple[int, int] | None = None
    stream: bytes | None = None  # encoded CCITT G4
    stream_object: object | None = None  # XObject to write the stream into
    binary: Image.Image | None = None  # prepared image when encoding is unavailable
    size: tuple[int, int] = (0, 0)


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

    if options.auto_contrast:
        gray = ImageOps.autocontrast(gray, cutoff=0.5)
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
    if options.denoise:
        binary = _despeckle(binary)
    return binary, threshold


def _despeckle(binary: Image.Image, min_neighbours: int = 2) -> Image.Image:
    """Drop black pixels with fewer than min_neighbours black neighbours (8-connected).

    Removes scan grain from the 1bit result instead of smoothing the grayscale,
    which keeps thin lines intact: a 1px line has two neighbours along its run, so
    it survives, while isolated dots and 2px specks do not. Runs at native
    resolution with shifted copies, so it costs a fraction of a 3x3 median filter.
    """
    width, height = binary.size
    white = Image.new("1", (width, height), 1)
    black = ImageChops.logical_xor(binary, white)  # 1 where the pixel is black
    counts: Image.Image | None = None
    if min_neighbours <= 1:
        neighbours: Image.Image | None = None
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                if dx == 0 and dy == 0:
                    continue
                shifted = Image.new("1", (width, height), 0)
                shifted.paste(black, (dx, dy))
                neighbours = (
                    shifted if neighbours is None else ImageChops.logical_or(neighbours, shifted)
                )
        keep = ImageChops.logical_and(black, neighbours)
    else:
        ones = black.convert("L").point(lambda value: 1 if value else 0)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                if dx == 0 and dy == 0:
                    continue
                shifted = Image.new("L", (width, height), 0)
                shifted.paste(ones, (dx, dy))
                counts = shifted if counts is None else ImageChops.add(counts, shifted)
        enough = counts.point(lambda value: 255 if value >= min_neighbours else 0).convert("1")
        keep = ImageChops.logical_and(black, enough)
    result = ImageChops.logical_xor(keep, white)
    if min_neighbours > 1:
        # Dropping a 2px speck leaves its partner without neighbours.
        result = _despeckle(result, 1)
    return result


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


def _write_ccitt_stream(stream_object: object, stream: bytes, size: tuple[int, int]) -> bool:
    """Write a prepared 1bit image into its existing XObject as CCITT G4.

    pypdf's ImageFile.replace() round-trips the image through a temporary PDF
    (Pillow write, parse, decode, re-encode). Writing the stream in place keeps
    the same bytes with a single encode. Returns False when the object is not an
    image stream, so the caller can fall back to replace().
    """
    if not isinstance(stream_object, StreamObject) or stream_object.get("/Subtype") != "/Image":
        return False
    width, height = size
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
    return True


def _image_key(image_file: object) -> tuple[int, int] | None:
    reference = getattr(image_file, "indirect_reference", None)
    if reference is None:
        return None
    return int(reference.idnum), int(reference.generation)


def _resolve_image_object(page: object, key: object) -> tuple[object, tuple[int, int]] | None:
    """Resolve a pypdf image key to (image stream object, (idnum, generation)).

    Returns None for inline images and for direct (non-indirect) image objects,
    which pypdf reports without an indirect reference and this tool skips, as well
    as for any key shape this walk does not understand — those keep pypdf's own
    path so nothing is dropped without its usual accounting.
    """
    names = list(key) if isinstance(key, (list, tuple)) else [key]
    if not names or not all(isinstance(name, str) for name in names):
        return None
    if names[0].startswith("~") and names[0].endswith("~"):
        return None  # inline image
    container: object = page
    for position, name in enumerate(names):
        if not hasattr(container, "get"):
            return None
        resources = container.get("/Resources")
        if resources is None:
            return None
        if hasattr(resources, "get_object"):
            resources = resources.get_object()
        xobjects = resources.get("/XObject") if hasattr(resources, "get") else None
        if xobjects is None:
            return None
        if hasattr(xobjects, "get_object"):
            xobjects = xobjects.get_object()
        if not hasattr(xobjects, "get") or name not in xobjects:
            return None
        # .get() keeps the IndirectObject; __getitem__ would resolve it away.
        entry = xobjects.get(name)
        if entry is None:
            return None
        target = entry.get_object() if hasattr(entry, "get_object") else entry
        if position + 1 == len(names):
            idnum = getattr(entry, "idnum", None)
            if idnum is None:
                return None  # direct object: pypdf reports no indirect reference
            return target, (int(idnum), int(getattr(entry, "generation", 0)))
        container = target
    return None


def _collect_images(writer: PdfWriter, result: OptimizeResult) -> list[ImageJob]:
    """List the images to convert without decoding them.

    Decoding is the expensive part of a large drawing, so it happens later in the
    worker pool; this walk only reads the object graph.
    """
    jobs: list[ImageJob] = []
    seen: set[tuple[int, int]] = set()
    for page_number, page in enumerate(writer.pages, start=1):
        try:
            keys = list(page.images.keys())
        except Exception as exc:
            raise RuntimeError(
                f"{page_number}ページの画像一覧を取得できないため、処理を中止しました: {exc}"
            ) from exc
        for key in keys:
            resolved = _resolve_image_object(page, key) if _xobj_to_image is not None else None
            if resolved is None:
                jobs.append(ImageJob(page_number, page, key, None, None))
                continue
            stream_object, reference = resolved
            if reference in seen:
                continue
            seen.add(reference)
            jobs.append(ImageJob(page_number, page, key, stream_object, reference))
    return jobs




def _decode_job_image(
    job: ImageJob,
) -> tuple[Image.Image | None, object | None, tuple[int, int] | None]:
    """Decode one image and locate the XObject it belongs to; runs in a worker.

    Images resolved during collection are decoded straight from their stream object.
    Anything else goes through pypdf's own image accessor, which also covers inline
    images and keys this walk does not understand.
    """
    if job.stream_object is not None:
        return _xobj_to_image(job.stream_object)[2], job.stream_object, job.reference
    image_file = job.page._get_image(job.key)
    reference = _image_key(image_file)
    target = None
    if image_file.indirect_reference is not None:
        target = image_file.indirect_reference.get_object()
    return image_file.image, target, reference


def _process_image_job(job: ImageJob, options: OptimizeOptions) -> ImageOutcome:
    """Decode, prepare and encode one image; runs inside a worker thread."""
    image, target, reference = _decode_job_image(job)
    if image is None:
        raise ValueError("画像を復号できません")
    if reference is None:
        return ImageOutcome(skip="inline")
    width, height = image.size
    bits = int(getattr(image, "bits", 8))
    # Treat tiled drawing backgrounds as processable raster images.
    # Many CAD/PDF exports split one scanned sheet into 1024x1024 JPEG tiles;
    # those tiles are smaller than min_long_side but still large enough by pixel count.
    # Skip only images that are small by both dimensions and area.
    if not options.include_small_images and (
        max(width, height) < options.min_long_side and width * height < options.min_pixels
    ):
        return ImageOutcome(skip="small", reference=reference)
    max_width, max_height = _a3_pixel_box(image, options.dpi)
    if image.mode == "1" and bits == 1 and width <= max_width and height <= max_height:
        return ImageOutcome(skip="already", reference=reference)
    binary, _ = _prepare_image(image, options)
    stream = _encode_ccitt_g4(binary)
    if stream is None:
        # No CCITT encoder available: the caller falls back to replace(), which
        # asks pypdf for the image again rather than keeping decoded pixels here.
        return ImageOutcome(reference=reference, binary=binary, size=binary.size)
    return ImageOutcome(
        reference=reference, stream=stream, stream_object=target, size=binary.size
    )


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

        jobs = _collect_images(writer, result)
        result.total_images = len(jobs)
        conversion_errors: list[str] = []
        # Decoding and encoding are Pillow C work that releases the GIL, so the
        # whole convert step runs in the pool; pypdf writer mutation stays here.
        worker_count = min(8, max(2, (os.cpu_count() or 4) // 2), len(jobs)) or 1

        written: set[tuple[int, int]] = set()
        with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as pool:
            futures = [pool.submit(_process_image_job, job, options) for job in jobs]
            for index, (job, future) in enumerate(zip(jobs, futures), start=1):
                if cancel_event is not None and cancel_event.is_set():
                    raise CancelledError("処理を中止しました。")
                if progress:
                    progress(f"画像 {index}/{len(jobs)} を処理中", index - 1, len(jobs))
                try:
                    outcome = future.result()
                except (
                    OSError,
                    ValueError,
                    TypeError,
                    MemoryError,
                    LimitReachedError,
                    PdfReadError,
                ) as exc:
                    result.skipped_unsupported += 1
                    message = f"画像 {index} ({job.key}): {type(exc).__name__} - {exc}"
                    result.warnings.append(message)
                    conversion_errors.append(message)
                    continue
                if outcome.skip == "inline":
                    result.skipped_inline += 1
                    continue
                if outcome.skip == "small":
                    result.skipped_small += 1
                    continue
                if outcome.skip == "already":
                    continue
                if outcome.reference is not None:
                    if outcome.reference in written:
                        continue
                    written.add(outcome.reference)
                try:
                    if outcome.stream is not None:
                        if outcome.stream_object is None or not _write_ccitt_stream(
                            outcome.stream_object, outcome.stream, outcome.size
                        ):
                            raise ValueError("画像XObjectへ書き込めません")
                    else:
                        job.page._get_image(job.key).replace(outcome.binary)
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
                    message = f"画像 {index} ({job.key}): {type(exc).__name__} - {exc}"
                    result.warnings.append(message)
                    conversion_errors.append(message)

        if progress:
            progress("PDFを保存中", len(jobs), len(jobs))
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
            progress("保存完了", len(jobs), len(jobs))

        result.bytes_after = target.stat().st_size
        return result
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise
