"""The text of a PDF in reading order, with a marker where each embedded image sits.

Service manuals print some facts as small images inside the text: the two test points of a
measurement, a NOTICE badge, a connector drawing. Plain text extraction drops them silently. Here
every image leaves a marker such as `[image 4]` at its place in the text, so whatever is read
from the image later can be put back exactly where it belongs.
"""

from __future__ import annotations

from dataclasses import dataclass

import pymupdf

MARGIN_TOP, MARGIN_BOTTOM = 27, 30  # the browser's print header and footer


@dataclass(frozen=True)
class PageImage:
    number: int  # as used in the marker: [image 4]
    page: int  # from 1
    box: tuple[float, float, float, float]  # where it is on the page, in points
    pixels: tuple[int, int]
    png: bytes


@dataclass(frozen=True)
class PdfText:
    text: str  # all pages, with [image N] markers and "=== page N ===" lines
    images: list[PageImage]
    header: list[str]  # the lines at the top of the first page (vehicle, system path, title)


def read_pdf_text(data: bytes) -> PdfText:
    images: list[PageImage] = []
    parts: list[str] = []
    header: list[str] = []
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        for page_no, page in enumerate(doc, start=1):
            area = pymupdf.Rect(0, MARGIN_TOP, page.rect.width, page.rect.height - MARGIN_BOTTOM)
            # (top, left, text) for text blocks and image markers alike, then sorted into reading order
            items: list[tuple[float, float, str]] = []
            for x0, y0, x1, y1, text, _, kind in page.get_text("blocks", clip=area):
                if kind == 0 and text.strip():
                    items.append((y0, x0, text.strip()))
            for info in page.get_image_info(xrefs=True):
                box = pymupdf.Rect(info["bbox"])
                if not info.get("xref") or box.is_empty or not area.contains(box):
                    continue
                number = len(images) + 1
                png = page.get_pixmap(clip=box, dpi=300).tobytes("png")  # as printed, enlarged for reading
                images.append(PageImage(number, page_no, tuple(round(v, 1) for v in box), (info["width"], info["height"]), png))
                items.append((box.y0, box.x0, f"[image {number}]"))
            items.sort(key=lambda item: (round(item[0] / 4), item[1]))  # rows about 4 pt apart count as one row
            if page_no == 1:
                header = [line.strip() for _, _, text in items[:6] for line in text.splitlines() if line.strip()][:8]
            if items:
                parts.append(f"=== page {page_no} ===\n" + "\n".join(text for _, _, text in items))
    return PdfText("\n\n".join(parts), images, header)
