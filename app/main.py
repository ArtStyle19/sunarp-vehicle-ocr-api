"""FastAPI application factory, middleware and error rendering."""

from __future__ import annotations

import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.routes import health, vehicles
from app.config import get_settings
from app.core.exceptions import AppError, InvalidRequestError
from app.core.logging import configure_logging, get_logger, log_exception, request_id_var
from app.models.extraction import ErrorBody, ErrorResponse

logger = get_logger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"

#: Starlette status codes we can map onto our own stable vocabulary.
_STATUS_TO_CODE = {401: "UNAUTHORIZED", 403: "UNAUTHORIZED", 404: "NOT_FOUND", 405: "INVALID_REQUEST"}


def _error_response(status_code: int, code: str, message: str) -> JSONResponse:
    body = ErrorResponse(
        error=ErrorBody(code=code, message=message), request_id=request_id_var.get()
    )
    return JSONResponse(
        status_code=status_code,
        content=body.model_dump(mode="json"),
        headers={REQUEST_ID_HEADER: request_id_var.get()},
    )


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title="SUNARP Vehicle Characteristics API",
        version="1.0.0",
        description=(
            "Extracts a fixed set of vehicle characteristics from SUNARP 'Boleta Informativa' "
            "PDFs stored in Google Drive. Values are never inferred: redacted cells come back "
            "as 'unavailable' and unreadable ones as 'not_found'."
        ),
    )

    @app.middleware("http")
    async def attach_request_id(request: Request, call_next):
        request_id = request.headers.get(REQUEST_ID_HEADER) or str(uuid.uuid4())
        request.state.request_id = request_id
        token = request_id_var.set(request_id)
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response

    @app.exception_handler(AppError)
    async def handle_app_error(_request: Request, exc: AppError) -> JSONResponse:
        return _error_response(exc.http_status, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        # Pydantic's own message is safe to surface: it describes the caller's payload, not ours.
        first = exc.errors()[0] if exc.errors() else {}
        location = ".".join(str(part) for part in first.get("loc", ()) if part != "body")
        detail = first.get("msg", "Invalid request payload.")
        message = f"{location}: {detail}" if location else detail
        return _error_response(InvalidRequestError.http_status, InvalidRequestError.code, message)

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _STATUS_TO_CODE.get(exc.status_code, "INTERNAL_ERROR")
        return _error_response(exc.status_code, code, str(exc.detail))

    @app.exception_handler(Exception)
    async def handle_unexpected(_request: Request, exc: Exception) -> JSONResponse:
        # Log the type for diagnosis; never return a traceback to the caller.
        log_exception(logger, "request_failed", error_code="INTERNAL_ERROR")
        return _error_response(500, "INTERNAL_ERROR", "An unexpected error occurred.")

    app.include_router(health.router)
    app.include_router(vehicles.router)
    return app


app = create_app()
