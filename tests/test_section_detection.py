"""Section bounds and column geometry."""

from __future__ import annotations

from app.services.section_detector import band_rows, cluster, detect_section, find_section_bounds
from app.services.tokens import Token
from tests.conftest import (
    COLUMN_COLON_X,
    COLUMN_LABEL_LEFT,
    PAGE_HEIGHT,
    PAGE_WIDTH,
    build_page,
    build_tokens,
)
from tests.test_vehicle_characteristics import COLUMN_1, COLUMN_2, COLUMN_3

FULL_LAYOUT = [COLUMN_1, COLUMN_2, COLUMN_3]


def test_detects_three_columns_from_the_page_itself() -> None:
    _source, section = build_page(FULL_LAYOUT)
    assert not section.used_fallback_columns, "geometry should come from the document, not the fallback"
    assert len(section.columns) == 3
    for column, label_left, colon_x in zip(
        section.columns, COLUMN_LABEL_LEFT, COLUMN_COLON_X, strict=True
    ):
        assert abs(column.label_left - label_left) <= 4
        assert abs(column.colon_x - colon_x) <= 4


def test_column_value_cells_do_not_overlap_the_next_column() -> None:
    _source, section = build_page(FULL_LAYOUT)
    for current, following in zip(section.columns, section.columns[1:], strict=False):
        assert current.right <= following.label_left
        assert current.colon_x < current.right


def test_section_is_bounded_by_header_and_footer() -> None:
    tokens = build_tokens(FULL_LAYOUT)
    top, bottom, found = find_section_bounds(tokens, PAGE_HEIGHT)
    assert found
    assert top == 1071
    assert bottom == 1920


def test_inscripcion_value_does_not_truncate_the_section() -> None:
    """Regression: ``Inmatriculac. : INSCRIPCION`` sits *inside* the section.

    Treating "inscripcion" as a footer keyword cut the section in half and lost PESO_BRUTO,
    NUM_ASIENTOS and NUM_PASAJEROS.
    """
    tokens = build_tokens(FULL_LAYOUT)
    top, bottom, _found = find_section_bounds(tokens, PAGE_HEIGHT)
    pasajeros = [t for t in tokens if t.text.startswith("Pasajer")]
    assert pasajeros, "fixture should contain the last row of column 2"
    assert top <= pasajeros[0].center_y <= bottom


def test_falls_back_to_measured_geometry_without_colon_columns() -> None:
    tokens = [
        Token("Características", 82, 1071, 240, 26, 96.0),
        Token("Placa", 80, 1154, 87, 26, 96.0),
        Token("A0A952", 342, 1154, 127, 26, 96.0),
    ]
    section = detect_section(tokens, PAGE_WIDTH, PAGE_HEIGHT)
    assert section.used_fallback_columns
    assert len(section.columns) == 3


def test_missing_header_falls_back_to_whole_page() -> None:
    tokens = [Token("Placa", 80, 1154, 87, 26, 96.0)]
    top, bottom, found = find_section_bounds(tokens, PAGE_HEIGHT)
    assert not found
    assert (top, bottom) == (0, PAGE_HEIGHT)


def test_band_rows_orders_by_x_within_a_row() -> None:
    """Regression: sorting by raw ``y`` puts a word one pixel higher ahead of its own label.

    On the sample ``Asientos`` (y=1768) sorted before the ``N`` (y=1769) that precedes it.
    """
    tokens = [
        Token("Asientos", 893, 1768, 136, 26, 96.0),
        Token("N°", 847, 1769, 35, 26, 96.0),
        Token("20", 1108, 1770, 38, 26, 96.0),
    ]
    rows = band_rows(tokens, 0.008 * PAGE_HEIGHT)
    assert len(rows) == 1
    assert [t.text for t in rows[0]] == ["N°", "Asientos", "20"]


def test_band_rows_separates_adjacent_rows() -> None:
    tokens = [
        Token("Peso", 847, 1603, 78, 26, 96.0),
        Token("Bruto", 847, 1657, 82, 26, 96.0),
    ]
    rows = band_rows(tokens, 0.008 * PAGE_HEIGHT)
    assert len(rows) == 2


def test_cluster_groups_within_tolerance() -> None:
    assert cluster([80, 82, 83, 842, 845, 1608], 30) == [[80, 82, 83], [842, 845], [1608]]
