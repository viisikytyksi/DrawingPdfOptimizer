# -*- coding: utf-8 -*-
"""2つのPDFを描画して画素差を数え、差分の大きい領域の比較パネルを作る。

使い方: python tools/diff_render.py <pdfA.label> <pdfA.path> <pdfB.label> <pdfB.path> <out_dir>
"""
from __future__ import annotations

import sys
from pathlib import Path

import pypdfium2 as pdfium
from PIL import ImageChops, Image, ImageDraw


def render(doc, index, dpi):
    return doc[index].render(scale=dpi / 72.0).to_pil().convert("L")


def main(argv: list[str]) -> int:
    name_a = argv[1]
    path_a = argv[2]
    name_b = argv[3]
    path_b = argv[4]
    out_dir = Path(argv[5])
    out_dir.mkdir(parents=True, exist_ok=True)

    doc_a = pdfium.PdfDocument(str(path_a))
    doc_b = pdfium.PdfDocument(str(path_b))
    pages = min(len(doc_a), len(doc_b))

    rows = []
    for page in range(pages):
        diff = ImageChops.difference(render(doc_a, page, 150), render(doc_b, page, 150))
        hist = diff.histogram()
        rows.append((page, sum(hist[1:]), diff.getbbox() or (0, 0, diff.width, diff.height)))

    total = sum(r[1] for r in rows)
    with (out_dir / "page_diff_stats.txt").open("w", encoding="utf-8") as fh:
        fh.write("page\tdiff_pixels(150dpi)\n")
        for page, count, _ in rows:
            fh.write(f"{page + 1}\t{count}\n")

    top = sorted(rows, key=lambda r: r[1], reverse=True)[:4]

    for page, _count, bbox in top:
        a150 = render(doc_a, page, 150)
        cx, cy = (bbox[0] + bbox[2]) // 2, (bbox[1] + bbox[3]) // 2
        half = 400
        box150 = (
            max(0, cx - half),
            max(0, cy - half),
            min(a150.width, cx + half),
            min(a150.height, cy + half),
        )
        factor = 4
        crop = tuple(int(v * factor) for v in box150)
        panel_specs = [(f"{name_a}(150dpi参照で拡大)", doc_a, 600), (f"{name_b}(600dpi)", doc_b, 600)]
        panels = []
        for label, doc, dpi in panel_specs:
            img = render(doc, page, dpi)
            panels.append((label, img.crop(crop)))
        pw = 900
        ph = round(pw * (crop[3] - crop[1]) / (crop[2] - crop[0]))
        stacked = Image.new("L", (pw + 8, len(panels) * (ph + 30) + 8), 150)
        draw = ImageDraw.Draw(stacked)
        y = 4
        for label, panel in panels:
            panel = panel.resize((pw, ph), Image.BOX)
            draw.text((4, y + 8), f"{label}  page {page + 1}", fill=0)
            stacked.paste(panel, (4, y + 30))
            y += ph + 30
        stacked.save(out_dir / f"page{page + 1}_panel.png")
        print("saved", out_dir / f"page{page + 1}_panel.png")

    print(f"total diff pixels over {pages} pages: {total}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
