"""Unauthenticated liveness and readiness probes."""

from __future__ import annotations

from typing import Any

import pytesseract
from fastapi import APIRouter, Response, status

from app.config import get_settings
from app.services.drive import DRIVE_SCOPES

router = APIRouter(tags=["health"])


@router.get("/health", summary="Liveness probe")
def health() -> dict[str, str]:
    return {"status": "ok"}


def _check_tesseract(language: str) -> dict[str, Any]:
    try:
        available = set(pytesseract.get_languages(config=""))
    except Exception as exc:  # pragma: no cover - depends on the host install
        return {"ok": False, "detail": f"tesseract unavailable: {type(exc).__name__}"}
    missing = [code for code in language.split("+") if code not in available]
    if missing:
        return {"ok": False, "detail": f"missing language data: {', '.join(missing)}"}
    return {"ok": True, "version": str(pytesseract.get_tesseract_version())}


def _check_drive_credentials() -> dict[str, Any]:
    """Confirm credentials *resolve*. Never touches the network and never reveals the key."""
    settings = get_settings()
    try:
        if settings.google_application_credentials:
            from google.oauth2 import service_account

            service_account.Credentials.from_service_account_file(
                settings.google_application_credentials, scopes=list(DRIVE_SCOPES)
            )
        else:
            import google.auth

            google.auth.default(scopes=list(DRIVE_SCOPES))
    except Exception as exc:
        return {"ok": False, "detail": f"credentials unavailable: {type(exc).__name__}"}
    return {"ok": True}


@router.get("/ready", summary="Readiness probe (configuration and dependencies, no OCR)")
def ready(response: Response) -> dict[str, Any]:
    settings = get_settings()
    checks = {
        "api_key_configured": {"ok": settings.has_api_key},
        "tesseract": _check_tesseract(settings.ocr_lang),
        "google_credentials": _check_drive_credentials(),
    }
    all_ok = all(check["ok"] for check in checks.values())
    if not all_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ready" if all_ok else "not_ready", "checks": checks}
