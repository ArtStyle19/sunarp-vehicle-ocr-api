"""The single extraction endpoint n8n calls."""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from functools import lru_cache

from fastapi import APIRouter, Depends, Request
from starlette.concurrency import run_in_threadpool

from app.config import Settings, get_settings
from app.core.exceptions import (
    AppError,
    DocumentProcessingError,
    FileTooLargeError,
    InternalError,
    TargetSectionNotFoundError,
)
from app.core.logging import get_logger, log_event, log_exception, request_id_var
from app.core.security import require_api_key
from app.models.extraction import (
    DebugInfo,
    DocumentInfo,
    ExtractionWarning,
    ExtractRequest,
    ExtractResponse,
    ProcessingInfo,
    RequestEcho,
    WarningCode,
)
from app.services.drive import DriveClient, GoogleDriveClient, ensure_pdf
from app.services.pdf import analyze_document, open_document
from app.services.vehicle_characteristics import ExtractionOutcome, extract_fields
from app.utils.normalization import normalize_plate, plates_match

router = APIRouter(prefix="/api/v1/vehicles", tags=["vehicles"])
logger = get_logger(__name__)


class _LazyDriveClient:
    """Builds the real Drive client on first use.

    Constructing it eagerly during dependency resolution would run credential discovery before
    the request body is even validated, turning a malformed payload into a credentials error.
    """

    def __init__(self) -> None:
        self._client: DriveClient | None = None

    def _resolve(self) -> DriveClient:
        if self._client is None:
            try:
                self._client = GoogleDriveClient(get_settings())
            except Exception as exc:
                raise InternalError(
                    "Google Drive credentials are not configured. See GET /ready for details."
                ) from exc
        return self._client

    def get_metadata(self, file_id: str):
        return self._resolve().get_metadata(file_id)

    def download_to(self, file_id: str, destination: str, *, chunk_size: int) -> int:
        return self._resolve().download_to(file_id, destination, chunk_size=chunk_size)


@lru_cache(maxsize=1)
def _drive_client_singleton() -> DriveClient:
    return _LazyDriveClient()


def get_drive_client() -> DriveClient:
    """Dependency seam so tests can inject a fake Drive client."""
    return _drive_client_singleton()


@lru_cache(maxsize=1)
def _extraction_semaphore() -> asyncio.Semaphore:
    # OCR is CPU-bound. The MVP deliberately processes documents one at a time rather than
    # letting concurrent requests thrash the CPU.
    return asyncio.Semaphore(get_settings().max_concurrent_extractions)


@dataclass(slots=True)
class ProcessingSummary:
    """Everything the response needs about *how* the document was processed.

    Deliberately a plain snapshot rather than the live page source: the PDF is closed as soon as
    extraction finishes, and on the native-text path the page object would otherwise outlive the
    document it belongs to.
    """

    page_count: int
    native_text_available: bool
    ocr_used: bool
    render_scale: float | None
    header_found: bool
    raw_text: str = ""
    tokens: list[dict] = field(default_factory=list)
    columns: list[dict] = field(default_factory=list)


def _process_pdf(path: str, settings: Settings) -> tuple[ExtractionOutcome, ProcessingSummary]:
    """Blocking work: open, analyse and extract. Runs in a worker thread."""
    document = open_document(path)
    try:
        analysis = analyze_document(document, settings)
        outcome = extract_fields(analysis.source, analysis.section)
        debug = settings.return_debug_data
        summary = ProcessingSummary(
            page_count=analysis.page_count,
            native_text_available=analysis.native_text_available,
            ocr_used=analysis.ocr_used,
            render_scale=analysis.render_scale,
            header_found=analysis.section.header_found,
            raw_text=analysis.raw_text if debug else "",
            tokens=[t.as_dict() for t in analysis.source.tokens()] if debug else [],
            columns=[c.as_dict() for c in analysis.section.columns],
        )
        return outcome, summary
    finally:
        document.close()


@router.post(
    "/extract",
    response_model=ExtractResponse,
    response_model_exclude_none=False,
    dependencies=[Depends(require_api_key)],
    summary="Extract vehicle characteristics from a SUNARP PDF stored in Google Drive",
)
async def extract_vehicle(
    payload: ExtractRequest,
    request: Request,
    drive: DriveClient = Depends(get_drive_client),
    settings: Settings = Depends(get_settings),
) -> ExtractResponse:
    request_id = getattr(request.state, "request_id", None) or str(uuid.uuid4())
    request_id_var.set(request_id)
    started = time.perf_counter()
    normalized_plate = normalize_plate(payload.plate)

    log_event(
        logger,
        "request_received",
        drive_file_id=payload.drive_file_id,
        plate=normalized_plate,
    )

    async with _extraction_semaphore():
        try:
            metadata = await run_in_threadpool(drive.get_metadata, payload.drive_file_id)
            log_event(
                logger,
                "drive_metadata_loaded",
                drive_file_id=payload.drive_file_id,
                filename=metadata.name,
                mime_type=metadata.mime_type,
                size=metadata.size,
            )
            ensure_pdf(metadata, settings)

            # Everything below lives inside the temporary directory and is removed on exit --
            # the PDF is never persisted.
            with tempfile.TemporaryDirectory(prefix="vehicle-ocr-") as tmpdir:
                pdf_path = os.path.join(tmpdir, "original.pdf")
                log_event(logger, "pdf_download_started", drive_file_id=payload.drive_file_id)
                size = await run_in_threadpool(
                    drive.download_to,
                    payload.drive_file_id,
                    pdf_path,
                    chunk_size=settings.drive_download_chunk_size,
                )
                if size > settings.max_pdf_bytes:
                    raise FileTooLargeError(
                        f"The document is {size} bytes, above the {settings.max_pdf_bytes} byte limit."
                    )
                log_event(
                    logger,
                    "pdf_download_completed",
                    drive_file_id=payload.drive_file_id,
                    size=size,
                )

                log_event(logger, "native_text_checked")
                log_event(logger, "target_section_detection_started")
                log_event(logger, "ocr_started")
                outcome, summary = await run_in_threadpool(_process_pdf, pdf_path, settings)
                log_event(
                    logger,
                    "ocr_completed",
                    ocr_used=summary.ocr_used,
                    render_scale=summary.render_scale,
                )

            if outcome.found_count == 0:
                raise TargetSectionNotFoundError()

            warnings: list[ExtractionWarning] = list(outcome.warnings)
            if not summary.header_found:
                warnings.insert(
                    0,
                    ExtractionWarning(
                        code=WarningCode.SECTION_FALLBACK_USED,
                        message="The section header was not found; the whole page was searched instead.",
                    ),
                )
            _append_plate_warning(warnings, normalized_plate, outcome.plate_in_document)

            elapsed_ms = int((time.perf_counter() - started) * 1000)
            log_event(
                logger,
                "field_extraction_completed",
                fields_found_count=outcome.found_count,
                warnings=len(warnings),
            )

            response = ExtractResponse(
                request=RequestEcho(
                    plate=payload.plate,
                    normalized_plate=normalized_plate,
                    drive_file_id=payload.drive_file_id,
                ),
                document=DocumentInfo(
                    filename=metadata.name,
                    mime_type=metadata.mime_type,
                    pages=summary.page_count,
                ),
                processing=ProcessingInfo(
                    native_text_available=summary.native_text_available,
                    ocr_used=summary.ocr_used,
                    target_section_found=summary.header_found,
                    render_scale=summary.render_scale,
                    processing_time_ms=elapsed_ms,
                ),
                fields=outcome.fields,
                values=outcome.values,
                warnings=warnings,
                debug=_build_debug(settings, summary, outcome),
            )

            log_event(
                logger,
                "request_completed",
                drive_file_id=payload.drive_file_id,
                plate=normalized_plate,
                filename=metadata.name,
                processing_time_ms=elapsed_ms,
                ocr_used=summary.ocr_used,
                fields_found_count=outcome.found_count,
            )
            return response

        except AppError as exc:
            log_event(
                logger,
                "request_failed",
                drive_file_id=payload.drive_file_id,
                plate=normalized_plate,
                error_code=exc.code,
                error_detail=exc.detail,
                processing_time_ms=int((time.perf_counter() - started) * 1000),
            )
            raise
        except Exception as exc:
            log_exception(
                logger,
                "request_failed",
                drive_file_id=payload.drive_file_id,
                error_code="DOCUMENT_PROCESSING_FAILED",
            )
            raise DocumentProcessingError(detail=str(exc)) from exc


def _append_plate_warning(
    warnings: list[ExtractionWarning], requested: str, found: str | None
) -> None:
    """Compare the requested plate with the one printed on the document.

    Compared on a confusable-folded, noise-tolerant form: OCR reads ``A0A952`` as ``AOA952`` and
    sometimes inserts a phantom character (``A0L952`` -> ``A0OL952``). Warning on those would be
    pure noise. A genuinely different vehicle still trips the warning. Never fails the request.
    """
    if not found:
        return
    if plates_match(found, requested):
        return
    warnings.append(
        ExtractionWarning(
            code=WarningCode.PLATE_MISMATCH,
            message=(
                f"The document's plate ({normalize_plate(found)}) differs from "
                f"the requested plate ({requested})."
            ),
        )
    )


def _build_debug(
    settings: Settings, summary: ProcessingSummary, outcome: ExtractionOutcome
) -> DebugInfo | None:
    if not settings.return_debug_data:
        return None
    return DebugInfo(
        raw_text=summary.raw_text,
        target_section_text=outcome.section_text,
        ocr_tokens=summary.tokens,
        column_geometry=summary.columns,
    )
