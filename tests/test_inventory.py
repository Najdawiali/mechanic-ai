"""The inventory script, on a small made-up folder."""

import sys
from pathlib import Path

import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import inventory  # noqa: E402


def make_pdf(path: Path, pages: list[str]) -> None:
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        if text:
            page.insert_text((72, 72), text)
    doc.save(path)
    doc.close()


def test_inventory_finds_types_duplicates_and_text_layers(tmp_path):
    root = tmp_path / "reports"
    root.mkdir()
    make_pdf(root / "TAS_a211 EN.pdf", ["Ride height calibration case, page one text", ""])
    (root / "TAS_a211 EN.txt").write_text("Ride height calibration case", encoding="utf-8")
    (root / "TAS_a211 AR.txt").write_text("حالة معايرة ارتفاع السيارة", encoding="utf-8")
    (root / "copy.txt").write_text("Ride height calibration case", encoding="utf-8")

    items = inventory.scan([root])
    by_name = {i.path: i for i in items}
    pdf = by_name["TAS_a211 EN.pdf"]
    assert (pdf.pages, pdf.pages_with_text) == (2, 1)
    assert by_name["TAS_a211 AR.txt"].language == "ar" and by_name["TAS_a211 EN.txt"].language == "en"

    text = inventory.report(items)
    assert "exact duplicates (identical content): 1 group" in text
    assert "some pages without text: 1" in text
    assert "tas a211" in text  # the three formats grouped under one case name


def test_resolve_adds_the_hidden_pdf_extension(tmp_path):
    make_pdf(tmp_path / "DTC p0010.pdf", ["x" * 30])
    assert inventory.resolve(str(tmp_path / "DTC p0010")) == tmp_path / "DTC p0010.pdf"
    assert inventory.resolve(str(tmp_path / "nothing here")) is None


def test_single_file_as_root(tmp_path):
    make_pdf(tmp_path / "one.pdf", ["some text that is long enough"])
    items = inventory.scan([tmp_path / "one.pdf"])
    assert len(items) == 1 and items[0].path == "one.pdf" and items[0].pages == 1
