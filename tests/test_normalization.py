"""Normalisation rules -- especially the ones that must NOT be over-eager."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.utils.normalization import (
    looks_masked,
    matches_ordinal_marker,
    normalize_label_token,
    normalize_plate,
    parse_decimal,
    parse_int,
    plate_confusables,
    strip_leading_separator,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("A0A952", "A0A952"), ("2ZR-315", "2ZR315"), ("2ZR315", "2ZR315"), (" a0a-952 ", "A0A952")],
)
def test_normalize_plate(raw: str, expected: str) -> None:
    assert normalize_plate(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"), [("2.89", "2.89"), ("2,89", "2.89"), ("4.6", "4.6"), ("20", "20")]
)
def test_parse_decimal_keeps_scale(raw: str, expected: str) -> None:
    """``2.89`` must stay ``2.89`` -- never collapse to ``289``."""
    assert parse_decimal(raw) == Decimal(expected)


@pytest.mark.parametrize("raw", ["", "abc", "1.2.3", "1,234.5", "##########", "AAAAAIAAAAAAE", "4-6"])
def test_parse_decimal_refuses_ambiguous_input(raw: str) -> None:
    assert parse_decimal(raw) is None


@pytest.mark.parametrize(("raw", "expected"), [("4", 4), ("20", 20), (" 19 ", 19), (":6", 6)])
def test_parse_int(raw: str, expected: int) -> None:
    assert parse_int(raw) == expected


@pytest.mark.parametrize("raw", ["2.5", "", "N/A", "##"])
def test_parse_int_refuses_non_integers(raw: str) -> None:
    """A decimal is rejected outright rather than silently truncated."""
    assert parse_int(raw) is None


def test_normalize_label_token_preserves_the_letter_o() -> None:
    """Regression: stripping the degree sign with a class containing a plain ``o`` would
    turn ``Ancho`` into ``anch`` and break every label containing the letter."""
    assert normalize_label_token("Ancho") == "ancho"
    assert normalize_label_token("Tipo") == "tipo"
    assert normalize_label_token("Uso") == "uso"
    assert normalize_label_token("Longitud") == "longitud"
    assert normalize_label_token("Categoría") == "categoria"


@pytest.mark.parametrize("marker", ["N°", "Nº", "N", "N.", "Nro", "No", "N*", "N?", "N?*"])
def test_ordinal_marker_variants(marker: str) -> None:
    assert matches_ordinal_marker(marker)


@pytest.mark.parametrize("word", ["Neto", "Nro.Serie", "Numero", "Peso", "Ejes"])
def test_ordinal_marker_rejects_real_words(word: str) -> None:
    assert not matches_ordinal_marker(word)


@pytest.mark.parametrize("raw", [":4.6", " :4.6", "; 20", "|2.76", "— 4X2", ". 6"])
def test_strip_leading_separator(raw: str) -> None:
    assert not strip_leading_separator(raw).startswith((":", ";", "|", "—", "."))


@pytest.mark.parametrize(
    "text",
    ["##########", "********", "AAAAAIAAAAAAE", "HHAHHAHAAEE", "ad ddidaididid", "PARAR AAA RAR"],
)
def test_looks_masked_detects_redaction(text: str) -> None:
    """The mask does not survive OCR as ``#``; detection must not rely on that character."""
    assert looks_masked(text, 0.0)


@pytest.mark.parametrize(
    ("text", "confidence"),
    [
        ("Transporte interprovincial", 96.0),
        ("INSCRIPCION", 96.0),
        ("MERCEDES BENZ", 96.0),
        ("DIESEL", 95.0),
        ("MINIBUS", 90.0),
        ("VIGENTE", 96.0),
        ("52172133", 96.0),
        ("2.89", 96.0),
    ],
)
def test_looks_masked_leaves_real_values_alone(text: str, confidence: float) -> None:
    assert not looks_masked(text, confidence)


def test_plate_confusables_folds_ocr_lookalikes() -> None:
    """The sample's ``A0A952`` is read as ``AOA952``; that must not raise a mismatch."""
    assert plate_confusables("AOA952") == plate_confusables("A0A952")
    assert plate_confusables("2ZR315") != plate_confusables("2XY315")


@pytest.mark.parametrize(
    ("read", "requested"),
    [
        ("AOA952", "A0A952"),   # O/0 confusion
        ("A0OL952", "A0L952"),  # OCR inserted a phantom character
        ("AO0Z967", "A0Z967"),
        ("A0A952", "A0A952"),
    ],
)
def test_plates_match_absorbs_ocr_noise(read: str, requested: str) -> None:
    from app.utils.normalization import plates_match

    assert plates_match(read, requested)


@pytest.mark.parametrize(
    ("read", "requested"),
    [
        ("A1B738", "A1B759"),  # genuinely different, and only two characters apart
        ("A0W951", "A0W960"),
        ("A1Q954", "A0L952"),
        ("A1D770", "A1D964"),
    ],
)
def test_plates_match_still_catches_a_different_vehicle(read: str, requested: str) -> None:
    """The tolerance must not be so loose that a wrong Drive id goes unnoticed."""
    from app.utils.normalization import plates_match

    assert not plates_match(read, requested)
