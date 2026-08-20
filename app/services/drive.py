"""Read-only Google Drive access.

Credentials come from ``GOOGLE_APPLICATION_CREDENTIALS`` or Application Default Credentials and
are never logged, echoed, or embedded in the image. Downloads are streamed to disk in chunks
rather than buffered whole in memory.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import google.auth
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

from app.config import Settings
from app.core.exceptions import (
    DocumentProcessingError,
    DriveFileNotFoundError,
    DrivePermissionDeniedError,
    FileTooLargeError,
    InvalidDocumentTypeError,
)

DRIVE_SCOPES = ("https://www.googleapis.com/auth/drive.readonly",)
PDF_MIME_TYPE = "application/pdf"

#: Requested on every call so files living in Shared Drives resolve.
_SHARED_DRIVE_ARGS = {"supportsAllDrives": True}
_METADATA_FIELDS = "id,name,mimeType,size,modifiedTime"


@dataclass(frozen=True, slots=True)
class DriveFileMetadata:
    id: str
    name: str
    mime_type: str
    size: int | None
    modified_time: str | None


class DriveClient(Protocol):
    def get_metadata(self, file_id: str) -> DriveFileMetadata: ...

    def download_to(self, file_id: str, destination: str, *, chunk_size: int) -> int: ...


def _credentials(settings: Settings):
    if settings.google_application_credentials:
        return service_account.Credentials.from_service_account_file(
            settings.google_application_credentials, scopes=list(DRIVE_SCOPES)
        )
    credentials, _project = google.auth.default(scopes=list(DRIVE_SCOPES))
    return credentials


def _translate(error: HttpError, file_id: str) -> Exception:
    status = getattr(error, "status_code", None) or getattr(error.resp, "status", None)
    if status == 404:
        return DriveFileNotFoundError(f"Drive file '{file_id}' was not found.")
    if status in (401, 403):
        return DrivePermissionDeniedError(
            f"The service account is not allowed to read Drive file '{file_id}'."
        )
    return DocumentProcessingError("Google Drive request failed.", detail=str(error))


class GoogleDriveClient:
    """Thin wrapper over the Drive v3 API."""

    def __init__(self, settings: Settings):
        self._settings = settings
        self._service = build(
            "drive", "v3", credentials=_credentials(settings), cache_discovery=False
        )

    def get_metadata(self, file_id: str) -> DriveFileMetadata:
        try:
            payload = (
                self._service.files()
                .get(fileId=file_id, fields=_METADATA_FIELDS, **_SHARED_DRIVE_ARGS)
                .execute()
            )
        except HttpError as exc:
            raise _translate(exc, file_id) from exc
        raw_size = payload.get("size")
        return DriveFileMetadata(
            id=payload.get("id", file_id),
            name=payload.get("name", f"{file_id}.pdf"),
            mime_type=payload.get("mimeType", ""),
            size=int(raw_size) if raw_size is not None else None,
            modified_time=payload.get("modifiedTime"),
        )

    def download_to(self, file_id: str, destination: str, *, chunk_size: int) -> int:
        request = self._service.files().get_media(fileId=file_id, **_SHARED_DRIVE_ARGS)
        written = 0
        try:
            with open(destination, "wb") as handle:
                downloader = MediaIoBaseDownload(handle, request, chunksize=chunk_size)
                done = False
                while not done:
                    _status, done = downloader.next_chunk()
                written = handle.tell()
        except HttpError as exc:
            raise _translate(exc, file_id) from exc
        return written


def ensure_pdf(metadata: DriveFileMetadata, settings: Settings) -> None:
    """Reject anything that is not a PDF, or is too large, *before* downloading it."""
    if metadata.mime_type != PDF_MIME_TYPE:
        raise InvalidDocumentTypeError(
            f"Expected a PDF document, but the Drive file is '{metadata.mime_type or 'unknown'}'."
        )
    if metadata.size is not None and metadata.size > settings.max_pdf_bytes:
        raise FileTooLargeError(
            f"The document is {metadata.size} bytes, above the {settings.max_pdf_bytes} byte limit."
        )
