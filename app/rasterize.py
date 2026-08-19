from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from PIL import Image
from pypdf import PdfReader


@dataclass(frozen=True)
class RasterizeOptions:
    dpi: int = 300
    workers: int = 2
    format: str = "png"
    slides_mode: bool = True


def _find_ghostscript(app_root: Path) -> tuple[Path, Path | None]:
    runtime_root = app_root / "runtime"
    ghostscript_root = runtime_root / "Ghostscript"
    candidates = [
        ghostscript_root / "bin" / "gswin64c.exe",
        ghostscript_root / "bin" / "gswin32c.exe",
    ]
    ghostscript = next((path for path in candidates if path.is_file()), None)
    if ghostscript is None:
        command = shutil.which("gswin64c") or shutil.which("gswin32c")
        ghostscript = Path(command) if command else None
    if ghostscript is None:
        raise FileNotFoundError(
            "Ghostscriptが見つかりません。"
            "runtime\\Ghostscript\\bin\\gswin64c.exeへ配置してください。"
        )
    resource_dir = ghostscript.parent.parent / "Resource"
    return ghostscript, resource_dir if resource_dir.is_dir() else None


def _output_folder(source: Path, parent: Path | None) -> Path:
    root = parent or source.parent
    candidate = root / f"{source.stem}_画像化"
    number = 2
    while candidate.exists():
        candidate = root / f"{source.stem}_画像化_{number}"
        number += 1
    candidate.mkdir(parents=True)
    return candidate


def _render_chunk(
    ghostscript: Path,
    source: Path,
    output_dir: Path,
    chunk_number: int,
    first_page: int,
    last_page: int,
    dpi: int,
    output_format: str,
    slides_mode: bool,
    environment: dict[str, str],
) -> list[tuple[int, Path]]:
    if slides_mode:
        device, extension = "pnggray", "png"
    else:
        device, extension = {
            "png": ("pngmono", "png"),
            "tif": ("tiffg4", "tif"),
        }[output_format]
    pattern = output_dir / f"chunk-{chunk_number}-page-%03d.{extension}"
    arguments = [
        str(ghostscript),
        "-q",
        "-dSAFER",
        "-dBATCH",
        "-dNOPAUSE",
        f"-sDEVICE={device}",
        f"-r{dpi}",
        f"-dFirstPage={first_page}",
        f"-dLastPage={last_page}",
        f"-sOutputFile={pattern}",
        str(source),
    ]
    completed = subprocess.run(
        arguments,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "Ghostscriptで変換できませんでした")

    files = sorted(output_dir.glob(f"chunk-{chunk_number}-page-*.{extension}"))
    expected = last_page - first_page + 1
    if len(files) != expected:
        raise RuntimeError(f"出力ページ数が不一致です ({len(files)} / {expected})")
    return [(first_page + index, path) for index, path in enumerate(files)]


def rasterize_pdf(
    input_path: str | os.PathLike[str],
    output_parent: str | os.PathLike[str] | None,
    options: RasterizeOptions,
    app_root: Path | None = None,
) -> dict[str, object]:
    source = Path(input_path)
    if source.suffix.lower() != ".pdf":
        raise ValueError(f"PDFではありません: {source}")
    if options.dpi not in {200, 300, 400}:
        raise ValueError("解像度は200、300、400のいずれかです")
    if options.format not in {"png", "tif"}:
        raise ValueError("形式はpngまたはtifです")
    workers = max(1, min(4, int(options.workers)))

    page_count = len(PdfReader(str(source), strict=False).pages)
    root = app_root or Path(__file__).resolve().parents[1]
    ghostscript, resource_dir = _find_ghostscript(root)
    target = _output_folder(source, Path(output_parent) if output_parent else None)
    environment = os.environ.copy()
    environment["PATH"] = os.pathsep.join(
        str(path) for path in (ghostscript.parent, environment.get("PATH", "")) if path
    )
    if resource_dir is not None:
        environment["GS_LIB"] = str(resource_dir)
    output_format = "png" if options.slides_mode else options.format

    try:
        # Use unique files in the output folder; Windows may reject worker dirs
        # created below network/output folders with restrictive ACLs.
        worker_count = min(workers, page_count)
        chunk_count = min(worker_count if worker_count == 1 else worker_count * 2, page_count)
        chunk_size = (page_count + chunk_count - 1) // chunk_count
        chunks = [
            (first, min(first + chunk_size - 1, page_count))
            for first in range(1, page_count + 1, chunk_size)
        ]
        rendered: list[tuple[int, Path]] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as pool:
            futures = [
                pool.submit(
                    _render_chunk,
                    ghostscript,
                    source,
                    target,
                    index,
                    first,
                    last,
                    options.dpi,
                    output_format,
                    options.slides_mode,
                    environment,
                )
                for index, (first, last) in enumerate(chunks, start=1)
            ]
            for future in futures:
                rendered.extend(future.result())

        for page_number, path in sorted(rendered):
            destination = target / f"page-{page_number:03d}.{output_format}"
            shutil.move(str(path), destination)
            if options.slides_mode:
                with Image.open(destination) as image:
                    height = max(1, round(image.height * 2560 / image.width))
                    resized = image.convert("L").resize(
                        (2560, height), Image.Resampling.LANCZOS
                    )
                    temporary = destination.with_name(destination.stem + ".resize.png")
                    resized.save(temporary, format="PNG", optimize=True)
                os.replace(temporary, destination)
            with Image.open(destination) as image:
                expected_mode = "L" if options.slides_mode else "1"
                if image.mode != expected_mode:
                    raise RuntimeError(f"想定モードではありません: {destination.name} ({image.mode})")
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        raise

    return {
        "input": str(source),
        "output_dir": str(target),
        "pages": page_count,
        "format": output_format,
        "slides_mode": options.slides_mode,
        "workers": worker_count,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--dpi", type=int, choices=(200, 300, 400), default=300)
    parser.add_argument("--workers", type=int, choices=(1, 2, 4), default=2)
    parser.add_argument("--format", choices=("png", "tif"), default="png")
    parser.add_argument("--no-slides", action="store_true", help="2560px LモードPNG化を無効にする")
    args = parser.parse_args()
    options = RasterizeOptions(args.dpi, args.workers, args.format, not args.no_slides)
    root = Path(__file__).resolve().parents[1]
    results = [rasterize_pdf(path, args.output_dir, options, root) for path in args.inputs]
    # Keep the pipe ASCII-only; Windows PowerShell 5.1 may decode redirected
    # stdout with a different code page even when the GUI requests UTF-8.
    print(json.dumps({"results": results}, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
