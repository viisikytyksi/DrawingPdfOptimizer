from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from PIL import Image, ImageEnhance, ImageFilter, ImageOps
from pypdf import PdfWriter
from pypdf.errors import LimitReachedError, PdfReadError
from pypdf.generic import NameObject


ProgressCallback = Callable[[str, int, int], None]


@dataclass(frozen=True)
class OptimizeOptions:
    dpi: int = 300
    auto_contrast: bool = True
    contrast: float = 1.15
    sharpen: bool = True
    threshold_offset: int = 0
    include_small_images: bool = False
    strip_metadata: bool = True
    min_long_side: int = 1200
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

    gray = image.convert("L")
    max_size = _a3_pixel_box(gray, options.dpi)
    if gray.width > max_size[0] or gray.height > max_size[1]:
        gray.thumbnail(max_size, Image.Resampling.LANCZOS)

    if options.auto_contrast:
        gray = ImageOps.autocontrast(gray, cutoff=0.5)
    if options.contrast != 1.0:
        gray = ImageEnhance.Contrast(gray).enhance(options.contrast)
    if options.sharpen:
        gray = gray.filter(ImageFilter.UnsharpMask(radius=1.0, percent=130, threshold=3))

    threshold = max(0, min(255, _otsu_threshold(gray) + options.threshold_offset))
    table = [0 if value <= threshold else 255 for value in range(256)]
    return gray.point(table, mode="1"), threshold


def _image_key(image_file: object) -> tuple[int, int] | None:
    reference = getattr(image_file, "indirect_reference", None)
    if reference is None:
        return None
    return int(reference.idnum), int(reference.generation)


def _collect_images(writer: PdfWriter, result: OptimizeResult) -> list[object]:
    images: list[object] = []
    seen: set[tuple[int, int]] = set()
    for page_number, page in enumerate(writer.pages, start=1):
        try:
            keys = list(page.images.keys())
        except Exception as exc:
            result.warnings.append(f"{page_number}ページ: 画像一覧を取得できません ({exc})")
            continue
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
                images.append(image_file)
            except Exception as exc:
                result.skipped_unsupported += 1
                result.warnings.append(
                    f"{page_number}ページの画像 {key}: 読み込みをスキップ ({exc})"
                )
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
        writer = PdfWriter(clone_from=source)
        if _is_signature_present(writer):
            result.warnings.append("電子署名は保存時に無効になります。")

        if options.strip_metadata:
            result.metadata_stripped = _strip_pdf_metadata(writer)

        images = _collect_images(writer, result)
        result.total_images = len(images) + result.skipped_inline
        conversion_errors: list[str] = []

        for index, image_file in enumerate(images, start=1):
            if cancel_event is not None and cancel_event.is_set():
                raise CancelledError("処理を中止しました。")
            if progress:
                progress(f"画像 {index}/{len(images)} を処理中", index - 1, len(images))

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

                binary, _ = _prepare_image(image, options)
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
        # ponytail: skip global object deduplication; it re-decompresses huge streams,
        # and the drawing images are already replaced above. Re-enable only if output
        # size becomes a measured problem and large-stream handling is added.
        with temporary.open("wb") as stream:
            writer.write(stream)
        writer.close()

        os.replace(temporary, target)
        result.bytes_after = target.stat().st_size
        return result
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise
