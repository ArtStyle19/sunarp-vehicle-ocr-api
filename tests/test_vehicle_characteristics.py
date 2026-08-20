"""Field extraction: the golden regression plus the failure modes that matter."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.models.extraction import FieldStatus, TargetField, WarningCode
from app.services.vehicle_characteristics import extract_fields
from tests.conftest import build_page

# The reference document's layout, reproduced faithfully: three columns, staggered rows, and
# the redacted cells that appear in real boletas.
COLUMN_1 = [
    ("Placa", "A0A952"),
    ("Tipo Uso", "Transporte interprovincial"),
    ("Categoría", "M2"),
    ("Carrocería", "MINIBUS"),
    ("Marca", "MERCEDES BENZ"),
    ("Modelo", "SPRINTER 413CDI/ C4025"),
    ("Año Mod", "2010"),
    ("Año Fab", "2010"),
    ("N° Versión", "SIN VERSION"),
    ("N° Serie", "8AC904663BE038811"),
    ("N° de VIN", "8AC904663BE038811"),
    ("Color 1", "BLANCO"),
    ("Color 2", "##########"),
]
COLUMN_2 = [
    ("Color 3", "##########"),
    ("N° Motor", "61198170115239"),
    ("Tipo Combus", "DIESEL"),
    ("Pot. Motor", "90@3800"),
    ("N° Cilindros", "4"),
    ("Cilindrada", "##########"),
    ("Peso Neto", "2.89"),
    ("Peso Bruto", "4.6"),
    ("Carga Útil", "1.71"),
    ("N° Asientos", "20"),
    ("N° Pasajer.", "19"),
]
COLUMN_3 = [
    ("N° Partida", "52172133"),
    ("N° Ejes", "2"),
    ("N° Ruedas", "6"),
    ("Longitud", "6.99"),
    ("Ancho", "1.99"),
    ("Altura", "2.76"),
    ("Form. Rodan.", "4X2"),
    ("Inmatriculac.", "INSCRIPCION"),
    ("Fec. Prop", "15/07/2015"),
    ("Condición", "VIGENTE"),
]

EXPECTED = {
    TargetField.NUM_CILINDROS: 4,
    TargetField.PESO_NETO: Decimal("2.89"),
    TargetField.PESO_BRUTO: Decimal("4.6"),
    TargetField.NUM_ASIENTOS: 20,
    TargetField.NUM_PASAJEROS: 19,
    TargetField.NUM_EJES: 2,
    TargetField.NUM_RUEDAS: 6,
    TargetField.LONGITUD: Decimal("6.99"),
    TargetField.ANCHO: Decimal("1.99"),
    TargetField.ALTURA: Decimal("2.76"),
    TargetField.NUM_PARTIDA: "52172133",
    TargetField.TIPO_USO: "Transporte interprovincial",
}


def _extract(columns=None, **kwargs):
    source, section = build_page(columns or [COLUMN_1, COLUMN_2, COLUMN_3], **kwargs)
    return extract_fields(source, section)


def test_extracts_every_target_field() -> None:
    outcome = _extract()
    for field, expected in EXPECTED.items():
        result = outcome.fields[field]
        assert result.value == expected, f"{field.value}: {result.value!r} != {expected!r}"
        assert result.status is FieldStatus.EXTRACTED
        assert result.valid is True


def test_returns_exactly_the_twelve_target_fields() -> None:
    """Nothing outside the contract leaks in -- no owner, marca, motor, VIN or colour."""
    outcome = _extract()
    assert set(outcome.fields) == set(TargetField)
    assert len(outcome.fields) == 12
    assert "CILINDRADA" not in {f.value for f in outcome.fields}


def test_unrelated_fields_are_not_extracted() -> None:
    outcome = _extract()
    serialised = {f.value for f in outcome.fields}
    for forbidden in ("MARCA", "MODELO", "COLOR", "NUM_MOTOR", "VIN", "CATEGORIA", "CARROCERIA"):
        assert forbidden not in serialised


@pytest.mark.parametrize(
    ("marker", "expected_field"),
    [
        ("N° Ejes", TargetField.NUM_EJES),
        ("Nº Ejes", TargetField.NUM_EJES),
        ("N Ejes", TargetField.NUM_EJES),
        ("N* Ejes", TargetField.NUM_EJES),
        ("N? Ejes", TargetField.NUM_EJES),
        ("Nro Ejes", TargetField.NUM_EJES),
    ],
)
def test_ordinal_label_variants(marker: str, expected_field: TargetField) -> None:
    column = [(marker, "2"), ("Longitud", "6.99")] + COLUMN_3[3:]
    outcome = _extract([COLUMN_1, COLUMN_2, column])
    assert outcome.fields[expected_field].value == 2


@pytest.mark.parametrize("label", ["N° Pasajer.", "N° Pasajeros", "Nº Pasajeros", "N* Pasajer."])
def test_pasajeros_label_variants(label: str) -> None:
    column = COLUMN_2[:-1] + [(label, "19")]
    outcome = _extract([COLUMN_1, column, COLUMN_3])
    assert outcome.fields[TargetField.NUM_PASAJEROS].value == 19


@pytest.mark.parametrize("label", ["Tipo Uso", "Tipo de Uso"])
def test_tipo_uso_label_variants(label: str) -> None:
    column = [COLUMN_1[0], (label, "Transporte interprovincial")] + COLUMN_1[2:]
    outcome = _extract([column, COLUMN_2, COLUMN_3])
    assert outcome.fields[TargetField.TIPO_USO].value == "Transporte interprovincial"


def test_colon_fused_into_value_is_corrected_geometrically() -> None:
    """Real OCR reads ``N Ejes : 2`` as the single token ``22`` and ``N Ruedas : 6`` as ``16``.

    The fused glyph is identified by position -- the token starts on the colon anchor -- so the
    value recovers to 2 and 6 rather than 22 and 16.
    """
    outcome = _extract(fuse_colon={(2, 1), (2, 2)})
    assert outcome.fields[TargetField.NUM_EJES].value == 2
    assert outcome.fields[TargetField.NUM_RUEDAS].value == 6


def test_value_starting_at_the_value_column_is_never_truncated() -> None:
    """The counterpart to the fusion fix: ``20`` sits in the value column, so both digits stay."""
    outcome = _extract()
    assert outcome.fields[TargetField.NUM_ASIENTOS].value == 20
    assert outcome.fields[TargetField.NUM_PASAJEROS].value == 19


def test_masked_value_is_reported_unavailable_not_guessed() -> None:
    """A redacted cell must never be inferred, derived, or replaced with zero."""
    column = [row if row[0] != "Peso Neto" else ("Peso Neto", "##########") for row in COLUMN_2]
    outcome = _extract([COLUMN_1, column, COLUMN_3], value_confidence={(1, 6): 0.0})
    result = outcome.fields[TargetField.PESO_NETO]
    assert result.status is FieldStatus.UNAVAILABLE
    assert result.value is None
    assert result.valid is None
    assert result.ocr_confidence is None
    # It must not have borrowed a plausible number from anywhere else on the page.
    assert outcome.fields[TargetField.PESO_BRUTO].value == Decimal("4.6")


def test_ocr_garbled_mask_is_still_recognised_as_unavailable() -> None:
    """Under ``-l spa`` the mask comes back as letters, not ``#``."""
    column = [row if row[0] != "Peso Neto" else ("Peso Neto", "AAAAAIAAAAAAE") for row in COLUMN_2]
    outcome = _extract([COLUMN_1, column, COLUMN_3], value_confidence={(1, 6): 0.0})
    assert outcome.fields[TargetField.PESO_NETO].status is FieldStatus.UNAVAILABLE
    assert outcome.fields[TargetField.PESO_NETO].value is None


def test_missing_label_reports_not_found() -> None:
    column = [row for row in COLUMN_3 if row[0] != "Ancho"]
    outcome = _extract([COLUMN_1, COLUMN_2, column])
    result = outcome.fields[TargetField.ANCHO]
    assert result.status is FieldStatus.NOT_FOUND
    assert result.value is None
    assert result.source_label is None
    assert any(
        w.field is TargetField.ANCHO and w.code is WarningCode.FIELD_NOT_FOUND
        for w in outcome.warnings
    )


def test_neighbouring_column_value_is_never_borrowed() -> None:
    """``N Cilindros : 4`` and ``Ancho : 1.99`` share a row band; each keeps its own value."""
    outcome = _extract()
    assert outcome.fields[TargetField.NUM_CILINDROS].value == 4
    assert outcome.fields[TargetField.ANCHO].value == Decimal("1.99")


def test_cilindrada_row_does_not_contaminate_neighbours() -> None:
    """``Cilindrada`` sits between ``N Cilindros`` and ``Peso Neto`` and is masked in the sample."""
    outcome = _extract(value_confidence={(1, 0): 0.0, (1, 5): 0.0})
    assert outcome.fields[TargetField.NUM_CILINDROS].value == 4
    assert outcome.fields[TargetField.PESO_NETO].value == Decimal("2.89")


def test_implausible_value_is_flagged_but_not_repaired() -> None:
    column = [row if row[0] != "N° Ejes" else ("N° Ejes", "0") for row in COLUMN_3]
    outcome = _extract([COLUMN_1, COLUMN_2, column])
    result = outcome.fields[TargetField.NUM_EJES]
    assert result.status is FieldStatus.EXTRACTED
    assert result.value == 0, "the value is reported exactly as read, never corrected"
    assert result.valid is False
    assert any(
        w.field is TargetField.NUM_EJES and w.code is WarningCode.VALIDATION_FAILED
        for w in outcome.warnings
    )


def test_decimal_comma_is_normalised() -> None:
    column = [row if row[0] != "Peso Neto" else ("Peso Neto", "2,89") for row in COLUMN_2]
    outcome = _extract([COLUMN_1, column, COLUMN_3])
    assert outcome.fields[TargetField.PESO_NETO].value == Decimal("2.89")


def test_num_partida_stays_a_string() -> None:
    outcome = _extract()
    value = outcome.fields[TargetField.NUM_PARTIDA].value
    assert isinstance(value, str)
    assert value == "52172133"


def test_plate_is_read_for_cross_checking_only() -> None:
    outcome = _extract()
    assert outcome.plate_in_document == "A0A952"
    assert "PLACA" not in {f.value for f in outcome.fields}


def test_weight_cross_check_warns_without_changing_values() -> None:
    column = [row if row[0] != "Peso Bruto" else ("Peso Bruto", "1.5") for row in COLUMN_2]
    outcome = _extract([COLUMN_1, column, COLUMN_3])
    assert outcome.fields[TargetField.PESO_BRUTO].value == Decimal("1.5")
    assert outcome.fields[TargetField.PESO_NETO].value == Decimal("2.89")
    assert any("PESO_BRUTO is lower" in w.message for w in outcome.warnings)


class TestOverlappingLabelRecovery:
    """Regressions from real documents where a long ``Color 1`` value is printed over the
    next column's ``N° Asientos`` label and OCR fuses the two into one token."""

    @pytest.mark.parametrize(
        ("fused", "expect_fuzzy_warning"),
        [
            # A0L952: the marker was destroyed but the keyword survived intact.
            ("Asientos", False),
            # A1E965: the keyword survived as the token's suffix.
            ("ARUAsientos", False),
            # A0W951: the keyword itself was corrupted (s -> B) but is still recognisable.
            ("AMARWNiABientos", True),
        ],
    )
    def test_fused_label_is_recovered(self, fused: str, expect_fuzzy_warning: bool) -> None:
        column = [row if row[0] != "N° Asientos" else (row[0], "20") for row in COLUMN_2]
        outcome = _extract(
            [COLUMN_1, column, COLUMN_3],
            overlap_labels={(1, COLUMN_2.index(("N° Asientos", "20"))): fused},
        )
        result = outcome.fields[TargetField.NUM_ASIENTOS]
        assert result.status is FieldStatus.EXTRACTED
        assert result.value == 20
        fuzzy = [w for w in outcome.warnings if w.code is WarningCode.LABEL_FUZZY_MATCH]
        assert bool(fuzzy) is expect_fuzzy_warning

    def test_destroyed_label_is_not_guessed(self) -> None:
        """A0R951: the label is physically illegible in the source PDF.

        It must come back ``not_found`` rather than being inferred from position or derived
        from ``N° Pasajer.``.
        """
        outcome = _extract(
            [COLUMN_1, COLUMN_2, COLUMN_3],
            overlap_labels={(1, COLUMN_2.index(("N° Asientos", "20"))): "ESMERALDANANMétitdsO"},
        )
        result = outcome.fields[TargetField.NUM_ASIENTOS]
        assert result.status is FieldStatus.NOT_FOUND
        assert result.value is None
        # The neighbouring passenger count must not have leaked into it.
        assert outcome.fields[TargetField.NUM_PASAJEROS].value == 19

    def test_degraded_match_never_preempts_an_exact_one(self) -> None:
        """Every other field still matches exactly and keeps its own value."""
        outcome = _extract(
            [COLUMN_1, COLUMN_2, COLUMN_3],
            overlap_labels={(1, COLUMN_2.index(("N° Asientos", "20"))): "ARUAsientos"},
        )
        for field, expected in EXPECTED.items():
            assert outcome.fields[field].value == expected, field


class TestPassDisagreement:
    """When the two independent reads differ, the plausible high-confidence one must win."""

    def _extract_with_crop(
        self, label: str, page_value: str, page_conf: float, crop: tuple[str, float]
    ):
        from tests.conftest import row_y

        index = [r[0] for r in COLUMN_3].index(label)
        column = list(COLUMN_3)
        column[index] = (label, page_value)
        source, section = build_page(
            [COLUMN_1, COLUMN_2, column],
            crop_overrides={row_y(index): crop},
            value_confidence={(2, index): page_conf},
        )
        return extract_fields(source, section)

    def test_dropped_decimal_point_is_rejected_in_favour_of_the_plausible_read(self) -> None:
        """A0W951/A0L952: the page pass read ``2.3`` as ``23`` at confidence 38, while the
        isolated cell read ``2.3`` at confidence 95. Only ``2.3`` is a plausible width."""
        outcome = self._extract_with_crop("Ancho", "23", 38.0, ("2.3", 95.0))
        result = outcome.fields[TargetField.ANCHO]
        assert result.value == Decimal("2.3")
        assert result.valid is True
        assert any(
            w.field is TargetField.ANCHO and w.code is WarningCode.LOW_CONFIDENCE_FIELD
            for w in outcome.warnings
        ), "a disagreement must still be surfaced, even when resolved"

    def test_higher_confidence_wins_when_both_readings_are_plausible(self) -> None:
        """A1B738: the crop clipped the last syllable of TIPO_USO; the page pass was right."""
        from tests.conftest import row_y

        index = [r[0] for r in COLUMN_1].index("Tipo Uso")
        source, section = build_page(
            [COLUMN_1, COLUMN_2, COLUMN_3],
            crop_overrides={row_y(index): ("Transporte interprovincia", 80.0)},
        )
        outcome = extract_fields(source, section)
        assert outcome.fields[TargetField.TIPO_USO].value == "Transporte interprovincial"

    def test_neither_reading_is_averaged_or_invented(self) -> None:
        """A1Q954: ``124`` from the page pass vs ``12.4`` from the cell. The answer must be one
        of those two, never a blend of them."""
        outcome = self._extract_with_crop("Longitud", "124", 51.0, ("12.4", 95.0))
        value = outcome.fields[TargetField.LONGITUD].value
        assert value == Decimal("12.4")
        assert value in (Decimal("12.4"), Decimal("124")), "must be one of the two real readings"
