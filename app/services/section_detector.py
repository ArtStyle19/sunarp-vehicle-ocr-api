"""Locate the 'Caracteristicas del Vehiculo' section and derive its column geometry.

Why this exists at all: the section is laid out as **three independent label/colon/value
columns**, and their rows are staggered relative to one another. Flat OCR text therefore
interleaves columns onto shared lines (``N Cilindros : 4    Ancho : 1.99``), and at higher render
scales Tesseract's reading order collapses completely. Any regex over page text is unsafe; the
association has to be geometric.

Everything here is derived from the page at runtime and expressed relative to page size, so it
survives a change of resolution. Measured fractions from the reference document are kept only as
a last-resort fallback.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

from app.services.tokens import Token
from app.utils.normalization import normalize_label_token

#: Header/footer keywords that bound the section.
_HEADER_KEYWORD = "caracteristicas"
# NOTE: "inscripcion" must NOT be listed here. The section itself contains the value
# ``Inmatriculac. : INSCRIPCION``, which would truncate the section above half its fields.
_FOOTER_KEYWORDS = ("afectaciones", "titulos")

# Tolerances, all relative to page size so they hold at any render scale.
COLON_CLUSTER_TOL_W = 0.030
ROW_BAND_TOL_H = 0.008
COLON_ANCHOR_TOL_W = 0.006
CELL_LEFT_INSET_W = 0.007
CELL_RIGHT_MARGIN_W = 0.006
LABEL_RUN_GAP_GLYPHS = 2.6
SECTION_FALLBACK_HEIGHT_H = 0.30

#: Measured on the reference SUNARP boleta, as fractions of page width. Used only when dynamic
#: detection cannot find three colon columns.
FALLBACK_COLUMN_FRACTIONS = (
    (0.0336, 0.1357, 0.1437),
    (0.3538, 0.4567, 0.4647),
    (0.6756, 0.7912, 0.7992),
)


@dataclass(frozen=True, slots=True)
class Column:
    """One label/colon/value column."""

    index: int
    label_left: int
    colon_x: int
    value_left: int
    right: int

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "label_left": self.label_left,
            "colon_x": self.colon_x,
            "value_left": self.value_left,
            "right": self.right,
        }


@dataclass(frozen=True, slots=True)
class Section:
    top: int
    bottom: int
    columns: tuple[Column, ...]
    #: False when the header could not be found and the whole page was used instead.
    header_found: bool
    #: True when column geometry came from the measured fallback rather than this document.
    used_fallback_columns: bool

    def contains(self, token: Token) -> bool:
        return self.top <= token.center_y <= self.bottom


def cluster(values: list[float], tolerance: float) -> list[list[float]]:
    """Group sorted values into runs separated by more than ``tolerance``."""
    groups: list[list[float]] = []
    for value in sorted(values):
        if groups and value - groups[-1][-1] <= tolerance:
            groups[-1].append(value)
        else:
            groups.append([value])
    return groups


def band_rows(tokens: list[Token], tolerance: float) -> list[list[Token]]:
    """Group tokens into visual rows, then order each row left-to-right.

    Banding before sorting is essential: sorting tokens by raw ``y`` scrambles order *within* a
    row, because a word sitting one pixel higher than its neighbour sorts ahead of it (observed
    on the sample: ``Asientos`` at y=1768 sorting before its own ``N`` at y=1769).
    """
    rows: list[list[Token]] = []
    current: list[Token] = []
    for token in sorted(tokens, key=lambda t: t.center_y):
        if current and token.center_y - current[-1].center_y > tolerance:
            rows.append(current)
            current = []
        current.append(token)
    if current:
        rows.append(current)
    for row in rows:
        row.sort(key=lambda t: t.x)
    return rows


def _median_glyph_width(tokens: list[Token]) -> float:
    widths = [t.width / len(t.text) for t in tokens if len(t.text) >= 3 and t.width > 0]
    return statistics.median(widths) if widths else 6.0


def find_section_bounds(tokens: list[Token], page_height: int) -> tuple[int, int, bool]:
    """Return ``(top, bottom, header_found)`` for the characteristics section."""
    header_candidates = [t for t in tokens if normalize_label_token(t.text).startswith(_HEADER_KEYWORD)]
    if not header_candidates:
        return 0, page_height, False

    header = min(header_candidates, key=lambda t: t.y)
    top = header.y
    footer_ys = [
        t.y
        for t in tokens
        if t.y > header.bottom and normalize_label_token(t.text) in _FOOTER_KEYWORDS
    ]
    fallback_bottom = min(page_height, top + int(SECTION_FALLBACK_HEIGHT_H * page_height))
    bottom = min(footer_ys) if footer_ys else fallback_bottom
    if bottom <= top:
        bottom = fallback_bottom
    return top, bottom, True


def _label_left_for_anchor(
    rows: list[list[Token]], anchor: int, anchor_tol: int, gap_limit: float
) -> int | None:
    """Find where the label column feeding ``anchor`` starts.

    For every row that has a colon at this anchor, walk leftwards across the label's own words
    (small inter-word gaps) and record where that run begins. The **median** of those starts is
    the column edge -- not the minimum, because a single OCR artefact can bridge the gap to the
    previous column and drag the minimum far too far left (a stray em-dash did exactly that on
    the sample at 2x).
    """
    starts: list[int] = []
    for row in rows:
        if not any(abs(t.x - anchor) <= anchor_tol and t.text.startswith(":") for t in row):
            continue
        left_of_colon = [t for t in row if t.right <= anchor + 2]
        if not left_of_colon:
            continue
        run = [left_of_colon[-1]]
        for token in reversed(left_of_colon[:-1]):
            if run[-1].x - token.right <= gap_limit:
                run.append(token)
            else:
                break
        starts.append(min(t.x for t in run))
    if not starts:
        return None
    return int(statistics.median(starts))


def detect_columns(
    section_tokens: list[Token], page_width: int, page_height: int
) -> tuple[list[Column], bool]:
    """Derive column geometry from the page. Returns ``(columns, used_fallback)``."""
    colon_tol = COLON_CLUSTER_TOL_W * page_width
    anchor_tol = int(COLON_ANCHOR_TOL_W * page_width)
    colon_xs = [t.x for t in section_tokens if t.text.startswith(":") and len(t.text.strip()) <= 2]
    anchors = [int(min(group)) for group in cluster(colon_xs, colon_tol) if len(group) >= 3]

    if len(anchors) < 2:
        return _fallback_columns(page_width), True

    rows = band_rows(section_tokens, ROW_BAND_TOL_H * page_height)
    gap_limit = LABEL_RUN_GAP_GLYPHS * _median_glyph_width(section_tokens)

    label_lefts: list[int] = []
    for anchor in anchors:
        left = _label_left_for_anchor(rows, anchor, anchor_tol, gap_limit)
        if left is None:
            return _fallback_columns(page_width), True
        label_lefts.append(left)

    # Column edges must be strictly increasing to be usable.
    if any(b <= a for a, b in zip(label_lefts, label_lefts[1:], strict=False)):
        return _fallback_columns(page_width), True

    right_margin = int(CELL_RIGHT_MARGIN_W * page_width)
    columns: list[Column] = []
    for index, (label_left, anchor) in enumerate(zip(label_lefts, anchors, strict=True)):
        following = [t.x for t in section_tokens if anchor + anchor_tol < t.x]
        value_left = min(following) if following else anchor + anchor_tol
        right = label_lefts[index + 1] - right_margin if index + 1 < len(label_lefts) else page_width
        columns.append(
            Column(
                index=index,
                label_left=label_left,
                colon_x=anchor,
                value_left=int(value_left),
                right=int(right),
            )
        )
    return columns, False


def _fallback_columns(page_width: int) -> list[Column]:
    columns: list[Column] = []
    fractions = FALLBACK_COLUMN_FRACTIONS
    for index, (label_f, colon_f, value_f) in enumerate(fractions):
        right = (
            int(fractions[index + 1][0] * page_width) - int(CELL_RIGHT_MARGIN_W * page_width)
            if index + 1 < len(fractions)
            else page_width
        )
        columns.append(
            Column(
                index=index,
                label_left=int(label_f * page_width),
                colon_x=int(colon_f * page_width),
                value_left=int(value_f * page_width),
                right=right,
            )
        )
    return columns


def detect_section(tokens: list[Token], page_width: int, page_height: int) -> Section:
    """Locate the section and its columns, falling back to the full page when necessary."""
    top, bottom, header_found = find_section_bounds(tokens, page_height)
    section_tokens = [t for t in tokens if top <= t.center_y <= bottom]
    if not section_tokens:
        section_tokens = tokens
        top, bottom = 0, page_height
    columns, used_fallback = detect_columns(section_tokens, page_width, page_height)
    return Section(
        top=top,
        bottom=bottom,
        columns=tuple(columns),
        header_found=header_found,
        used_fallback_columns=used_fallback,
    )
