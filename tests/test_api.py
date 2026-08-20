"""End-to-end API behaviour, including the golden regression on the reference PDF."""

from __future__ import annotations

import shutil
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.exceptions import DriveFileNotFoundError
from app.services.drive import DriveFileMetadata
from tests.conftest import GOLDEN_VALUES

API_KEY = "test-secret-key"
DRIVE_FILE_ID = "14k4VNYTJf4CQnZTTdswNs5COtylfZ_bp"
PAYLOAD = {"plate": "A0A952", "drive_file_id": DRIVE_FILE_ID}


class FakeDriveClient:
    """Stands in for Google Drive so tests never touch the network."""

    def __init__(
        self,
        source_pdf: Path,
        *,
        mime_type: str = "application/pdf",
        name: str = "A0A952.pdf",
        size: int | None = None,
        error: Exception | None = None,
    ):
        self.source_pdf = source_pdf
        self.mime_type = mime_type
        self.name = name
        self.size = size if size is not None else source_pdf.stat().st_size
        self.error = error
        self.downloaded_to: list[str] = []

    def get_metadata(self, file_id: str) -> DriveFileMetadata:
        if self.error is not None:
            raise self.error
        return DriveFileMetadata(
            id=file_id,
            name=self.name,
            mime_type=self.mime_type,
            size=self.size,
            modified_time="2026-08-18T23:56:18.000Z",
        )

    def download_to(self, file_id: str, destination: str, *, chunk_size: int) -> int:
        shutil.copyfile(self.source_pdf, destination)
        self.downloaded_to.append(destination)
        return Path(destination).stat().st_size


def _make_client(monkeypatch: pytest.MonkeyPatch, drive: FakeDriveClient, **env: str) -> TestClient:
    from app.api.routes import vehicles
    from app.config import get_settings

    monkeypatch.setenv("API_KEY", API_KEY)
    # Keep the suite hermetic: never read a developer's .env or real credentials.
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    vehicles._drive_client_singleton.cache_clear()
    vehicles._extraction_semaphore.cache_clear()

    from app.main import create_app

    app = create_app()
    app.dependency_overrides[vehicles.get_drive_client] = lambda: drive
    return TestClient(app)


@pytest.fixture
def drive(sample_pdf_path: Path) -> FakeDriveClient:
    return FakeDriveClient(sample_pdf_path)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, drive: FakeDriveClient) -> TestClient:
    return _make_client(monkeypatch, drive)


def test_health_is_unauthenticated(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_reports_dependency_checks(client: TestClient) -> None:
    response = client.get("/ready")
    assert response.status_code in (200, 503)
    body = response.json()
    assert set(body["checks"]) == {"api_key_configured", "tesseract", "google_credentials"}
    assert body["checks"]["api_key_configured"]["ok"] is True


@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "wrong-key"}])
def test_extract_requires_a_valid_api_key(client: TestClient, headers: dict) -> None:
    response = client.post("/api/v1/vehicles/extract", headers=headers, json=PAYLOAD)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


@pytest.fixture(scope="module")
def response_body(request: pytest.FixtureRequest) -> dict:
    """Run the reference PDF through the real pipeline once for the whole golden class."""
    monkeypatch = pytest.MonkeyPatch()
    request.addfinalizer(monkeypatch.undo)
    drive = FakeDriveClient(request.getfixturevalue("sample_pdf_path"))
    client = _make_client(monkeypatch, drive)
    response = client.post("/api/v1/vehicles/extract", headers={"X-API-Key": API_KEY}, json=PAYLOAD)
    assert response.status_code == 200, response.text
    return response.json()


class TestGoldenExtraction:
    """The reference PDF must always produce exactly these values."""

    def test_envelope(self, response_body: dict) -> None:
        assert response_body["status"] == "success"
        assert response_body["request"] == {
            "plate": "A0A952",
            "normalized_plate": "A0A952",
            "drive_file_id": DRIVE_FILE_ID,
        }
        assert response_body["document"]["filename"] == "A0A952.pdf"
        assert response_body["document"]["mime_type"] == "application/pdf"
        assert response_body["document"]["pages"] == 1

    def test_processing_reports_the_ocr_path(self, response_body: dict) -> None:
        processing = response_body["processing"]
        # The reference PDF is a single JPEG with no text layer at all.
        assert processing["native_text_available"] is False
        assert processing["ocr_used"] is True
        assert processing["target_section_found"] is True

    @pytest.mark.parametrize(("field", "expected"), sorted(GOLDEN_VALUES.items()))
    def test_field_values(self, response_body: dict, field: str, expected) -> None:
        result = response_body["fields"][field]
        assert result["status"] == "extracted", f"{field}: {result}"
        assert result["valid"] is True
        if isinstance(expected, str) and field != "NUM_PARTIDA" and field != "TIPO_USO":
            assert Decimal(str(result["value"])) == Decimal(expected)
        else:
            assert result["value"] == expected

    def test_num_partida_is_serialised_as_a_string(self, response_body: dict) -> None:
        assert response_body["fields"]["NUM_PARTIDA"]["value"] == "52172133"
        assert isinstance(response_body["fields"]["NUM_PARTIDA"]["value"], str)

    def test_decimals_are_json_numbers_with_their_scale_intact(self, response_body: dict) -> None:
        assert response_body["fields"]["PESO_NETO"]["value"] == 2.89
        assert response_body["fields"]["PESO_BRUTO"]["value"] == 4.6
        assert response_body["fields"]["LONGITUD"]["value"] == 6.99

    def test_contract_is_exactly_twelve_fields(self, response_body: dict) -> None:
        assert len(response_body["fields"]) == 12
        assert "CILINDRADA" not in response_body["fields"]

    def test_flat_values_block_mirrors_fields(self, response_body: dict) -> None:
        values = response_body["values"]
        assert set(values) == set(response_body["fields"])
        for name, result in response_body["fields"].items():
            assert values[name] == result["value"]

    def test_confidences_are_real_or_absent(self, response_body: dict) -> None:
        for name, result in response_body["fields"].items():
            confidence = result["ocr_confidence"]
            assert confidence is None or 0.0 <= confidence <= 100.0, name

    def test_debug_key_is_absent_by_default(self, response_body: dict) -> None:
        assert "debug" not in response_body

    def test_null_field_values_still_survive_serialisation(self, response_body: dict) -> None:
        """Dropping ``debug`` must not drop meaningful nulls elsewhere."""
        for result in response_body["fields"].values():
            assert set(result) == {"value", "status", "ocr_confidence", "valid", "source_label"}

    def test_no_warnings_on_the_reference_document(self, response_body: dict) -> None:
        assert response_body["warnings"] == []


def test_debug_payload_is_returned_when_enabled(
    monkeypatch: pytest.MonkeyPatch, drive: FakeDriveClient
) -> None:
    client = _make_client(monkeypatch, drive, RETURN_DEBUG_DATA="true")
    response = client.post("/api/v1/vehicles/extract", headers={"X-API-Key": API_KEY}, json=PAYLOAD)
    assert response.status_code == 200
    debug = response.json()["debug"]
    assert debug is not None
    assert "Caracter" in debug["raw_text"]
    assert len(debug["ocr_tokens"]) > 50
    assert len(debug["column_geometry"]) == 3


def test_temporary_files_are_removed(client: TestClient, drive: FakeDriveClient) -> None:
    response = client.post("/api/v1/vehicles/extract", headers={"X-API-Key": API_KEY}, json=PAYLOAD)
    assert response.status_code == 200
    assert drive.downloaded_to, "the fake Drive client should have been asked for the PDF"
    for path in drive.downloaded_to:
        assert not Path(path).exists()
        assert not Path(path).parent.exists()


def test_plate_is_normalised_and_mismatch_only_warns(
    monkeypatch: pytest.MonkeyPatch, drive: FakeDriveClient
) -> None:
    client = _make_client(monkeypatch, drive)
    response = client.post(
        "/api/v1/vehicles/extract",
        headers={"X-API-Key": API_KEY},
        json={"plate": "2ZR-315", "drive_file_id": DRIVE_FILE_ID},
    )
    assert response.status_code == 200, "a plate mismatch must not fail the extraction"
    body = response.json()
    assert body["request"]["normalized_plate"] == "2ZR315"
    assert any(w["code"] == "PLATE_MISMATCH" for w in body["warnings"])
    # The document's own values are still returned in full.
    assert body["values"]["NUM_EJES"] == 2


def test_non_pdf_is_rejected_before_download(
    monkeypatch: pytest.MonkeyPatch, sample_pdf_path: Path
) -> None:
    drive = FakeDriveClient(sample_pdf_path, mime_type="image/png", name="scan.png")
    client = _make_client(monkeypatch, drive)
    response = client.post("/api/v1/vehicles/extract", headers={"X-API-Key": API_KEY}, json=PAYLOAD)
    assert response.status_code == 415
    assert response.json()["error"]["code"] == "INVALID_DOCUMENT_TYPE"
    assert drive.downloaded_to == [], "a non-PDF must never be downloaded"


def test_oversized_file_is_rejected(monkeypatch: pytest.MonkeyPatch, sample_pdf_path: Path) -> None:
    drive = FakeDriveClient(sample_pdf_path, size=99 * 1024 * 1024)
    client = _make_client(monkeypatch, drive)
    response = client.post("/api/v1/vehicles/extract", headers={"X-API-Key": API_KEY}, json=PAYLOAD)
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "FILE_TOO_LARGE"


def test_missing_drive_file_maps_to_a_stable_code(
    monkeypatch: pytest.MonkeyPatch, sample_pdf_path: Path
) -> None:
    drive = FakeDriveClient(sample_pdf_path, error=DriveFileNotFoundError())
    client = _make_client(monkeypatch, drive)
    response = client.post("/api/v1/vehicles/extract", headers={"X-API-Key": API_KEY}, json=PAYLOAD)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "DRIVE_FILE_NOT_FOUND"


def test_invalid_pdf_reports_pdf_invalid(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"this is not a pdf")
    drive = FakeDriveClient(broken)
    client = _make_client(monkeypatch, drive)
    response = client.post("/api/v1/vehicles/extract", headers={"X-API-Key": API_KEY}, json=PAYLOAD)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "PDF_INVALID"


@pytest.mark.parametrize(
    "payload",
    [
        {"plate": "", "drive_file_id": DRIVE_FILE_ID},
        {"plate": "A0A952"},
        {"drive_file_id": DRIVE_FILE_ID},
        {"plate": "A0A952", "drive_file_id": "short"},
        {"plate": "A0A952", "drive_file_id": DRIVE_FILE_ID, "unexpected": True},
    ],
)
def test_invalid_payloads_are_rejected(client: TestClient, payload: dict) -> None:
    response = client.post("/api/v1/vehicles/extract", headers={"X-API-Key": API_KEY}, json=payload)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_REQUEST"


def test_errors_never_leak_a_traceback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"not a pdf")
    client = _make_client(monkeypatch, FakeDriveClient(broken))
    response = client.post("/api/v1/vehicles/extract", headers={"X-API-Key": API_KEY}, json=PAYLOAD)
    body = response.text
    assert "Traceback" not in body
    assert "File \"" not in body
    assert set(response.json()) == {"status", "error", "request_id"}
