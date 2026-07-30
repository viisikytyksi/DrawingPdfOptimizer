from __future__ import annotations

import queue
import sys
import threading
import traceback
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from engine import CancelledError, OptimizeOptions, OptimizeResult, optimize_pdf


APP_NAME = "図面PDF 画像2値化"


class OptimizerApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_NAME)
        self.geometry("820x640")
        self.minsize(720, 560)
        self._events: queue.Queue[tuple[str, object]] = queue.Queue()
        self._cancel = threading.Event()
        self._worker: threading.Thread | None = None

        self.dpi_var = tk.StringVar(value="300")
        self.output_var = tk.StringVar(value="")
        self.auto_contrast_var = tk.BooleanVar(value=True)
        self.contrast_var = tk.DoubleVar(value=1.15)
        self.sharpen_var = tk.BooleanVar(value=True)
        self.threshold_var = tk.IntVar(value=0)
        self.include_small_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="PDFを追加してください。")

        self._build_ui()
        self.after(100, self._poll_events)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=12)
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)

        toolbar = ttk.Frame(root)
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Button(toolbar, text="PDFを追加...", command=self._add_files).pack(side="left")
        ttk.Button(toolbar, text="選択を削除", command=self._remove_selected).pack(side="left", padx=6)
        ttk.Button(toolbar, text="すべて消去", command=self._clear_files).pack(side="left")

        list_frame = ttk.Frame(root)
        list_frame.grid(row=1, column=0, sticky="nsew")
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)
        self.file_list = tk.Listbox(list_frame, selectmode="extended")
        self.file_list.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.file_list.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.file_list.configure(yscrollcommand=scrollbar.set)

        output = ttk.LabelFrame(root, text="保存先", padding=10)
        output.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        output.columnconfigure(0, weight=1)
        ttk.Entry(output, textvariable=self.output_var).grid(row=0, column=0, sticky="ew")
        ttk.Button(output, text="参照...", command=self._choose_output).grid(row=0, column=1, padx=(8, 0))
        ttk.Label(output, text="空欄なら元PDFと同じフォルダーへ別名保存").grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(4, 0)
        )

        settings = ttk.LabelFrame(root, text="画像処理", padding=10)
        settings.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        for column in range(4):
            settings.columnconfigure(column, weight=1 if column in (1, 3) else 0)

        ttk.Label(settings, text="A3印刷解像度").grid(row=0, column=0, sticky="w")
        ttk.Combobox(
            settings, textvariable=self.dpi_var, values=("200", "300", "400"),
            state="readonly", width=8
        ).grid(row=0, column=1, sticky="w", padx=(8, 20))
        ttk.Label(settings, text="dpi（300推奨）").grid(row=0, column=1, sticky="w", padx=(82, 0))

        ttk.Checkbutton(settings, text="自動コントラスト", variable=self.auto_contrast_var).grid(
            row=0, column=2, sticky="w"
        )
        ttk.Checkbutton(settings, text="線を明瞭化", variable=self.sharpen_var).grid(
            row=0, column=3, sticky="w"
        )

        ttk.Label(settings, text="コントラスト").grid(row=1, column=0, sticky="w", pady=(10, 0))
        ttk.Scale(
            settings, from_=1.0, to=1.5, variable=self.contrast_var, orient="horizontal"
        ).grid(row=1, column=1, sticky="ew", padx=(8, 20), pady=(10, 0))

        ttk.Label(settings, text="黒を増減").grid(row=1, column=2, sticky="w", pady=(10, 0))
        threshold = ttk.Scale(
            settings, from_=-30, to=30, variable=self.threshold_var, orient="horizontal"
        )
        threshold.grid(row=1, column=3, sticky="ew", padx=(8, 0), pady=(10, 0))

        ttk.Checkbutton(
            settings,
            text="小さい画像（ロゴ・印影など）も2値化する",
            variable=self.include_small_var,
        ).grid(row=2, column=0, columnspan=4, sticky="w", pady=(10, 0))

        run_area = ttk.Frame(root)
        run_area.grid(row=4, column=0, sticky="ew", pady=(12, 0))
        run_area.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(run_area, mode="determinate")
        self.progress.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        self.start_button = ttk.Button(run_area, text="最適化を開始", command=self._start)
        self.start_button.grid(row=0, column=1)
        self.cancel_button = ttk.Button(run_area, text="中止", command=self._request_cancel, state="disabled")
        self.cancel_button.grid(row=0, column=2, padx=(6, 0))
        ttk.Label(root, textvariable=self.status_var).grid(row=5, column=0, sticky="w", pady=(8, 0))

    def _add_files(self) -> None:
        files = filedialog.askopenfilenames(title="PDFを選択", filetypes=[("PDF", "*.pdf")])
        existing = set(self.file_list.get(0, "end"))
        for filename in files:
            if filename not in existing:
                self.file_list.insert("end", filename)

    def _remove_selected(self) -> None:
        for index in reversed(self.file_list.curselection()):
            self.file_list.delete(index)

    def _clear_files(self) -> None:
        self.file_list.delete(0, "end")

    def _choose_output(self) -> None:
        folder = filedialog.askdirectory(title="保存先フォルダー")
        if folder:
            self.output_var.set(folder)

    @staticmethod
    def _output_path(source: Path, folder: str) -> Path:
        destination = Path(folder) if folder else source.parent
        candidate = destination / f"{source.stem}_2値化.pdf"
        number = 2
        while candidate.exists():
            candidate = destination / f"{source.stem}_2値化_{number}.pdf"
            number += 1
        return candidate

    def _start(self) -> None:
        files = [Path(item) for item in self.file_list.get(0, "end")]
        if not files:
            messagebox.showinfo(APP_NAME, "処理するPDFを追加してください。")
            return
        output = self.output_var.get().strip()
        if output and not Path(output).is_dir():
            messagebox.showerror(APP_NAME, "保存先フォルダーが見つかりません。")
            return

        options = OptimizeOptions(
            dpi=int(self.dpi_var.get()),
            auto_contrast=self.auto_contrast_var.get(),
            contrast=round(float(self.contrast_var.get()), 2),
            sharpen=self.sharpen_var.get(),
            threshold_offset=round(float(self.threshold_var.get())),
            include_small_images=self.include_small_var.get(),
        )
        self._cancel.clear()
        self.start_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.progress.configure(value=0, maximum=max(len(files), 1))
        self._worker = threading.Thread(
            target=self._run_batch, args=(files, output, options), daemon=True
        )
        self._worker.start()

    def _run_batch(self, files: list[Path], output: str, options: OptimizeOptions) -> None:
        results: list[OptimizeResult] = []
        try:
            for file_index, source in enumerate(files):
                if self._cancel.is_set():
                    raise CancelledError("処理を中止しました。")

                def progress(message: str, current: int, total: int) -> None:
                    fraction = (current / total) if total else 0
                    self._events.put(("progress", (file_index + fraction, len(files), source.name, message)))

                result = optimize_pdf(
                    source,
                    self._output_path(source, output),
                    options,
                    progress=progress,
                    cancel_event=self._cancel,
                )
                results.append(result)
                self._events.put(("progress", (file_index + 1, len(files), source.name, "完了")))
            self._events.put(("done", results))
        except CancelledError as exc:
            self._events.put(("cancelled", str(exc)))
        except Exception:
            self._events.put(("error", traceback.format_exc()))

    def _request_cancel(self) -> None:
        self._cancel.set()
        self.cancel_button.configure(state="disabled")
        self.status_var.set("現在の画像処理が終わり次第、中止します。")

    def _poll_events(self) -> None:
        try:
            while True:
                kind, payload = self._events.get_nowait()
                if kind == "progress":
                    value, maximum, filename, message = payload
                    self.progress.configure(value=value, maximum=maximum)
                    self.status_var.set(f"{filename}: {message}")
                elif kind == "done":
                    self._finish(payload)
                elif kind == "cancelled":
                    self._reset_buttons()
                    self.status_var.set(str(payload))
                elif kind == "error":
                    self._reset_buttons()
                    self.status_var.set("エラーで停止しました。")
                    messagebox.showerror(APP_NAME, str(payload))
        except queue.Empty:
            pass
        self.after(100, self._poll_events)

    def _finish(self, results: list[OptimizeResult]) -> None:
        self._reset_buttons()
        converted = sum(result.converted_images for result in results)
        before = sum(result.bytes_before for result in results)
        after = sum(result.bytes_after for result in results)
        reduction = (1 - after / before) * 100 if before else 0
        warning_count = sum(len(result.warnings) for result in results)
        self.status_var.set(f"完了: {len(results)}ファイル、{converted}画像を2値化")
        message = (
            f"{len(results)}ファイルを保存しました。\n"
            f"2値化した画像: {converted}\n"
            f"容量: {before / 1024 / 1024:.1f} MB → {after / 1024 / 1024:.1f} MB "
            f"（{reduction:.0f}%削減）"
        )
        if warning_count:
            message += f"\n\n処理できなかった画像・注意事項: {warning_count}件"
        messagebox.showinfo(APP_NAME, message)

    def _reset_buttons(self) -> None:
        self.start_button.configure(state="normal")
        self.cancel_button.configure(state="disabled")
        self._worker = None

    def _on_close(self) -> None:
        if self._worker and self._worker.is_alive():
            if not messagebox.askyesno(APP_NAME, "処理中です。中止して終了しますか？"):
                return
            self._cancel.set()
        self.destroy()


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--optimize":
        source = Path(sys.argv[2])
        target = source.with_name(f"{source.stem}_2値化.pdf")
        result = optimize_pdf(source, target, OptimizeOptions())
        print(target)
        print(f"converted={result.converted_images}")
        return 0
    OptimizerApp().mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
