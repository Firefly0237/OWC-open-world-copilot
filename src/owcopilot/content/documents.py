"""Binary document -> plain text for imported manuscripts and references.

Planners hand us whatever they have: a .docx design doc, a .pdf novel, an .epub. These are all
containers we have to crack open to get the prose out. Each extractor degrades gracefully on a
corrupt file (raising ``ValueError`` with a clear message) and never crashes the import. Both the
manuscript-extraction and the inspiration-library upload paths dispatch through here, so the set
of supported formats is defined once.
"""

from __future__ import annotations

import io
import re
import zipfile
from collections.abc import Callable
from html import unescape
from pathlib import Path
from xml.etree import ElementTree

_DOCX_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def docx_text(data: bytes) -> str:
    """Paragraph text from a .docx (a zip of XML), stdlib-only."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            xml = archive.read("word/document.xml")
    except (zipfile.BadZipFile, KeyError) as e:
        raise ValueError("not a valid .docx file") from e
    root = ElementTree.fromstring(xml)
    paragraphs: list[str] = []
    for paragraph in root.iter(f"{_DOCX_NS}p"):
        text = "".join(node.text or "" for node in paragraph.iter(f"{_DOCX_NS}t")).strip()
        if text:
            paragraphs.append(text)
    return "\n\n".join(paragraphs)


def pdf_text(data: bytes) -> str:
    """Page text from a PDF via pypdf; tolerates per-page extraction failures."""
    try:
        from pypdf import PdfReader
    except ImportError as e:  # pragma: no cover - pypdf is a core dependency
        raise ValueError("PDF import requires the 'pypdf' package") from e
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            reader.decrypt("")  # try the empty password; many PDFs are "encrypted" with none
        pages = list(reader.pages)
    except Exception as e:  # pypdf raises a variety of types on malformed input
        raise ValueError("could not read PDF file") from e
    parts: list[str] = []
    for page in pages:
        try:
            text = (page.extract_text() or "").strip()
        except Exception:
            text = ""  # one unreadable page must not lose the rest of the book
        if text:
            parts.append(text)
    return "\n\n".join(parts)


def epub_text(data: bytes) -> str:
    """Reading-order text from an .epub (a zip of XHTML), stdlib-only.

    Reads the spine declared in the package's .opf for correct chapter order, falling back to all
    (x)html files sorted by name when the manifest is missing or malformed."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
            parts: list[str] = []
            for entry in _epub_reading_order(archive, names):
                try:
                    html = archive.read(entry).decode("utf-8", errors="replace")
                except KeyError:
                    continue
                text = _strip_html(html)
                if text:
                    parts.append(text)
            return "\n\n".join(parts)
    except zipfile.BadZipFile as e:
        raise ValueError("not a valid .epub file") from e


def binary_document_text(data: bytes, filename: str) -> str | None:
    """Extract text for a known binary document format, or ``None`` for text formats."""
    extractor = _BINARY_EXTRACTORS.get(Path(filename).suffix.lower())
    return extractor(data) if extractor else None


_BINARY_EXTRACTORS: dict[str, Callable[[bytes], str]] = {
    ".docx": docx_text,
    ".pdf": pdf_text,
    ".epub": epub_text,
}


def _epub_reading_order(archive: zipfile.ZipFile, names: list[str]) -> list[str]:
    html_names = sorted(n for n in names if n.lower().endswith((".xhtml", ".html", ".htm")))
    try:
        container = archive.read("META-INF/container.xml").decode("utf-8", errors="replace")
        opf_path = re.search(r'full-path="([^"]+)"', container)
        if not opf_path:
            return html_names
        opf_dir = opf_path.group(1).rsplit("/", 1)[0] if "/" in opf_path.group(1) else ""
        opf = ElementTree.fromstring(archive.read(opf_path.group(1)))
        ns = {"opf": "http://www.idpf.org/2007/opf"}
        manifest = {
            item.get("id"): item.get("href")
            for item in opf.iterfind(".//opf:manifest/opf:item", ns)
        }
        order: list[str] = []
        for ref in opf.iterfind(".//opf:spine/opf:itemref", ns):
            href = manifest.get(ref.get("idref"))
            if href:
                order.append(f"{opf_dir}/{href}" if opf_dir else href)
        return order or html_names
    except (KeyError, ElementTree.ParseError, zipfile.BadZipFile):
        return html_names


def _strip_html(html: str) -> str:
    text = unescape(re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.I | re.S))
    text = unescape(re.sub(r"<[^>]+>", " ", text))
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n\s*", "\n\n", text).strip()
