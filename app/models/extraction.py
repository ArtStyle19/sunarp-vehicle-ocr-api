"""Request/response models and the definition of the target field set."""

from __future__ import annotations

import re
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    field_serializer,
    field_validator,
    model_serializer,
)

from app.utils.normalization import normalize_plate

DRIVE_FILE_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{10,256}$")


class TargetField(StrEnum):
    """The fields this service extracts -- and nothing else.

    ``CILINDRADA`` is deliberately absent: it is redacted (``##########``) in effectively every
    document, so it is not part of the contract. The generic mask detection that protected it
    still guards every field below.
    """

    NUM_CILINDROS = "NUM_CILINDROS"
    PESO_NETO = "PESO_NETO"
    PESO_BRUTO = "PESO_BRUTO"
    NUM_ASIENTOS = "NUM_ASIENTOS"
    NUM_PASAJEROS = "NUM_PASAJEROS"
    NUM_EJES = "NUM_EJES"
    NUM_RUEDAS = "NUM_RUEDAS"
    LONGITUD = "LONGITUD"
    ANCHO = "ANCHO"
    ALTURA = "ALTURA"
    NUM_PARTIDA = "NUM_PARTIDA"
    TIPO_USO = "TIPO_USO"


class FieldStatus(StrEnum):
    EXTRACTED = "extracted"
    #: The value exists in the document but is redacted/unreadable. Never guessed.
    UNAVAILABLE = "unavailable"
    NOT_FOUND = "not_found"


class WarningCode(StrEnum):
    PLATE_MISMATCH = "PLATE_MISMATCH"
    FIELD_NOT_FOUND = "FIELD_NOT_FOUND"
    FIELD_UNAVAILABLE = "FIELD_UNAVAILABLE"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    UNPARSEABLE_VALUE = "UNPARSEABLE_VALUE"
    LOW_CONFIDENCE_FIELD = "LOW_CONFIDENCE_FIELD"
    #: The label was recovered by degraded matching because neighbouring text overlapped it.
    LABEL_FUZZY_MATCH = "LABEL_FUZZY_MATCH"
    SECTION_FALLBACK_USED = "SECTION_FALLBACK_USED"


def to_json_number(value: Decimal) -> int | float:
    """Render a Decimal as a JSON number, keeping ``4.6`` as ``4.6``."""
    return int(value) if value == value.to_integral_value() else float(value)


class ExtractionWarning(BaseModel):
    code: WarningCode
    message: str
    field: TargetField | None = None


class FieldResult(BaseModel):
    """Per-field outcome. ``ocr_confidence`` is only ever a real Tesseract number."""

    model_config = ConfigDict(use_enum_values=False)

    value: Decimal | int | str | None = None
    status: FieldStatus = FieldStatus.NOT_FOUND
    ocr_confidence: float | None = None
    valid: bool | None = None
    source_label: str | None = None

    @field_serializer("value")
    def _serialize_value(self, value: Decimal | int | str | None) -> int | float | str | None:
        return to_json_number(value) if isinstance(value, Decimal) else value


class ExtractRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plate: Annotated[str, Field(min_length=1, max_length=32)]
    drive_file_id: Annotated[str, Field(min_length=10, max_length=256)]

    @field_validator("plate")
    @classmethod
    def _plate_has_content(cls, value: str) -> str:
        if not normalize_plate(value):
            raise ValueError("plate must contain at least one alphanumeric character")
        return value

    @field_validator("drive_file_id")
    @classmethod
    def _valid_drive_id(cls, value: str) -> str:
        if not DRIVE_FILE_ID_RE.match(value):
            raise ValueError("drive_file_id is not a valid Google Drive file id")
        return value


class RequestEcho(BaseModel):
    plate: str
    normalized_plate: str
    drive_file_id: str


class DocumentInfo(BaseModel):
    filename: str
    mime_type: str
    pages: int


class ProcessingInfo(BaseModel):
    native_text_available: bool
    ocr_used: bool
    target_section_found: bool
    render_scale: float | None = None
    processing_time_ms: int | None = None


class DebugInfo(BaseModel):
    """Only populated when ``RETURN_DEBUG_DATA=true``. Off in production."""

    raw_text: str | None = None
    target_section_text: str | None = None
    ocr_tokens: list[dict] = Field(default_factory=list)
    column_geometry: list[dict] = Field(default_factory=list)


class ExtractResponse(BaseModel):
    status: Literal["success"] = "success"
    request: RequestEcho
    document: DocumentInfo
    processing: ProcessingInfo
    fields: dict[TargetField, FieldResult]
    #: Flat convenience mirror of ``fields[*].value`` so n8n can map Sheets columns directly.
    values: dict[TargetField, Decimal | int | str | None]
    warnings: list[ExtractionWarning] = Field(default_factory=list)
    debug: DebugInfo | None = None

    @field_serializer("values")
    def _serialize_values(
        self, values: dict[TargetField, Decimal | int | str | None]
    ) -> dict[str, int | float | str | None]:
        return {
            key.value: (to_json_number(val) if isinstance(val, Decimal) else val)
            for key, val in values.items()
        }

    @model_serializer(mode="wrap")
    def _drop_empty_debug(self, handler: SerializerFunctionWrapHandler) -> dict:
        """Omit ``debug`` entirely unless RETURN_DEBUG_DATA populated it.

        Only this key is dropped: a ``null`` field *value* is meaningful and must survive.
        """
        data = handler(self)
        if data.get("debug") is None:
            data.pop("debug", None)
        return data


class ErrorBody(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    status: Literal["error"] = "error"
    error: ErrorBody
    request_id: str | None = None
