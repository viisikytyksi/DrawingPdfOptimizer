from __future__ import annotations

import argparse
import json
from pathlib import Path

from engine import OptimizeOptions, optimize_pdf


def output_path(source: Path, folder: Path | None) -> Path:
    destination = folder or source.parent
    candidate = destination / f"{source.stem}_2値化.pdf"
    number = 2
    while candidate.exists():
        candidate = destination / f"{source.stem}_2値化_{number}.pdf"
        number += 1
    return candidate


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--dpi", type=int, choices=(200, 300, 400), default=300)
    parser.add_argument("--contrast", type=float, default=1.15)
    parser.add_argument("--threshold", type=int, default=0)
    parser.add_argument("--no-auto-contrast", action="store_true")
    parser.add_argument("--no-sharpen", action="store_true")
    parser.add_argument("--include-small", action="store_true")
    parser.add_argument("--keep-metadata", action="store_true")
    args = parser.parse_args()

    options = OptimizeOptions(
        dpi=args.dpi,
        auto_contrast=not args.no_auto_contrast,
        contrast=max(1.0, min(1.5, args.contrast)),
        sharpen=not args.no_sharpen,
        threshold_offset=max(-30, min(30, args.threshold)),
        include_small_images=args.include_small,
        strip_metadata=not args.keep_metadata,
    )
    results = []
    for source in args.inputs:
        target = output_path(source, args.output_dir)
        result = optimize_pdf(source, target, options)
        results.append(
            {
                "input": str(source),
                "output": str(target),
                "total_images": result.total_images,
                "converted_images": result.converted_images,
                "skipped_small": result.skipped_small,
                "skipped_inline": result.skipped_inline,
                "skipped_unsupported": result.skipped_unsupported,
                "bytes_before": result.bytes_before,
                "bytes_after": result.bytes_after,
                "metadata_stripped": result.metadata_stripped,
                "warnings": result.warnings,
            }
        )
    print(json.dumps({"results": results}, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
