"""Shared fixtures.

Two kinds of test input are used:

* the **real** reference PDF (``fixtures/A0A952.pdf``), which exercises the whole OCR pipeline
  and pins the golden values;
* **synthetic token layouts**, which reproduce the three-column geometry exactly and let the
  tricky cases (colon fusion, redaction masks, label variants) be tested deterministically and
  fast, without depending on Tesseract's behaviour.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from app.services.section_detector import Section, detect_section
from app.services.tokens import Token

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE_PDF = FIXTURES / "A0A952.pdf"

#: The values the reference document must always yield. This is the regression contract.
GOLDEN_VALUES = {
    "NUM_CILINDROS": 4,
    "PESO_NETO": "2.89",
    "PESO_BRUTO": "4.6",
    "NUM_ASIENTOS": 20,
    "NUM_PASAJEROS": 19,
    "NUM_EJES": 2,
    "NUM_RUEDAS": 6,
    "LONGITUD": "6.99",
    "ANCHO": "1.99",
    "ALTURA": "2.76",
    "NUM_PARTIDA": "52172133",
    "TIPO_USO": "Transporte interprovincial",
}

# Geometry measured on the reference document at 2x render scale.
PAGE_WIDTH = 2380
PAGE_HEIGHT = 3368
COLUMN_LABEL_LEFT = (80, 842, 1608)
COLUMN_COLON_X = (323, 1087, 1883)
COLUMN_VALUE_LEFT = (342, 1106, 1902)
ROW_PITCH = 56
FIRST_ROW_Y = 1154
GLYPH_WIDTH = 17
TOKEN_HEIGHT = 26


def _token(text: str, x: int, y: int, confidence: float | None = 96.0) -> Token:
    return Token(
        text=text,
        x=x,
        y=y,
        width=max(4, len(text) * GLYPH_WIDTH),
        height=TOKEN_HEIGHT,
        confidence=confidence,
        page=1,
    )


def build_tokens(
    columns: list[list[tuple[str, str]]],
    *,
    fuse_colon: set[tuple[int, int]] | None = None,
    value_confidence: dict[tuple[int, int], float] | None = None,
    overlap_labels: dict[tuple[int, int], str] | None = None,
) -> list[Token]:
    """Build a synthetic three-column section.

    ``columns[c]`` is the ordered ``(label, value)`` rows of column ``c``.

    ``fuse_colon`` marks ``(column, row)`` cells where the colon glyph should be merged into the
    value token and placed on the colon anchor -- reproducing the corruption seen in real
    full-page OCR, where ``N Ejes : 2`` comes back as the single token ``22``.

    ``overlap_labels`` replaces a label with a single garbled token that starts back inside the
    *previous* column and ends where the label ends. This reproduces the real documents where a
    long ``Color 1`` value is printed over the next column's label and OCR fuses them, e.g.
    ``ARUAsientos`` or ``AMARWNiABientos`` in place of ``N Asientos``.
    """
    fuse_colon = fuse_colon or set()
    value_confidence = value_confidence or {}
    overlap_labels = overlap_labels or {}
    tokens = [
        _token("Características", 82, 1071),
        _token("del", 300, 1071),
        _token("Vehículo", 360, 1071),
    ]
    for column_index, rows in enumerate(columns):
        label_left = COLUMN_LABEL_LEFT[column_index]
        colon_x = COLUMN_COLON_X[column_index]
        value_left = COLUMN_VALUE_LEFT[column_index]
        for row_index, (label, value) in enumerate(rows):
            y = FIRST_ROW_Y + row_index * ROW_PITCH
            fused = overlap_labels.get((column_index, row_index))
            if fused is not None:
                # One token, ending in this column's label zone but starting to its left.
                width = len(fused) * GLYPH_WIDTH
                right_edge = label_left + int((colon_x - label_left) * 0.75)
                tokens.append(
                    Token(
                        text=fused,
                        x=right_edge - width,
                        y=y,
                        width=width,
                        height=TOKEN_HEIGHT,
                        confidence=17.0,
                        page=1,
                    )
                )
            else:
                cursor = label_left
                for word in label.split():
                    tokens.append(_token(word, cursor, y))
                    cursor += len(word) * GLYPH_WIDTH + GLYPH_WIDTH
            confidence = value_confidence.get((column_index, row_index), 96.0)
            if (column_index, row_index) in fuse_colon:
                # The colon is not recognised separately; it becomes the first glyph of the value
                # token, which therefore starts on the colon anchor.
                tokens.append(_token(f":{value}".replace(":", "2", 1), colon_x, y, confidence))
            else:
                tokens.append(_token(":", colon_x, y, None))
                cursor = value_left
                for word in value.split():
                    tokens.append(_token(word, cursor, y, confidence))
                    cursor += len(word) * GLYPH_WIDTH + GLYPH_WIDTH
    tokens.append(_token("NO", 1100, 1920))
    tokens.append(_token("REGISTRA", 1150, 1920))
    tokens.append(_token("AFECTACIONES", 1300, 1920))
    return tokens


class FakePageSource:
    """A :class:`~app.services.tokens.PageSource` over synthetic tokens.

    ``read_cell`` returns the tokens whose centres fall inside the requested rectangle, which is
    what a clean per-cell OCR pass would produce.
    """

    is_ocr = True

    def __init__(
        self,
        tokens: list[Token],
        *,
        width: int = PAGE_WIDTH,
        height: int = PAGE_HEIGHT,
        crop_overrides: dict[int, tuple[str, float]] | None = None,
    ):
        self._tokens = tokens
        self.width = width
        self.height = height
        # Forces the isolated-cell pass to read something different from the page token grid,
        # which is how the two passes disagree on real documents.
        self._crop_overrides = crop_overrides or {}

    def tokens(self) -> list[Token]:
        return self._tokens

    def read_cell(self, box: tuple[int, int, int, int]) -> tuple[str, float | None]:
        left, top, right, bottom = box
        for y_center, override in self._crop_overrides.items():
            if top <= y_center <= bottom:
                return override
        inside = [
            t
            for t in self._tokens
            if left <= t.x + t.width / 2 <= right and top <= t.center_y <= bottom
        ]
        if not inside:
            return "", None
        inside.sort(key=lambda t: t.x)
        text = " ".join(t.text for t in inside)
        # A per-cell read never includes the colon: the crop starts to the right of it.
        text = text.lstrip(": ").strip()
        confidences = [t.confidence for t in inside if t.confidence is not None]
        return text, (sum(confidences) / len(confidences) if confidences else None)


def row_y(row_index: int) -> int:
    """Vertical centre of a synthetic row, for targeting a crop override."""
    return FIRST_ROW_Y + row_index * ROW_PITCH + TOKEN_HEIGHT // 2


def build_page(
    columns: list[list[tuple[str, str]]],
    *,
    crop_overrides: dict[int, tuple[str, float]] | None = None,
    **kwargs,
) -> tuple[FakePageSource, Section]:
    tokens = build_tokens(columns, **kwargs)
    source = FakePageSource(tokens, crop_overrides=crop_overrides)
    section = detect_section(tokens, source.width, source.height)
    return source, section


@pytest.fixture(scope="session")
def sample_pdf_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A private copy of the reference PDF, so tests never mutate the fixture."""
    if not SAMPLE_PDF.exists():  # pragma: no cover
        pytest.skip("reference PDF fixture is not available")
    destination = tmp_path_factory.mktemp("pdf") / "A0A952.pdf"
    shutil.copyfile(SAMPLE_PDF, destination)
    return destination
