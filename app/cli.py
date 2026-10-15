from __future__ import annotations

import argparse
import csv
import getpass
import json
import os
import socket
import sys
import time
from datetime import datetime
from pathlib import Path

from engine import OptimizeOptions, optimize_pdf
from version import __version__

_LOG_NAME = "DrawingPdfOptimizer.log"
_LOG_HEADER = [
    "timestamp", "version", "user", "fullname", "hostname", "file", "filesize_bytes",
    "total_images", "converted", "skipped_small", "skipped_unsupported",
    "bytes_after", "elapsed_sec", "dpi", "status", "error",
]


def output_path(source: Path, folder: Path | None) -> Path:
    destination = folder or source.parent
    candidate = destination / f"{source.stem}_2値化.pdf"
    number = 2
    while candidate.exists():
        candidate = destination / f"{source.stem}_2値化_{number}.pdf"
        number += 1
    return candidate


def _emit_progress(stream, event_type: str, **kwargs) -> None:
    """Write a single JSON progress line to the given stream."""
    payload = {"type": event_type, **kwargs}
    stream.write(json.dumps(payload, ensure_ascii=True) + "\n")
    stream.flush()


def _log_path(log_dir: Path | None, output_dir: Path | None, source: Path) -> Path:
    """Determine where to write the log file.

    Priority: log_dir (NAS) > output_dir > source.parent.
    """
    dest = log_dir or output_dir or source.parent
    return dest / _LOG_NAME


def _append_log(log_path: Path, row: list[str]) -> str:
    """Append a single CSV row to the log file.

    Never raises and never fails the run. Returns "" on success, or the error
    message when the row could not be written (NAS unreachable, read-only
    share, missing folder), so the caller can surface the gap.
    """
    try:
        write_header = not log_path.exists() or log_path.stat().st_size == 0
        with log_path.open("a", newline="", encoding="shift_jis", errors="replace") as f:
            writer = csv.writer(f)
            if write_header:
                writer.writerow(_LOG_HEADER)
            writer.writerow(row)
    except OSError as exc:
        return f"{type(exc).__name__}: {exc}"
    return ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--dpi", type=int, choices=(200, 300, 400), default=300)
    parser.add_argument("--contrast", type=float, default=1.15)
    parser.add_argument("--threshold", type=int, default=0)
    parser.add_argument("--no-auto-contrast", action="store_true")
    parser.add_argument("--no-sharpen", action="store_true")
    parser.add_argument("--denoise", action="store_true")
    parser.add_argument("--include-small", action="store_true")
    parser.add_argument("--keep-metadata", action="store_true")
    parser.add_argument(
        "--progress", action="store_true",
        help="Emit JSON progress lines to stderr (for GUI front-ends)",
    )
    parser.add_argument(
        "--log-dir", type=Path, default=None,
        help="Directory for DrawingPdfOptimizer.log (e.g. NAS share)",
    )
    parser.add_argument(
        "--fullname", type=str, default="",
        help="User full name (氏名) for the log",
    )
    args = parser.parse_args()

    options = OptimizeOptions(
        dpi=args.dpi,
        auto_contrast=not args.no_auto_contrast,
        contrast=max(1.0, min(1.5, args.contrast)),
        sharpen=not args.no_sharpen,
        denoise=args.denoise,
        threshold_offset=max(-30, min(30, args.threshold)),
        include_small_images=args.include_small,
        strip_metadata=not args.keep_metadata,
    )
    pstream = sys.stderr if args.progress else None
    results = []
    log_path = _log_path(args.log_dir, args.output_dir, args.inputs[0]) if args.inputs else None
    log_error_reported = False
    user = getpass.getuser()
    fullname = args.fullname
    hostname = socket.gethostname()

    for file_index, source in enumerate(args.inputs):
        target = output_path(source, args.output_dir)
        if pstream:
            _emit_progress(pstream, "file_start", file=file_index + 1, total_files=len(args.inputs), name=source.name)

        def progress_cb(message: str, current: int, total: int, _pstream=pstream, _fi=file_index, _tf=len(args.inputs), _name=source.name) -> None:
            if _pstream:
                frac = (current / total) if total else 0
                _emit_progress(_pstream, "progress", file=_fi + 1, total_files=_tf, image=current, total_images=total, fraction=round(frac, 4), name=_name, message=message)

        t0 = time.time()
        status = "ok"
        error_msg = ""
        try:
            result = optimize_pdf(source, target, options, progress=progress_cb if pstream else None)
        except Exception as exc:
            result = None
            status = "error"
            error_msg = f"{type(exc).__name__}: {exc}"
        elapsed = time.time() - t0

        if result:
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
        if log_path:
            log_error = _append_log(log_path, [
                datetime.now().isoformat(timespec="seconds"),
                __version__, user, fullname, hostname, str(source),
                source.stat().st_size,
                result.total_images if result else 0,
                result.converted_images if result else 0,
                result.skipped_small if result else 0,
                result.skipped_unsupported if result else 0,
                result.bytes_after if result else 0,
                round(elapsed, 1),
                args.dpi, status, error_msg,
            ])
            if log_error and not log_error_reported:
                log_error_reported = True
                if pstream:
                    _emit_progress(pstream, "log_error", path=str(log_path), error=log_error)
                else:
                    print(f"警告: 処理ログを保存できません: {log_path} ({log_error})", file=sys.stderr)
        if pstream:
            _emit_progress(pstream, "file_done", file=file_index + 1, total_files=len(args.inputs), name=source.name)
        if result is None:
            if pstream:
                _emit_progress(pstream, "file_error", file=file_index + 1, name=source.name, error=error_msg)
            raise RuntimeError(error_msg)
    if pstream:
        _emit_progress(pstream, "all_done", total_files=len(args.inputs))
    print(json.dumps({"results": results}, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
