"""Extract the 12 target fields from the 'Caracteristicas del Vehiculo' section.

Design constraints that shaped this module, all observed on real output rather than assumed:

* **Colon fusion.** In full-page OCR the colon glyph merges into the value token: ``N Ejes``
  reads as ``22`` (true value 2) and ``N Ruedas`` as ``16`` (true 6). Text alone cannot separate
  ``22`` -> 2 from ``20`` -> 20. Geometry can: a token whose left edge sits *on* the column's
  colon anchor begins with the colon; one starting at the value column does not.
* **Dual pass.** Every value is read twice -- once from the page-level token grid, once by
  re-reading the isolated cell. Agreement is the signal that a value is trustworthy;
  disagreement downgrades it and raises a warning. Neither pass is ever averaged or repaired.
* **Masked values.** A redacted cell must come back ``unavailable``. It is never inferred,
  derived from another field, or replaced with zero.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dataclass_field
from decimal import Decimal
from difflib import SequenceMatcher
from typing import Literal

from app.models.extraction import (
    ExtractionWarning,
    FieldResult,
    FieldStatus,
    TargetField,
    WarningCode,
)
from app.services.section_detector import (
    CELL_LEFT_INSET_W,
    COLON_ANCHOR_TOL_W,
    ROW_BAND_TOL_H,
    Column,
    Section,
    band_rows,
)
from app.services.tokens import PageSource, Token
from app.utils.normalization import (
    looks_masked,
    matches_ordinal_marker,
    normalize_label_token,
    parse_decimal,
    parse_int,
    strip_leading_separator,
)

#: Sentinel meaning "the N of a numbered label" (N, N., Nro, and the OCR corruptions N?, N*).
ORDINAL = "#N"

#: Vertical padding around a value crop, as a fraction of the label's height. Cropping tightly
#: clips the baseline and turns ``4.6`` into ``46``.
CELL_VERTICAL_PAD = 0.55

#: Degraded label matching, used only after exact matching fails. Some boletas print a long
#: ``Color 1`` value that physically overlaps the next column's label, and OCR fuses them into one
#: unreadable token (observed: ``ARUAsientos``, ``AMARWNiABientos`` for ``N° Asientos``).
#: Thresholds measured on real output: genuine fusions score 0.875-1.000 against the expected
#: word, while every unrelated label scores at most 0.500.
LABEL_FUZZY_MIN_RATIO = 0.85
#: Short words are excluded -- "ejes" or "ancho" are too small for a similarity score to be safe.
LABEL_FUZZY_MIN_WORD_LEN = 6

MatchQuality = Literal["exact", "keyword", "fuzzy"]

ValueKind = Literal["int", "decimal", "string"]


@dataclass(frozen=True, slots=True)
class FieldSpec:
    field: TargetField
    #: Alternative label token sequences. Matching is prefix-based per token and deliberately
    #: narrow -- broad fuzzy matching risks binding a value to the wrong label.
    label_patterns: tuple[tuple[str, ...], ...]
    canonical_label: str
    kind: ValueKind
    minimum: Decimal | int | None = None
    maximum: Decimal | int | None = None
    max_length: int | None = None


FIELD_SPECS: tuple[FieldSpec, ...] = (
    FieldSpec(TargetField.NUM_CILINDROS, ((ORDINAL, "cilindros"),), "N° Cilindros", "int", 1, 24),
    FieldSpec(
        TargetField.PESO_NETO,
        (("peso", "neto"),),
        "Peso Neto",
        "decimal",
        Decimal("0.001"),
        Decimal("200"),
    ),
    FieldSpec(
        TargetField.PESO_BRUTO,
        (("peso", "bruto"),),
        "Peso Bruto",
        "decimal",
        Decimal("0.001"),
        Decimal("200"),
    ),
    FieldSpec(TargetField.NUM_ASIENTOS, ((ORDINAL, "asientos"),), "N° Asientos", "int", 1, 120),
    FieldSpec(TargetField.NUM_PASAJEROS, ((ORDINAL, "pasajer"),), "N° Pasajer.", "int", 0, 120),
    FieldSpec(TargetField.NUM_EJES, ((ORDINAL, "ejes"),), "N° Ejes", "int", 1, 12),
    FieldSpec(TargetField.NUM_RUEDAS, ((ORDINAL, "ruedas"),), "N° Ruedas", "int", 1, 40),
    FieldSpec(
        TargetField.LONGITUD, (("longitud",),), "Longitud", "decimal", Decimal("0.001"), Decimal("40")
    ),
    FieldSpec(TargetField.ANCHO, (("ancho",),), "Ancho", "decimal", Decimal("0.001"), Decimal("10")),
    FieldSpec(TargetField.ALTURA, (("altura",),), "Altura", "decimal", Decimal("0.001"), Decimal("10")),
    FieldSpec(TargetField.NUM_PARTIDA, ((ORDINAL, "partida"),), "N° Partida", "string", max_length=32),
    FieldSpec(
        TargetField.TIPO_USO,
        (("tipo", "uso"), ("tipo", "de", "uso")),
        "Tipo Uso",
        "string",
        max_length=120,
    ),
)

#: Read only to cross-check the requested plate. Never returned as an extracted field.
_PLACA_PATTERN = ("placa",)


@dataclass
class ExtractionOutcome:
    fields: dict[TargetField, FieldResult]
    warnings: list[ExtractionWarning] = dataclass_field(default_factory=list)
    plate_in_document: str | None = None
    section_text: str = ""
    matched_labels: dict[TargetField, str] = dataclass_field(default_factory=dict)

    @property
    def values(self) -> dict[TargetField, Decimal | int | str | None]:
        return {name: result.value for name, result in self.fields.items()}

    @property
    def found_count(self) -> int:
        return sum(1 for r in self.fields.values() if r.status is FieldStatus.EXTRACTED)


def _token_matches(token: Token, pattern: str) -> bool:
    if pattern == ORDINAL:
        return matches_ordinal_marker(token.text)
    return normalize_label_token(token.text).startswith(pattern)


def _find_label(
    row: list[Token], patterns: tuple[tuple[str, ...], ...], quality: MatchQuality = "exact"
) -> list[Token] | None:
    """Find the tokens in ``row`` that form this field's label.

    Three strategies, applied in separate passes so a degraded match in the wrong row can never
    pre-empt an exact match in the right one:

    ``exact``
        A consecutive run matching the full pattern. This is the only strategy used on clean
        documents.
    ``keyword``
        The pattern's most distinctive word alone. Recovers a label whose ``N°`` marker was
        destroyed by overlapping text but whose keyword survived intact.
    ``fuzzy``
        The distinctive word matched against the *suffix* of a token, above
        :data:`LABEL_FUZZY_MIN_RATIO`. Recovers a keyword fused into a neighbouring word.
    """
    if quality == "exact":
        for pattern in patterns:
            span = len(pattern)
            for start in range(len(row) - span + 1):
                candidate = row[start : start + span]
                if all(_token_matches(t, p) for t, p in zip(candidate, pattern, strict=True)):
                    return candidate
        return None

    keywords = {pattern[-1] for pattern in patterns if pattern[-1] != ORDINAL}
    if quality == "keyword":
        for token in row:
            normalized = normalize_label_token(token.text)
            for keyword in keywords:
                # A keyword absorbed into the tail of a neighbouring word (``ARUAsientos``) is
                # still an exact match, not an approximate one. Suffix matching is restricted to
                # long keywords, where an accidental ending is implausible.
                if normalized.startswith(keyword) or (
                    len(keyword) >= LABEL_FUZZY_MIN_WORD_LEN and normalized.endswith(keyword)
                ):
                    return [token]
        return None

    best: tuple[float, Token] | None = None
    for keyword in keywords:
        if len(keyword) < LABEL_FUZZY_MIN_WORD_LEN:
            continue
        for token in row:
            normalized = normalize_label_token(token.text)
            if len(normalized) < len(keyword):
                continue
            ratio = SequenceMatcher(None, normalized[-len(keyword) :], keyword).ratio()
            if ratio >= LABEL_FUZZY_MIN_RATIO and (best is None or ratio > best[0]):
                best = (ratio, token)
    return [best[1]] if best else None


def _read_tokens_pass(
    row: list[Token], column: Column, anchor_tol: int
) -> tuple[str, float | None]:
    """Read the value from the page token grid, undoing colon/value glyph fusion."""
    value_tokens = [t for t in row if t.x >= column.colon_x - anchor_tol and t.x < column.right]
    if not value_tokens:
        return "", None

    raw = " ".join(t.text for t in value_tokens).strip()
    cleaned = strip_leading_separator(raw)
    if cleaned == raw and abs(value_tokens[0].x - column.colon_x) <= anchor_tol and len(raw) > 1:
        # No separator character survived OCR, yet the first token starts exactly on the colon
        # column -- so its first glyph *is* the colon, misrecognised as a digit or letter.
        cleaned = strip_leading_separator(raw[1:])

    confidences = [t.confidence for t in value_tokens if t.confidence is not None]
    mean_confidence = sum(confidences) / len(confidences) if confidences else None
    return cleaned.strip(), mean_confidence


def _read_crop_pass(
    source: PageSource, label_tokens: list[Token], column: Column
) -> tuple[str, float | None]:
    """Re-read the value by OCR'ing only its own cell."""
    top = min(t.y for t in label_tokens)
    bottom = max(t.bottom for t in label_tokens)
    pad = max(6, int((bottom - top) * CELL_VERTICAL_PAD))
    box = (
        column.colon_x + int(CELL_LEFT_INSET_W * source.width),
        max(0, top - pad),
        min(source.width, column.right),
        min(source.height, bottom + pad),
    )
    text, confidence = source.read_cell(box)
    return strip_leading_separator(text).strip(), confidence


def _parse(text: str, kind: ValueKind) -> Decimal | int | str | None:
    if not text:
        return None
    if kind == "int":
        return parse_int(text)
    if kind == "decimal":
        return parse_decimal(text)
    collapsed = " ".join(text.split())
    return collapsed or None


def _comparable(value: Decimal | int | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return " ".join(value.split()).casefold()
    return str(value)


def _validate(spec: FieldSpec, value: Decimal | int | str) -> bool:
    if spec.kind == "string":
        text = str(value)
        if spec.max_length is not None and len(text) > spec.max_length:
            return False
        if spec.field is TargetField.NUM_PARTIDA:
            compact = text.replace(" ", "")
            return compact.isalnum() and any(ch.isdigit() for ch in compact) and len(compact) >= 4
        return len(text.strip()) >= 2
    numeric = Decimal(value) if isinstance(value, int) else value
    if spec.minimum is not None and numeric < Decimal(spec.minimum):
        return False
    return not (spec.maximum is not None and numeric > Decimal(spec.maximum))


def _resolve(
    spec: FieldSpec,
    token_text: str,
    token_conf: float | None,
    crop_text: str,
    crop_conf: float | None,
) -> tuple[FieldResult, list[WarningCode]]:
    """Combine the two independent reads into one field result."""
    warnings: list[WarningCode] = []
    token_value = _parse(token_text, spec.kind)
    crop_value = _parse(crop_text, spec.kind)

    both_parsed = token_value is not None and crop_value is not None
    if both_parsed and _comparable(token_value) == _comparable(crop_value):
        confidences = [c for c in (token_conf, crop_conf) if c is not None]
        result_conf = max(confidences) if confidences else None
        chosen: Decimal | int | str = token_value
    elif both_parsed:
        # The two passes disagree. Rank the readings by plausibility first, then by real OCR
        # confidence -- never average, round, or otherwise reconcile them into a third value.
        # This is what recovers a dropped decimal point: the page-level pass reads ``2.3`` as
        # ``23`` at confidence 38, while the isolated cell reads ``2.3`` at confidence 95, and
        # only ``2.3`` is a plausible width.
        ranked = sorted(
            ((token_value, token_conf), (crop_value, crop_conf)),
            key=lambda c: (_validate(spec, c[0]), c[1] if c[1] is not None else -1.0),
            reverse=True,
        )
        chosen, result_conf = ranked[0]
        warnings.append(WarningCode.LOW_CONFIDENCE_FIELD)
    elif token_value is not None or crop_value is not None:
        # Only one pass produced something parseable; take it as-is.
        chosen = token_value if token_value is not None else crop_value  # type: ignore[assignment]
        result_conf = token_conf if token_value is not None else crop_conf
    else:
        # Nothing parsed. Distinguish "redacted" from "absent" -- never substitute a number.
        if looks_masked(token_text, token_conf) or looks_masked(crop_text, crop_conf):
            return (
                FieldResult(
                    value=None,
                    status=FieldStatus.UNAVAILABLE,
                    ocr_confidence=None,
                    valid=None,
                    source_label=spec.canonical_label,
                ),
                [WarningCode.FIELD_UNAVAILABLE],
            )
        has_text = bool(token_text or crop_text)
        code = WarningCode.UNPARSEABLE_VALUE if has_text else WarningCode.FIELD_NOT_FOUND
        return (
            FieldResult(
                value=None,
                status=FieldStatus.NOT_FOUND,
                ocr_confidence=None,
                valid=None,
                source_label=spec.canonical_label,
            ),
            [code],
        )

    valid = _validate(spec, chosen)
    if not valid:
        warnings.append(WarningCode.VALIDATION_FAILED)
    return (
        FieldResult(
            value=chosen,
            status=FieldStatus.EXTRACTED,
            ocr_confidence=result_conf,
            valid=valid,
            source_label=spec.canonical_label,
        ),
        warnings,
    )


def _column_rows(
    section_tokens: list[Token], column: Column, page_width: int, page_height: int
) -> list[list[Token]]:
    inset = int(0.01 * page_width)
    # Membership is decided by the token's *right* edge, not its left. A long ``Color 1`` value in
    # the previous column can physically overlap this column's label and OCR fuses the two into a
    # single token that starts well to the left; judging by the left edge would drop it entirely.
    # Values that merely end before this column starts are still excluded.
    column_tokens = [
        t for t in section_tokens if t.right >= column.label_left - inset and t.x < column.right
    ]
    return band_rows(column_tokens, ROW_BAND_TOL_H * page_height)


def _find_plate(
    section_tokens: list[Token], section: Section, page_width: int, page_height: int
) -> str | None:
    anchor_tol = int(COLON_ANCHOR_TOL_W * page_width)
    for column in section.columns:
        for row in _column_rows(section_tokens, column, page_width, page_height):
            label_tokens = [
                t for t in row if t.right <= column.colon_x + anchor_tol and normalize_label_token(t.text)
            ]
            if _find_label(label_tokens, (_PLACA_PATTERN,)) is None:
                continue
            text, _ = _read_tokens_pass(row, column, anchor_tol)
            return text or None
    return None


def extract_fields(source: PageSource, section: Section) -> ExtractionOutcome:
    """Extract every target field from the located section."""
    page_width, page_height = source.width, source.height
    anchor_tol = int(COLON_ANCHOR_TOL_W * page_width)
    all_tokens = source.tokens()
    section_tokens = [t for t in all_tokens if section.top <= t.center_y <= section.bottom] or all_tokens

    results: dict[TargetField, FieldResult] = {}
    warnings: list[ExtractionWarning] = []
    matched_labels: dict[TargetField, str] = {}

    # Pre-band each column once; every field then searches only inside its own column, so a
    # neighbouring column's value can never be mistaken for this field's.
    column_rows = {
        column.index: _column_rows(section_tokens, column, page_width, page_height)
        for column in section.columns
    }

    for spec in FIELD_SPECS:
        located: tuple[Column, list[Token], list[Token], MatchQuality] | None = None
        for quality in ("exact", "keyword", "fuzzy"):
            for column in section.columns:
                for row in column_rows[column.index]:
                    label_tokens = [
                        t
                        for t in row
                        if t.right <= column.colon_x + anchor_tol and normalize_label_token(t.text)
                    ]
                    match = _find_label(label_tokens, spec.label_patterns, quality)
                    if match is not None:
                        located = (column, row, match, quality)
                        break
                if located is not None:
                    break
            if located is not None:
                break

        if located is None:
            results[spec.field] = FieldResult(
                value=None, status=FieldStatus.NOT_FOUND, source_label=None
            )
            warnings.append(
                ExtractionWarning(
                    code=WarningCode.FIELD_NOT_FOUND,
                    message=f"Label for {spec.field.value} was not found in the section.",
                    field=spec.field,
                )
            )
            continue

        column, row, label_tokens, quality = located
        matched_labels[spec.field] = " ".join(t.text for t in label_tokens)
        if quality == "fuzzy":
            warnings.append(
                ExtractionWarning(
                    code=WarningCode.LABEL_FUZZY_MATCH,
                    message=(
                        f"The label for {spec.field.value} was overlapped by neighbouring text and "
                        f"recovered by approximate matching (read as "
                        f"{matched_labels[spec.field]!r})."
                    ),
                    field=spec.field,
                )
            )
        token_text, token_conf = _read_tokens_pass(row, column, anchor_tol)
        crop_text, crop_conf = _read_crop_pass(source, label_tokens, column)

        result, codes = _resolve(spec, token_text, token_conf, crop_text, crop_conf)
        results[spec.field] = result
        for code in codes:
            warnings.append(
                ExtractionWarning(code=code, message=_warning_message(code, spec), field=spec.field)
            )

    _cross_check(results, warnings)

    section_text = " ".join(t.text for t in sorted(section_tokens, key=lambda t: (t.center_y, t.x)))
    return ExtractionOutcome(
        fields=results,
        warnings=warnings,
        plate_in_document=_find_plate(section_tokens, section, page_width, page_height),
        section_text=section_text,
        matched_labels=matched_labels,
    )


def _warning_message(code: WarningCode, spec: FieldSpec) -> str:
    name = spec.field.value
    return {
        WarningCode.FIELD_NOT_FOUND: f"No value could be located for {name}.",
        WarningCode.FIELD_UNAVAILABLE: f"{name} is redacted in the document and was not inferred.",
        WarningCode.UNPARSEABLE_VALUE: (
            f"The value read for {name} could not be parsed and was not guessed."
        ),
        WarningCode.VALIDATION_FAILED: (
            f"{name} failed its plausibility check and was returned unmodified."
        ),
        WarningCode.LOW_CONFIDENCE_FIELD: f"The two independent reads of {name} disagreed.",
    }.get(code, f"{name}: {code.value}")


def _cross_check(
    results: dict[TargetField, FieldResult], warnings: list[ExtractionWarning]
) -> None:
    """Flag physically implausible combinations. Values are reported, never corrected."""
    neto = results.get(TargetField.PESO_NETO)
    bruto = results.get(TargetField.PESO_BRUTO)
    if (
        neto is not None
        and bruto is not None
        and isinstance(neto.value, Decimal)
        and isinstance(bruto.value, Decimal)
        and bruto.value < neto.value
    ):
        warnings.append(
            ExtractionWarning(
                code=WarningCode.VALIDATION_FAILED,
                message="PESO_BRUTO is lower than PESO_NETO; both values are reported as read.",
                field=TargetField.PESO_BRUTO,
            )
        )
