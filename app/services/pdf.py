"""PDF handling: validation, native-text detection, adaptive rasterisation.

The pipeline always tries the PDF's own text layer first and only rasterises when that fails.
The reference SUNARP boleta has *no* text layer at all -- it is a single 1190x1684 JPEG at 72 ppi
inside an A2 page -- so it takes the OCR path.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

try:  # PyMuPDF >= 1.24 exposes the package as ``pymupdf``; ``fitz`` is the legacy alias.
    import pymupdf as fitz
except ImportError:  # pragma: no cover - older PyMuPDF
    import fitz
from PIL import Image

from app.config import Settings
from app.core.exceptions import PdfInvalidError
from app.services.ocr import OcrPageSource, image_to_tokens
from app.services.section_detector import Section, detect_section
from app.services.tokens import PageSource, Token
from app.utils.normalization import normalize_label_token

_HEADER_KEYWORD = "caracteristicas"


class NativePageSource:
    """:class:`~app.services.tokens.PageSource` backed by the PDF's own text layer.

    ``read_cell`` re-reads the rectangle through PyMuPDF, which gives the dual-pass design a
    genuine second opinion here too. Confidence is always ``None``: no OCR ran, so there is no
    confidence to report and none is invented.
    """

    is_ocr = False

    def __init__(self, page: fitz.Page, tokens: list[Token]):
        self._page = page
        self._tokens = tokens
        rect = page.rect
        self.width = int(rect.width)
        self.height = int(rect.height)

    def tokens(self) -> list[Token]:
        return self._tokens

    def read_cell(self, box: tuple[int, int, int, int]) -> tuple[str, float | None]:
        left, top, right, bottom = box
        if right <= left or bottom <= top:
            return "", None
        text = self._page.get_textbox(fitz.Rect(left, top, right, bottom)) or ""
        return " ".join(text.split()), None


@dataclass
class DocumentAnalysis:
    source: PageSource
    section: Section
    page_count: int
    page_index: int
    native_text_available: bool
    ocr_used: bool
    render_scale: float | None
    raw_text: str


def open_document(path: str) -> fitz.Document:
    try:
        document = fitz.open(path)
    except Exception as exc:  # PyMuPDF raises a variety of types for malformed input
        raise PdfInvalidError(detail=str(exc)) from exc
    if document.page_count < 1:
        document.close()
        raise PdfInvalidError("The PDF contains no pages.")
    return document


def native_tokens(page: fitz.Page) -> list[Token]:
    """Words from the PDF text layer, in the page's own coordinate space."""
    tokens: list[Token] = []
    for x0, y0, x1, y1, word, *_ in page.get_text("words"):
        stripped = word.strip()
        if not stripped:
            continue
        tokens.append(
            Token(
                text=stripped,
                x=int(x0),
                y=int(y0),
                width=max(1, int(x1 - x0)),
                height=max(1, int(y1 - y0)),
                confidence=None,
                page=page.number + 1,
            )
        )
    return tokens


def has_section_header(tokens: list[Token]) -> bool:
    return any(normalize_label_token(t.text).startswith(_HEADER_KEYWORD) for t in tokens)


def render_page(page: fitz.Page, scale: float) -> Image.Image:
    pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
    return Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)


def _adaptive_scale(tokens: list[Token], probe_scale: float, settings: Settings) -> float:
    """Pick a render scale that puts glyph boxes near the size Tesseract reads best.

    A fixed 300 DPI is the wrong default for these documents: the page content is a 72 ppi scan,
    so 300 DPI is pure interpolation, and it measurably *degrades* results -- it drops the
    decimal point from ``4.6`` and misreads ``52172133`` as ``92172133``. Scaling to a target
    glyph height instead lands on ~2x for this template and still handles genuine 300 DPI scans.
    """
    heights = [t.height for t in tokens if len(t.text) >= 2 and t.height > 0]
    if not heights:
        return probe_scale
    median_height = statistics.median(heights)
    if median_height <= 0:
        return probe_scale
    desired = probe_scale * (settings.ocr_target_token_height_px / median_height)
    return max(settings.ocr_min_scale, min(settings.ocr_max_scale, desired))


def _ocr_page(page: fitz.Page, settings: Settings) -> tuple[OcrPageSource, float]:
    """Rasterise and OCR one page, choosing the scale adaptively."""
    probe_scale = settings.ocr_probe_scale
    image = render_page(page, probe_scale)
    tokens = image_to_tokens(
        image, lang=settings.ocr_lang, timeout=settings.ocr_timeout_seconds, page=page.number + 1
    )

    scale = _adaptive_scale(tokens, probe_scale, settings)
    if abs(scale - probe_scale) > 0.25:
        image = render_page(page, scale)
        tokens = image_to_tokens(
            image, lang=settings.ocr_lang, timeout=settings.ocr_timeout_seconds, page=page.number + 1
        )
    else:
        scale = probe_scale

    source = OcrPageSource(
        image, tokens, lang=settings.ocr_lang, timeout=settings.ocr_timeout_seconds
    )
    return source, scale


def analyze_document(document: fitz.Document, settings: Settings) -> DocumentAnalysis:
    """Find the page carrying the characteristics section and build a page source for it."""
    page_count = document.page_count
    native_available = False

    # 1. Native text layer, if there is a usable one.
    native_fallback: tuple[int, list[Token]] | None = None
    for index in range(page_count):
        page = document.load_page(index)
        tokens = native_tokens(page)
        if len(tokens) >= settings.native_text_min_words:
            native_available = True
            if native_fallback is None:
                native_fallback = (index, tokens)
            if has_section_header(tokens):
                source = NativePageSource(page, tokens)
                section = detect_section(tokens, source.width, source.height)
                return DocumentAnalysis(
                    source=source,
                    section=section,
                    page_count=page_count,
                    page_index=index,
                    native_text_available=True,
                    ocr_used=False,
                    render_scale=None,
                    raw_text=" ".join(t.text for t in tokens),
                )

    # 2. Otherwise rasterise and OCR, stopping at the first page that carries the section.
    first_ocr: tuple[int, OcrPageSource, float] | None = None
    for index in range(min(page_count, settings.max_ocr_pages)):
        page = document.load_page(index)
        source, scale = _ocr_page(page, settings)
        tokens = source.tokens()
        if first_ocr is None:
            first_ocr = (index, source, scale)
        if has_section_header(tokens):
            section = detect_section(tokens, source.width, source.height)
            return DocumentAnalysis(
                source=source,
                section=section,
                page_count=page_count,
                page_index=index,
                native_text_available=native_available,
                ocr_used=True,
                render_scale=scale,
                raw_text=" ".join(t.text for t in tokens),
            )

    # 3. Nothing matched the header. Fall back to the first page so the caller can still decide
    #    whether the section is genuinely absent.
    if first_ocr is not None:
        index, source, scale = first_ocr
        section = detect_section(source.tokens(), source.width, source.height)
        return DocumentAnalysis(
            source=source,
            section=section,
            page_count=page_count,
            page_index=index,
            native_text_available=native_available,
            ocr_used=True,
            render_scale=scale,
            raw_text=" ".join(t.text for t in source.tokens()),
        )

    index, tokens = native_fallback if native_fallback else (0, native_tokens(document.load_page(0)))
    page = document.load_page(index)
    source = NativePageSource(page, tokens)
    section = detect_section(tokens, source.width, source.height)
    return DocumentAnalysis(
        source=source,
        section=section,
        page_count=page_count,
        page_index=index,
        native_text_available=native_available,
        ocr_used=False,
        render_scale=None,
        raw_text=" ".join(t.text for t in tokens),
    )
