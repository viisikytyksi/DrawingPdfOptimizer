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
    environment: dict[str, str],
) -> list[tuple[int, Path]]:
    pattern = output_dir / f"chunk-{chunk_number}-page-%03d.tif"
    arguments = [
        str(ghostscript),
        "-q",
        "-dSAFER",
        "-dBATCH",
        "-dNOPAUSE",
        "-sDEVICE=tiffg4",
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

    files = sorted(output_dir.glob(f"chunk-{chunk_number}-page-*.tif"))
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

    try:
        # Use unique files in the output folder; Windows may reject worker dirs
        # created below network/output folders with restrictive ACLs.
        chunk_count = min(workers, page_count)
        chunk_size = (page_count + chunk_count - 1) // chunk_count
        chunks = [
            (first, min(first + chunk_size - 1, page_count))
            for first in range(1, page_count + 1, chunk_size)
        ]
        rendered: list[tuple[int, Path]] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=chunk_count) as pool:
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
                    environment,
                )
                for index, (first, last) in enumerate(chunks, start=1)
            ]
            for future in futures:
                rendered.extend(future.result())

        for page_number, path in sorted(rendered):
            destination = target / f"page-{page_number:03d}.tif"
            shutil.move(str(path), destination)
            with Image.open(destination) as image:
                if image.mode != "1":
                    raise RuntimeError(f"1bit画像ではありません: {destination.name} ({image.mode})")
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        raise

    return {
        "input": str(source),
        "output_dir": str(target),
        "pages": page_count,
        "format": "tif",
        "workers": chunk_count,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--dpi", type=int, choices=(200, 300, 400), default=300)
    parser.add_argument("--workers", type=int, choices=(1, 2, 4), default=2)
    args = parser.parse_args()
    options = RasterizeOptions(args.dpi, args.workers)
    root = Path(__file__).resolve().parents[1]
    results = [rasterize_pdf(path, args.output_dir, options, root) for path in args.inputs]
    print(json.dumps({"results": results}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
