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


def _fallback_log_path(
    log_fallback_dir: Path | None,
    output_dir: Path | None,
    source: Path,
    current: Path,
) -> Path | None:
    """Alternative log location used when the primary one cannot be written.

    Priority: the app folder (--log-fallback-dir, so a NAS that dropped mid-run
    still leaves a record) > output_dir > source.parent.
    """
    for candidate in (log_fallback_dir, output_dir, source.parent):
        if candidate is None:
            continue
        path = candidate / _LOG_NAME
        if path != current:
            return path
    return None


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
        "--log-fallback-dir", type=Path, default=None,
        help="Directory used for the log when --log-dir cannot be written (app folder)",
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
    failures: list[dict[str, str]] = []
    log_path = _log_path(args.log_dir, args.output_dir, args.inputs[0]) if args.inputs else None
    log_error_reported = False
    log_moved_reported = False
    user = getpass.getuser()
    fullname = args.fullname
    hostname = socket.gethostname()

    def report_log_problem(message: str, event: str, **fields) -> None:
        if pstream:
            _emit_progress(pstream, event, **fields)
        else:
            print(f"警告: {message}", file=sys.stderr)

    def write_log_row(source: Path, row: list[str]) -> None:
        """Append one row, moving to the fallback location if the primary fails."""
        nonlocal log_path, log_error_reported, log_moved_reported
        if log_path is None:
            return
        log_error = _append_log(log_path, row)
        if not log_error:
            return
        fallback = _fallback_log_path(args.log_fallback_dir, args.output_dir, source, log_path)
        if fallback is not None and not _append_log(fallback, row):
            moved_from = log_path
            log_path = fallback
            if not log_moved_reported:
                log_moved_reported = True
                report_log_problem(
                    f"ログの保存先を変更しました: {fallback} ({log_error})",
                    "log_moved",
                    path=str(fallback),
                    previous=str(moved_from),
                    error=log_error,
                )
            return
        if not log_error_reported:
            log_error_reported = True
            report_log_problem(
                f"処理ログを保存できません: {log_path} ({log_error})",
                "log_error",
                path=str(log_path),
                error=log_error,
            )

    def log_row(file_label: str, status: str, error: str, **counts: int) -> list[str]:
        return [
            datetime.now().isoformat(timespec="seconds"),
            __version__, user, fullname, hostname, file_label,
            counts.get("filesize_bytes", 0),
            counts.get("total_images", 0),
            counts.get("converted", 0),
            counts.get("skipped_small", 0),
            counts.get("skipped_unsupported", 0),
            counts.get("bytes_after", 0),
            round(counts.get("elapsed_sec", 0.0), 1),
            args.dpi, status, error,
        ]

    started = time.time()
    if args.inputs:
        write_log_row(
            args.inputs[0],
            log_row(f"(実行開始) 入力{len(args.inputs)}件", "run-start", ""),
        )

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
        else:
            # One broken file must not stop the rest of the batch: the failing
            # file simply has no output, and the run reports it at the end.
            failures.append({"input": str(source), "error": error_msg})

        try:
            filesize = source.stat().st_size
        except OSError:
            filesize = 0
        write_log_row(
            source,
            log_row(
                str(source), status, error_msg,
                filesize_bytes=filesize,
                total_images=result.total_images if result else 0,
                converted=result.converted_images if result else 0,
                skipped_small=result.skipped_small if result else 0,
                skipped_unsupported=result.skipped_unsupported if result else 0,
                bytes_after=result.bytes_after if result else 0,
                elapsed_sec=elapsed,
            ),
        )
        if pstream:
            if result is None:
                _emit_progress(pstream, "file_error", file=file_index + 1, name=source.name, error=error_msg)
            else:
                _emit_progress(pstream, "file_done", file=file_index + 1, total_files=len(args.inputs), name=source.name)

    if args.inputs:
        totals = {
            "total_images": sum(r["total_images"] for r in results),
            "converted": sum(r["converted_images"] for r in results),
            "skipped_small": sum(r["skipped_small"] for r in results),
            "skipped_unsupported": sum(r["skipped_unsupported"] for r in results),
            "bytes_after": sum(r["bytes_after"] for r in results),
        }
        summary = ""
        if failures:
            summary = f"成功{len(results)}件/失敗{len(failures)}件: " + "; ".join(
                f"{Path(f['input']).name}: {f['error'].splitlines()[0]}" for f in failures[:5]
            )
        write_log_row(
            args.inputs[0],
            log_row(
                f"(実行完了) 入力{len(args.inputs)}件 成功{len(results)}件 失敗{len(failures)}件",
                "run-done", summary,
                elapsed_sec=time.time() - started, **totals,
            ),
        )
    if pstream:
        _emit_progress(pstream, "all_done", total_files=len(args.inputs), failed=len(failures))
    print(json.dumps({"results": results, "failures": failures}, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
