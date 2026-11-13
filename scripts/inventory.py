"""Inventory of the client's data folders. Reads file facts only; changes nothing.

    python scripts/inventory.py                 # folders listed in data/sources.txt
    python scripts/inventory.py "C:\\path\\one" "C:\\path\\two"

For every file: type, size, a content fingerprint (to find exact duplicates) and, for PDFs,
pages, whether each page has a text layer, how many embedded images, and the PDF producer.

Why: before any extraction code we need to know how many files there are, which are
duplicates, which PDFs are text and which are pictures, and which cases exist in several
formats. The output holds file names and counts only, so it is safe to paste into chat.
"""

from __future__ import annotations

import csv
import hashlib
import re
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SOURCES_FILE = REPO / "data" / "sources.txt"
OUT_DIR = REPO / "data" / "inventory"
ARABIC = re.compile(r"[\u0600-\u06FF]")


@dataclass
class FileInfo:
    root: str
    path: str  # relative to the root
    ext: str
    size_kb: float
    fingerprint: str
    pages: int | None = None
    pages_with_text: int | None = None
    text_chars: int | None = None
    images: int | None = None
    producer: str = ""
    language: str = ""  # for text files: ar / en
    error: str = ""


def resolve(raw: str) -> Path | None:
    """Windows hides file extensions, so a 'folder' someone copied may be a PDF."""
    p = Path(raw.strip().strip('"'))
    if p.exists():
        return p
    for ext in (".pdf", ".PDF"):
        q = Path(str(p) + ext)
        if q.exists():
            return q
    return None


def describe(path: Path, root: Path) -> FileInfo:
    data = path.read_bytes()
    rel = path.name if root.is_file() else str(path.relative_to(root))
    info = FileInfo(
        root=root.name,
        path=rel,
        ext=path.suffix.lower() or "(none)",
        size_kb=round(len(data) / 1024, 1),
        fingerprint=hashlib.sha256(data).hexdigest()[:12],
    )
    try:
        if info.ext == ".pdf":
            import pymupdf  # imported here so non-PDF folders work even if it is missing

            with pymupdf.open(stream=data, filetype="pdf") as doc:
                texts = [page.get_text().strip() for page in doc]
                info.pages = len(texts)
                info.pages_with_text = sum(1 for t in texts if len(t) >= 20)
                info.text_chars = sum(len(t) for t in texts)
                info.images = sum(len(page.get_images()) for page in doc)
                info.producer = ((doc.metadata or {}).get("producer") or "")[:40]
        elif info.ext in (".txt", ".md"):
            text = data.decode("utf-8", errors="replace")
            info.text_chars = len(text)
            letters = [c for c in text if c.isalpha()]
            share = len(ARABIC.findall(text)) / max(len(letters), 1)
            info.language = "ar" if share > 0.5 else "en"
    except Exception as exc:  # noqa: BLE001
        info.error = f"{type(exc).__name__}: {str(exc)[:80]}"
    return info


def scan(roots: list[Path]) -> list[FileInfo]:
    out: list[FileInfo] = []
    for root in roots:
        files = [root] if root.is_file() else sorted(p for p in root.rglob("*") if p.is_file())
        out += [describe(f, root) for f in files]
    return out


def case_key(path: str) -> str:
    """Same case in several formats usually shares a file name: strip folder, extension, language tags."""
    stem = Path(path).stem.lower()
    stem = re.sub(r"[\s_\-]*(\(?(ar|en|arabic|english|عربي)\)?)$", "", stem)
    return re.sub(r"[\s_\-]+", " ", stem).strip()


def report(items: list[FileInfo]) -> str:
    lines: list[str] = []
    by_root: dict[str, list[FileInfo]] = defaultdict(list)
    for it in items:
        by_root[it.root].append(it)

    for root, files in by_root.items():
        lines += ["", f"=== {root} ===", f"files: {len(files)}   total: {sum(f.size_kb for f in files) / 1024:.1f} MB"]
        lines.append("by type: " + ", ".join(f"{e} x{n}" for e, n in Counter(f.ext for f in files).most_common()))
        pdfs = [f for f in files if f.ext == ".pdf" and not f.error]
        if pdfs:
            no_text = [f for f in pdfs if f.pages_with_text == 0]
            partial = [f for f in pdfs if 0 < (f.pages_with_text or 0) < (f.pages or 0)]
            lines.append(f"PDFs: {len(pdfs)}, pages: {sum(f.pages or 0 for f in pdfs)}, "
                         f"no text layer: {len(no_text)}, some pages without text: {len(partial)}")
            lines.append("PDF producers: " + ", ".join(
                f"{p or '(blank)'} x{n}" for p, n in Counter(f.producer for f in pdfs).most_common(5)))
        txts = [f for f in files if f.ext in (".txt", ".md")]
        if txts:
            lines.append("text files by language: " + ", ".join(
                f"{k} x{n}" for k, n in Counter(f.language for f in txts).items()))
        errors = [f for f in files if f.error]
        if errors:
            lines.append(f"could not read: {len(errors)}")
            lines += [f"  {f.path}: {f.error}" for f in errors[:10]]

        lines += ["", f"{'type':<6} {'KB':>8} {'pages':>5} {'text pg':>7} {'imgs':>5} {'lang':<4} file"]
        for f in files:
            lines.append(f"{f.ext:<6} {f.size_kb:>8} {f.pages if f.pages is not None else '-':>5} "
                         f"{f.pages_with_text if f.pages_with_text is not None else '-':>7} "
                         f"{f.images if f.images is not None else '-':>5} {f.language:<4} {f.path}")

    dupes = defaultdict(list)
    for it in items:
        dupes[it.fingerprint].append(f"{it.root}/{it.path}")
    exact = [paths for paths in dupes.values() if len(paths) > 1]
    lines += ["", f"=== exact duplicates (identical content): {len(exact)} group(s) ==="]
    lines += ["  " + "  ==  ".join(paths) for paths in exact]

    groups = defaultdict(list)
    for it in items:
        groups[(it.root, case_key(it.path))].append(it.ext + (f"[{it.language}]" if it.language else ""))
    multi = {k: v for k, v in groups.items() if len(v) > 1}
    lines += ["", f"=== same name in several files: {len(multi)} group(s) (same case in several formats, or versions) ==="]
    lines += [f"  {root} / {key}: {', '.join(sorted(v))}" for (root, key), v in sorted(multi.items())]
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    raw = argv or (
        [ln for ln in SOURCES_FILE.read_text(encoding="utf-8").splitlines()
         if ln.strip() and not ln.strip().startswith("#")]
        if SOURCES_FILE.exists() else []
    )
    if not raw:
        print(f"No folders given and {SOURCES_FILE} not found.")
        return 2

    roots: list[Path] = []
    for r in raw:
        p = resolve(r)
        print(f"{'FOUND  ' if p else 'MISSING'} {r}" + (f"   ({'file' if p.is_file() else 'folder'})" if p else ""))
        if p:
            roots.append(p)
    if not roots:
        return 1

    items = scan(roots)
    text = report(items)
    print(text)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    with (OUT_DIR / f"inventory-{stamp}.csv").open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(asdict(items[0]).keys()))
        writer.writeheader()
        writer.writerows(asdict(i) for i in items)
    print(f"\nSaved: {OUT_DIR / f'inventory-{stamp}.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
