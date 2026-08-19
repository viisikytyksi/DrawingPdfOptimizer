from __future__ import annotations

import argparse
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
    output_format: str = "png"
    threshold: int = 50


def _find_magick(app_root: Path) -> tuple[Path, Path | None]:
    runtime_root = app_root / "runtime"
    image_dir = runtime_root / "ImageMagick"
    candidates = [image_dir / "magick.exe", image_dir / "convert.exe"]
    magick = next((path for path in candidates if path.is_file()), None)
    if magick is None:
        # Do not use PATH's `convert.exe`: Windows ships a different disk utility
        # with that name. Legacy ImageMagick convert.exe is supported only when
        # explicitly placed under runtime/ImageMagick.
        command = shutil.which("magick")
        magick = Path(command) if command else None
    if magick is None:
        raise FileNotFoundError(
            "ImageMagickが見つかりません。runtime\\ImageMagick\\magick.exeへ配置してください。"
        )

    ghostscript_dir = runtime_root / "Ghostscript" / "bin"
    if not ghostscript_dir.is_dir():
        ghostscript_dir = None
    return magick, ghostscript_dir


def _output_folder(source: Path, parent: Path | None) -> Path:
    root = parent or source.parent
    candidate = root / f"{source.stem}_画像化"
    number = 2
    while candidate.exists():
        candidate = root / f"{source.stem}_画像化_{number}"
        number += 1
    candidate.mkdir(parents=True)
    return candidate


def rasterize_pdf(
    input_path: str | os.PathLike[str],
    output_parent: str | os.PathLike[str] | None,
    options: RasterizeOptions,
    app_root: Path | None = None,
) -> dict[str, object]:
    source = Path(input_path)
    if source.suffix.lower() != ".pdf":
        raise ValueError(f"PDFではありません: {source}")
    if options.output_format not in {"png", "tif"}:
        raise ValueError("出力形式はpngまたはtifです")

    page_count = len(PdfReader(str(source), strict=False).pages)
    root = app_root or Path(__file__).resolve().parents[1]
    magick, ghostscript_dir = _find_magick(root)
    target = _output_folder(source, Path(output_parent) if output_parent else None)
    extension = options.output_format
    pattern = target / f"page-%03d.{extension}"
    arguments = [
        str(magick),
        "-quiet",
        "-density",
        str(options.dpi),
        "-scene",
        "1",
        str(source),
        "-background",
        "white",
        "-alpha",
        "remove",
        "-alpha",
        "off",
        "-colorspace",
        "Gray",
        "-threshold",
        f"{options.threshold}%",
        "-type",
        "Bilevel",
    ]
    if extension == "tif":
        arguments.extend(["-compress", "Group4"])
    arguments.extend(["-strip", str(pattern)])

    environment = os.environ.copy()
    environment["PATH"] = os.pathsep.join(
        str(path) for path in (magick.parent, ghostscript_dir, environment.get("PATH", "")) if path
    )
    environment["MAGICK_HOME"] = str(magick.parent)
    if ghostscript_dir is not None:
        resource_dir = ghostscript_dir.parent / "Resource"
        if resource_dir.is_dir():
            environment["GS_LIB"] = str(resource_dir)
    try:
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
            raise RuntimeError(completed.stderr.strip() or "ImageMagickで変換できませんでした")

        files = sorted(target.glob(f"page-*.{extension}"))
        if len(files) != page_count:
            raise RuntimeError(f"出力ページ数が不一致です ({len(files)} / {page_count})")
        for path in files:
            with Image.open(path) as image:
                if image.mode != "1":
                    raise RuntimeError(f"1bit画像ではありません: {path.name} ({image.mode})")
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        raise

    return {
        "input": str(source),
        "output_dir": str(target),
        "pages": page_count,
        "format": extension,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--dpi", type=int, choices=(200, 300, 400), default=300)
    parser.add_argument("--format", choices=("png", "tif"), default="png")
    parser.add_argument("--threshold", type=int, choices=range(1, 100), default=50)
    args = parser.parse_args()
    options = RasterizeOptions(args.dpi, args.format, args.threshold)
    root = Path(__file__).resolve().parents[1]
    results = [rasterize_pdf(path, args.output_dir, options, root) for path in args.inputs]
    print(json.dumps({"results": results}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
