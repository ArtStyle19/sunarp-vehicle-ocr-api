"""Normalisation helpers for plates, labels, numbers and redaction masks.

Two rules drive everything in this module:

* **Never invent a value.** Anything that cannot be parsed confidently stays ``None``.
* **Never normalise too broadly.** Punctuation is not stripped globally, because ``2.89`` must
  survive as ``2.89`` and not collapse into ``289``.
"""

from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher

__all__ = [
    "normalize_plate",
    "plate_confusables",
    "plates_match",
    "normalize_label_token",
    "matches_ordinal_marker",
    "strip_leading_separator",
    "parse_decimal",
    "parse_int",
    "looks_masked",
]

# ``##########`` / ``********`` when the mask survives OCR (or comes from native text) verbatim.
_LITERAL_MASK_RE = re.compile(r"^[#*░-▓]{3,}$")

# Leading colon and the glyphs Tesseract commonly substitutes for one.
_LEADING_SEPARATOR_RE = re.compile(r"^[\s:;,.·|¦!\-—–_]+")

# A token normalising to one of these is the "N" of ``N Cilindros`` / ``Nro Ejes``.
_ORDINAL_MARKER_RE = re.compile(r"^n(?:o|ro|0)?$")


def _strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", text)
    return "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")


def normalize_plate(plate: str) -> str:
    """``2ZR-315`` -> ``2ZR315``. Keeps only alphanumerics and upper-cases them."""
    return re.sub(r"[^A-Za-z0-9]", "", _strip_accents(plate)).upper()


def plate_confusables(plate: str) -> str:
    """Fold glyphs OCR routinely confuses, for *comparison only*.

    The sample's plate ``A0A952`` is read by Tesseract as ``ADA952``; comparing the folded forms
    avoids a spurious ``PLATE_MISMATCH`` warning. This is never used to produce an output value.
    """
    folded = normalize_plate(plate)
    table = str.maketrans({"O": "0", "D": "0", "Q": "0", "I": "1", "L": "1", "S": "5", "Z": "2", "B": "8"})
    return folded.translate(table)


#: Folded plates at or above this similarity are treated as the same vehicle. Measured on real
#: output: OCR inserting one phantom character scores 0.923, while genuinely different plates --
#: including near-identical ones like A1B738 vs A1B759 -- score at most 0.667.
PLATE_MATCH_MIN_RATIO = 0.85


def plates_match(left: str, right: str) -> bool:
    """Compare two plates tolerantly enough to absorb OCR noise, but not a different vehicle."""
    a, b = plate_confusables(left), plate_confusables(right)
    if not a or not b:
        return False
    if a == b:
        return True
    return SequenceMatcher(None, a, b).ratio() >= PLATE_MATCH_MIN_RATIO


def normalize_label_token(token: str) -> str:
    """Fold one label token to ``[a-z0-9]``.

    Accents are removed via Unicode decomposition -- crucially *not* by stripping a character
    class that happens to contain a plain ``o``, which would turn ``Ancho`` into ``anch`` and
    ``Tipo Uso`` into ``tip us`` and break every label containing the letter o.
    """
    return re.sub(r"[^a-z0-9]", "", _strip_accents(token).lower())


def matches_ordinal_marker(token: str) -> bool:
    """True for the ``N`` token of a numbered label.

    Covers ``N``, ``N.``, ``N°``, ``Nº``, ``Nro``, ``No`` and the corruptions Tesseract
    actually produced on the sample: ``N?``, ``N*``, ``N?*``.
    """
    normalized = normalize_label_token(token)
    return bool(normalized) and len(normalized) <= 3 and bool(_ORDINAL_MARKER_RE.match(normalized))


def strip_leading_separator(text: str) -> str:
    """Remove a leading colon (or colon-lookalike) from a value string."""
    return _LEADING_SEPARATOR_RE.sub("", text).strip()


def parse_decimal(raw: str) -> Decimal | None:
    """Parse a SUNARP numeric value into an exact :class:`~decimal.Decimal`.

    Accepts both ``2.89`` and ``2,89``. Returns ``None`` -- never a guess -- when the text is not
    unambiguously a plain number.
    """
    text = strip_leading_separator(raw).strip()
    if not text:
        return None
    text = text.replace(" ", "")

    # A single comma is a decimal separator in these documents; normalise it to a dot.
    if text.count(",") == 1 and text.count(".") == 0:
        text = text.replace(",", ".")
    # Reject anything with more than one separator (e.g. thousands grouping): too ambiguous
    # to resolve without guessing.
    if text.count(".") > 1 or "," in text:
        return None
    if not re.fullmatch(r"[+-]?\d+(?:\.\d+)?", text):
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def parse_int(raw: str) -> int | None:
    """Parse a whole-number value. Rejects decimals rather than truncating them."""
    text = strip_leading_separator(raw).strip().replace(" ", "")
    if not re.fullmatch(r"[+-]?\d+", text):
        return None
    return int(text)


def looks_masked(text: str, confidence: float | None = None) -> bool:
    """Detect a redacted value such as ``##########``.

    The sample proves this cannot be done by searching for ``#``: under ``-l spa`` the mask is
    recognised as ``AAAAAIAAAAAAE`` / ``ad ddidaididid``, and under ``-l eng`` as ``HHAHHAHAAEE``.
    What *is* reliable is the combination of: no digits, an implausibly small distinct-character
    set for the length, and (when available) a near-zero Tesseract confidence.
    """
    stripped = text.strip()
    if not stripped:
        return False
    if _LITERAL_MASK_RE.match(re.sub(r"\s+", "", stripped)):
        return True

    compact = re.sub(r"[^A-Za-z0-9]", "", stripped)
    if len(compact) < 4 or any(ch.isdigit() for ch in compact):
        return False

    distinct = len(set(compact.lower()))
    # A long run built from two or three glyphs is a mask, not a word.
    if distinct <= 3 and len(compact) >= 6:
        return True
    # With a near-zero confidence a looser ratio is still conclusive. Real Spanish words never
    # reach this shape: "interprovincial" has 11 distinct characters across 15.
    return (
        confidence is not None
        and confidence <= 30.0
        and distinct <= max(3, len(compact) // 2)
    )
