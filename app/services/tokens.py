"""The spatial token type shared by the native-text and OCR paths."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class Token:
    """One word with its bounding box on the rendered page.

    ``confidence`` is ``None`` on the native-text path, where no OCR confidence exists. It is
    never synthesised -- the API contract depends on that.
    """

    text: str
    x: int
    y: int
    width: int
    height: int
    confidence: float | None
    page: int = 1

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height

    @property
    def center_y(self) -> float:
        return self.y + self.height / 2

    def as_dict(self) -> dict:
        return {
            "text": self.text,
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
            "confidence": self.confidence,
            "page": self.page,
        }


class PageSource(Protocol):
    """Everything the extractor needs from a page, independent of native-text vs OCR."""

    #: Page width/height in the same coordinate space as the tokens.
    width: int
    height: int
    #: True when the tokens came from OCR rather than the PDF's own text layer.
    is_ocr: bool

    def tokens(self) -> list[Token]:
        """All tokens on the page."""

    def read_cell(self, box: tuple[int, int, int, int]) -> tuple[str, float | None]:
        """Re-read one rectangle independently, returning ``(text, confidence)``.

        This is the second opinion in the dual-pass read: it isolates a single value cell so a
        neighbouring column can never bleed in, and it side-steps the colon/value glyph fusion
        that corrupts full-page OCR.
        """
