"""Application errors that map onto the stable, machine-readable error codes n8n consumes.

Every failure surfaced to a client goes through :class:`AppError`. Python tracebacks are never
returned; ``app.main`` renders these as ``{"status": "error", "error": {"code", "message"}}``.
"""

from __future__ import annotations

from http import HTTPStatus


class AppError(Exception):
    """Base class for every error that is safe to expose to the caller."""

    code: str = "INTERNAL_ERROR"
    http_status: int = HTTPStatus.INTERNAL_SERVER_ERROR
    message: str = "An unexpected error occurred."

    def __init__(self, message: str | None = None, *, detail: str | None = None) -> None:
        self.message = message or self.message
        # ``detail`` is for logs only and is deliberately never serialised into the response.
        self.detail = detail
        super().__init__(self.message)


class InvalidRequestError(AppError):
    code = "INVALID_REQUEST"
    http_status = HTTPStatus.UNPROCESSABLE_ENTITY
    message = "The request payload is invalid."


class UnauthorizedError(AppError):
    code = "UNAUTHORIZED"
    http_status = HTTPStatus.UNAUTHORIZED
    message = "Missing or invalid API key."


class DriveFileNotFoundError(AppError):
    code = "DRIVE_FILE_NOT_FOUND"
    http_status = HTTPStatus.NOT_FOUND
    message = "The requested Drive file does not exist."


class DrivePermissionDeniedError(AppError):
    code = "DRIVE_PERMISSION_DENIED"
    http_status = HTTPStatus.FORBIDDEN
    message = "The service account cannot read the requested Drive file."


class InvalidDocumentTypeError(AppError):
    code = "INVALID_DOCUMENT_TYPE"
    http_status = HTTPStatus.UNSUPPORTED_MEDIA_TYPE
    message = "Expected a PDF document."


class FileTooLargeError(AppError):
    code = "FILE_TOO_LARGE"
    http_status = HTTPStatus.REQUEST_ENTITY_TOO_LARGE
    message = "The document exceeds the configured size limit."


class PdfInvalidError(AppError):
    code = "PDF_INVALID"
    http_status = HTTPStatus.UNPROCESSABLE_ENTITY
    message = "The downloaded file could not be opened as a PDF."


class TargetSectionNotFoundError(AppError):
    code = "TARGET_SECTION_NOT_FOUND"
    http_status = HTTPStatus.UNPROCESSABLE_ENTITY
    message = "The 'Caracteristicas del Vehiculo' section could not be located."


class OcrTimeoutError(AppError):
    code = "OCR_TIMEOUT"
    http_status = HTTPStatus.GATEWAY_TIMEOUT
    message = "OCR did not finish within the configured timeout."


class OcrFailedError(AppError):
    code = "OCR_FAILED"
    http_status = HTTPStatus.INTERNAL_SERVER_ERROR
    message = "OCR failed to process the document."


class DocumentProcessingError(AppError):
    code = "DOCUMENT_PROCESSING_FAILED"
    http_status = HTTPStatus.INTERNAL_SERVER_ERROR
    message = "The document could not be processed."


class InternalError(AppError):
    code = "INTERNAL_ERROR"
    http_status = HTTPStatus.INTERNAL_SERVER_ERROR
    message = "An unexpected error occurred."
