"""Tesseract OCR: full-page tokens with bounding boxes, plus isolated cell re-reads."""

from __future__ import annotations

from typing import Any

import pytesseract
from PIL import Image
from pytesseract import Output

from app.core.exceptions import OcrFailedError, OcrTimeoutError
from app.services.tokens import Token

#: Full page with automatic layout analysis -- gives us the token grid we do geometry on.
PSM_PAGE = 3
#: A single text line -- used for one isolated value cell.
PSM_SINGLE_LINE = 7


def _coerce_confidence(raw: Any) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    # Tesseract reports -1 for non-text levels.
    return None if value < 0 else value


def image_to_tokens(
    image: Image.Image,
    *,
    lang: str,
    psm: int = PSM_PAGE,
    timeout: int = 120,
    page: int = 1,
) -> list[Token]:
    """Run OCR and return every word with its box and confidence."""
    try:
        data = pytesseract.image_to_data(
            image, lang=lang, config=f"--psm {psm}", output_type=Output.DICT, timeout=timeout
        )
    except RuntimeError as exc:  # pytesseract raises RuntimeError on timeout
        raise OcrTimeoutError(detail=str(exc)) from exc
    except pytesseract.TesseractError as exc:
        raise OcrFailedError(detail=str(exc)) from exc

    tokens: list[Token] = []
    for index, text in enumerate(data["text"]):
        stripped = text.strip()
        if not stripped:
            continue
        tokens.append(
            Token(
                text=stripped,
                x=int(data["left"][index]),
                y=int(data["top"][index]),
                width=int(data["width"][index]),
                height=int(data["height"][index]),
                confidence=_coerce_confidence(data["conf"][index]),
                page=page,
            )
        )
    return tokens


def read_cell(
    image: Image.Image,
    box: tuple[int, int, int, int],
    *,
    lang: str,
    timeout: int = 60,
) -> tuple[str, float | None]:
    """OCR a single value cell as one text line.

    Returns the joined text and the mean confidence of its words. Cropping tightly is what makes
    this pass trustworthy, but the crop must keep vertical padding: clipping the baseline turns
    ``4.6`` into ``46``.
    """
    left, top, right, bottom = box
    if right <= left or bottom <= top:
        return "", None
    crop = image.crop((left, top, right, bottom))
    tokens = image_to_tokens(crop, lang=lang, psm=PSM_SINGLE_LINE, timeout=timeout)
    if not tokens:
        return "", None
    text = " ".join(token.text for token in tokens).strip()
    confidences = [t.confidence for t in tokens if t.confidence is not None]
    mean_confidence = sum(confidences) / len(confidences) if confidences else None
    return text, mean_confidence


class OcrPageSource:
    """:class:`~app.services.tokens.PageSource` backed by a rendered raster page."""

    is_ocr = True

    def __init__(self, image: Image.Image, tokens: list[Token], *, lang: str, timeout: int = 60):
        self._image = image
        self._tokens = tokens
        self._lang = lang
        self._timeout = timeout
        self.width, self.height = image.size

    def tokens(self) -> list[Token]:
        return self._tokens

    def read_cell(self, box: tuple[int, int, int, int]) -> tuple[str, float | None]:
        return read_cell(self._image, box, lang=self._lang, timeout=self._timeout)
