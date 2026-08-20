#!/usr/bin/env python3
"""Throwaway terminal tool: extract one vehicle's characteristics from a Drive PDF.

Ask-and-answer helper for eyeballing documents while testing. It runs the same pipeline the API
runs and prints the same JSON n8n will receive, but talks to Drive directly so no server has to
be running.

    ./check_plate.py                      # interactive, keeps asking until you press Enter
    ./check_plate.py <drive-id-or-url>    # one shot
    ./check_plate.py <drive-id-or-url> A0A952
    ./check_plate.py <drive-id-or-url> --json-only > out.json

Delete this file whenever you like; nothing else imports it.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
import time

from app.api.routes.vehicles import _append_plate_warning
from app.config import Settings
from app.core.exceptions import AppError
from app.models.extraction import (
    DocumentInfo,
    ExtractResponse,
    FieldStatus,
    ProcessingInfo,
    RequestEcho,
)
from app.services.drive import GoogleDriveClient, ensure_pdf
from app.services.pdf import analyze_document, open_document
from app.services.vehicle_characteristics import extract_fields
from app.utils.normalization import normalize_plate

# Accepts a bare id, or any of the Drive URL shapes that end up in a spreadsheet.
_URL_ID = re.compile(r"/d/([A-Za-z0-9_-]{10,})|[?&]id=([A-Za-z0-9_-]{10,})")
_BARE_ID = re.compile(r"^[A-Za-z0-9_-]{10,}$")

BOLD, DIM, GREEN, YELLOW, RED, RESET = (
    ("\033[1m", "\033[2m", "\033[32m", "\033[33m", "\033[31m", "\033[0m")
    if sys.stdout.isatty()
    else ("", "", "", "", "", "")
)

STATUS_COLOR = {
    FieldStatus.EXTRACTED: GREEN,
    FieldStatus.UNAVAILABLE: YELLOW,
    FieldStatus.NOT_FOUND: RED,
}


def parse_drive_id(text: str) -> str | None:
    text = text.strip().strip("\"'")
    if not text:
        return None
    match = _URL_ID.search(text)
    if match:
        return match.group(1) or match.group(2)
    return text if _BARE_ID.match(text) else None


def extract(drive_id: str, plate: str | None, settings: Settings) -> tuple[ExtractResponse, str]:
    started = time.perf_counter()
    client = GoogleDriveClient(settings)
    metadata = client.get_metadata(drive_id)
    ensure_pdf(metadata, settings)

    # Same discipline as the service: the PDF lives in a temp dir and is deleted straight after.
    with tempfile.TemporaryDirectory(prefix="check-plate-") as tmpdir:
        path = os.path.join(tmpdir, "original.pdf")
        client.download_to(drive_id, path, chunk_size=settings.drive_download_chunk_size)
        document = open_document(path)
        try:
            analysis = analyze_document(document, settings)
            outcome = extract_fields(analysis.source, analysis.section)
            page_count = analysis.page_count
            processing = ProcessingInfo(
                native_text_available=analysis.native_text_available,
                ocr_used=analysis.ocr_used,
                target_section_found=analysis.section.header_found,
                render_scale=analysis.render_scale,
                processing_time_ms=int((time.perf_counter() - started) * 1000),
            )
        finally:
            document.close()

    # Fall back to the Drive filename when no plate was given -- these files are named by plate.
    resolved_plate = plate or os.path.splitext(metadata.name)[0]
    warnings = list(outcome.warnings)
    _append_plate_warning(warnings, normalize_plate(resolved_plate), outcome.plate_in_document)

    response = ExtractResponse(
        request=RequestEcho(
            plate=resolved_plate,
            normalized_plate=normalize_plate(resolved_plate),
            drive_file_id=drive_id,
        ),
        document=DocumentInfo(
            filename=metadata.name, mime_type=metadata.mime_type, pages=page_count
        ),
        processing=processing,
        fields=outcome.fields,
        values=outcome.values,
        warnings=warnings,
    )
    return response, outcome.plate_in_document or "-"


def print_table(response: ExtractResponse, plate_in_pdf: str) -> None:
    request, document, processing = response.request, response.document, response.processing
    print()
    print(f"{BOLD}{request.normalized_plate}{RESET}  {DIM}{document.filename}{RESET}")
    print(
        f"{DIM}pages={document.pages}  ocr={processing.ocr_used}  "
        f"scale={processing.render_scale}  section_found={processing.target_section_found}  "
        f"plate_in_pdf={plate_in_pdf}  {processing.processing_time_ms} ms{RESET}"
    )
    print()
    print(f"  {'FIELD':16}{'VALUE':>30}   {'STATUS':12}{'CONF':>6}  VALID")
    print("  " + "-" * 72)
    for name, result in response.fields.items():
        value = "-" if result.value is None else str(result.value)
        confidence = "-" if result.ocr_confidence is None else f"{result.ocr_confidence:.0f}"
        valid = {True: "yes", False: f"{RED}NO{RESET}", None: "-"}[result.valid]
        color = STATUS_COLOR[result.status]
        print(
            f"  {name.value:16}{color}{value:>30}{RESET}   "
            f"{color}{result.status.value:12}{RESET}{confidence:>6}  {valid}"
        )
    print()
    if response.warnings:
        print(f"  {YELLOW}warnings{RESET}")
        for warning in response.warnings:
            field = f" [{warning.field.value}]" if warning.field else ""
            print(f"    {YELLOW}!{RESET} {warning.code.value}{field}: {warning.message}")
    else:
        print(f"  {GREEN}no warnings{RESET}")
    print()


def handle(drive_id: str, plate: str | None, settings: Settings, args) -> int:
    try:
        response, plate_in_pdf = extract(drive_id, plate, settings)
    except AppError as exc:
        print(f"{RED}{exc.code}{RESET}: {exc.message}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - it is a debugging tool; show what broke
        print(f"{RED}FAILED{RESET}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if not args.json_only:
        print_table(response, plate_in_pdf)
    if not args.table_only:
        if not args.json_only:
            print(f"{DIM}--- JSON (exactly what n8n receives) ---{RESET}")
        print(response.model_dump_json(indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("drive_id", nargs="?", help="Drive file id, or a full Drive URL")
    parser.add_argument("plate", nargs="?", help="plate (defaults to the Drive filename)")
    parser.add_argument("--json-only", action="store_true", help="print only the JSON")
    parser.add_argument("--table-only", action="store_true", help="print only the table")
    args = parser.parse_args()

    settings = Settings()
    if not settings.google_application_credentials and not os.environ.get(
        "GOOGLE_APPLICATION_CREDENTIALS"
    ):
        print(
            f"{YELLOW}note{RESET}: no service-account path configured. Set "
            "GOOGLE_APPLICATION_CREDENTIALS in .env (see .env.example).",
            file=sys.stderr,
        )

    if args.drive_id:
        drive_id = parse_drive_id(args.drive_id)
        if not drive_id:
            print(f"{RED}Not a Drive id or URL:{RESET} {args.drive_id}", file=sys.stderr)
            return 2
        return handle(drive_id, args.plate, settings, args)

    print(f"{BOLD}SUNARP characteristics check{RESET}  {DIM}(Enter on an empty line to quit){RESET}")
    while True:
        try:
            raw = input(f"\n{BOLD}Drive ID or URL:{RESET} ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not raw:
            return 0
        drive_id = parse_drive_id(raw)
        if not drive_id:
            print(f"{RED}Not a Drive id or URL.{RESET} Paste the id or the full /file/d/... link.")
            continue
        try:
            plate = input(f"{BOLD}Plate{RESET} {DIM}(Enter to use the filename):{RESET} ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        handle(drive_id, plate or None, settings, args)


if __name__ == "__main__":
    raise SystemExit(main())
