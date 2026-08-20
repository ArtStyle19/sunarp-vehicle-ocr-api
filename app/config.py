"""Application configuration, loaded from the environment.

Nothing here ever holds a credential *value* other than the API key used to authenticate n8n;
Google credentials are referenced by path only and resolved by google-auth.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # --- service -------------------------------------------------------------------------
    app_name: str = "vehicle-ocr-api"
    log_level: str = "INFO"

    #: Shared secret n8n sends in ``X-API-Key``. Required; the service refuses to start without it.
    api_key: str = Field(default="", description="Shared secret for the X-API-Key header")

    #: When true the response carries raw OCR text and tokens. Never enable in production.
    return_debug_data: bool = False

    # --- Google Drive --------------------------------------------------------------------
    #: Path to the service-account JSON. Empty means "use Application Default Credentials".
    google_application_credentials: str = ""
    drive_download_chunk_size: int = 1024 * 1024
    max_pdf_bytes: int = 25 * 1024 * 1024

    # --- OCR -----------------------------------------------------------------------------
    ocr_lang: str = "spa"
    ocr_timeout_seconds: int = 120

    #: Scale used for the first (probe) render. Also the floor for the adaptive scale.
    ocr_probe_scale: float = 1.5
    ocr_min_scale: float = 1.5
    ocr_max_scale: float = 4.0

    #: Target median glyph-box height in pixels. Measured on the sample: Tesseract is most
    #: accurate on this template around 26 px. Rendering higher (e.g. a fixed 300 DPI against a
    #: 72 ppi scan) measurably *loses* decimal points and corrupts digits -- see README.
    ocr_target_token_height_px: int = 26

    #: A page is treated as having usable native text when it yields at least this many words.
    native_text_min_words: int = 40

    #: Upper bound on pages rasterised while hunting for the section. Keeps a pathological
    #: multi-page document from turning into an unbounded OCR job.
    max_ocr_pages: int = 3

    #: Max simultaneous extractions. OCR is CPU-bound; the MVP processes one PDF at a time.
    max_concurrent_extractions: int = 1

    #: Emit a warning (never an error) when the plate in the PDF disagrees with the request.
    fail_on_plate_mismatch: bool = False

    @field_validator("ocr_lang")
    @classmethod
    def _non_empty_lang(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("ocr_lang must not be empty")
        return value.strip()

    @property
    def has_api_key(self) -> bool:
        return bool(self.api_key.strip())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
