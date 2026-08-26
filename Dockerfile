# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------------------------
# Build stage: resolve dependencies into a self-contained virtualenv.
# ---------------------------------------------------------------------------------------------
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install --no-cache-dir .

# ---------------------------------------------------------------------------------------------
# Runtime stage
# ---------------------------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

# Tesseract plus the Spanish and English language data. These documents are Spanish; `eng` is
# included because it is small and useful when OCR_LANG is set to "spa+eng".
#
# Note on OCRmyPDF/qpdf/Ghostscript: this service does not use them. It needs word-level bounding
# boxes (pytesseract image_to_data) to associate labels with values across the three-column
# layout, which a sidecar-text tool cannot provide. Installing them would add weight and attack
# surface for no benefit. PyMuPDF ships its own MuPDF, so no extra PDF libraries are required.
RUN apt-get update \
    && apt-get install --no-install-recommends -y \
        tesseract-ocr \
        tesseract-ocr-spa \
        tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

# Run as a non-root user.
RUN groupadd --system --gid 1001 app \
    && useradd --system --uid 1001 --gid app --create-home app

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /srv/app
COPY --chown=app:app app ./app
# Operator tool: lets you check a single Drive id from inside the running container.
COPY --chown=app:app check_plate.py ./

USER app
EXPOSE 8000

# Uses the unauthenticated liveness probe.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

# OCR is CPU-bound and the MVP handles one document at a time, so a single worker is correct.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
