# SUNARP Vehicle Characteristics API

A small FastAPI service that extracts **12 specific vehicle characteristics** from SUNARP
*Boleta Informativa* PDFs stored in Google Drive, and returns them as structured JSON for n8n to
write into Google Sheets.

```text
n8n ──▶ POST /api/v1/vehicles/extract ──▶ Google Drive ──▶ temp PDF ──▶ native text? ──▶ OCR
                                                                                          │
        n8n ◀────────────── JSON (12 fields) ◀── validate ◀── normalise ◀── spatial extract ┘
```

The service **never writes to Google Sheets** and **never invents a value**. A redacted cell comes
back as `unavailable`; an unreadable one as `not_found`. Nothing is inferred, derived from another
field, or replaced with zero.

---

## 1. What the reference document actually looks like

The extraction logic was built against a real document (`tests/fixtures/A0A952.pdf`) and measured,
not assumed. The findings below are what shaped the design.

| Property | Value |
| --- | --- |
| Pages | 1 |
| Page size | 1190 × 1684 pt (A2) |
| Native text | **None** — `pdftotext` returns a single byte |
| Content | One embedded JPEG, 1190 × 1684 px, RGB, **72 ppi** |

So this document has no text layer at all and **must** be OCR'd. The pipeline still tries native
text first, because other SUNARP exports may have one.

### The three-column layout

`Características del Vehículo` runs from its heading down to the `NO REGISTRA AFECTACIONES` rule.
Inside it are three independent `label : value` columns. Measured at 2× render scale
(page width 2380 px):

| Column | Label left | Colon anchor | Value left | Target fields |
| --- | --- | --- | --- | --- |
| 1 | 80 | 323 | 342 | `Tipo Uso` |
| 2 | 842 | 1087 | 1106 | `N° Cilindros`, `Peso Neto`, `Peso Bruto`, `N° Asientos`, `N° Pasajer.` |
| 3 | 1608 | 1883 | 1902 | `N° Partida`, `N° Ejes`, `N° Ruedas`, `Longitud`, `Ancho`, `Altura` |

Rows are **staggered** between columns, so flat OCR text interleaves them onto shared lines
(`N° Cilindros : 4    Ancho : 1.99`). At 3× scale Tesseract's reading order collapsed entirely,
emitting every column-1 label as one block with the values far away. **A regex over page text is
therefore unsafe**, which is why association is geometric.

### Four failure modes found by measurement

These are real observations from running Tesseract on the sample, and each one is defended
against in code and pinned by a test.

**1. Colon fusion silently corrupts numbers.** In full-page OCR the colon glyph merges into the
value token:

| Label | Full-page OCR reads | True value |
| --- | --- | --- |
| `N° Ejes` | `22` | 2 |
| `N° Ruedas` | `16` | 6 |
| `Carga Útil` | `21.71` | 1.71 |

Text alone cannot distinguish `22` → 2 from `20` → 20. Geometry can: a value token whose left edge
sits *on* the column's colon anchor begins with the colon, while one starting at the value column
does not. `22` at x=1883 is the colon column; `20` at x=1108 is the value column.

**2. The `##########` mask does not survive OCR as `#`.** Under `-l spa` a redacted cell is
recognised as `AAAAAIAAAAAAE`, `ad ddidaididid`, or `PARAR AAA RAR`; under `-l eng` as
`HHAHHAHAAEE`. **Searching for `#` would silently fail.** What is reliable is the combination of:
no digits, an implausibly small distinct-character set for the length, and a near-zero Tesseract
confidence (masked tokens report exactly `0.00`).

**3. Higher DPI is measurably worse.** The page content is a 72 ppi scan, so rendering at 300 DPI
is pure interpolation — and it degrades accuracy:

| Render scale | Outcome on the reference document |
| --- | --- |
| 1.5× | all values correct |
| 2× | all values correct |
| 3× | drops the decimal point (`4.6` → `46`), loses `LONGITUD` |
| 4.17× (= 300 DPI) | `4.6` → `46`, and `52172133` → `92172133` |

> **Deliberate deviation from the original brief.** A fixed 300 DPI default was requested. It is
> the wrong choice for these documents. Instead the service measures glyph height on a cheap probe
> render and scales to put glyphs near `OCR_TARGET_TOKEN_HEIGHT_PX` (default 26 px), clamped to
> 1.5×–4×. On this template that lands on ~2×; a genuine 300 DPI scan still works, because the
> target is glyph size rather than a fixed multiplier.

**4. Overlapping text destroys labels.** Some boletas print a long `Color 1` value
(`BLANCO ROJO VIOLETA AZUL`) that physically runs over the next column's `N° Asientos` label.
OCR fuses them into a single unreadable token — observed: `ARUAsientos`, `AMARWNiABientos`,
`AZUN”`. Label matching therefore has three tiers, tried in separate passes so a degraded match
can never pre-empt an exact one: exact sequence, then the distinctive keyword alone (including as
a token *suffix*), then approximate suffix matching above a 0.85 similarity ratio. The threshold
is measured, not guessed: genuine fusions score 0.875–1.000 against the expected word while every
unrelated label in the section scores at most 0.500. Column membership is decided by a token's
*right* edge so a fused token that begins in the previous column still belongs to this one.

When a label is destroyed beyond recognition the field returns `not_found` with a warning — it is
never inferred from row position, and never derived from `N° Pasajer.`

**5. Language and crop padding matter for decimals.** `-l spa` reads the `Peso Bruto` cell as
`4.6`; `-l eng` reads `46`. A tight crop clips the baseline period and also yields `46`, so value
crops keep vertical padding of 55 % of the label height.

---

## 2. How extraction works

1. **Native text first.** PyMuPDF `get_text("words")`. If a page has enough words *and* the
   section heading, those words become the tokens, with `ocr_confidence: null` — there is no OCR
   confidence, and none is fabricated. Otherwise the page is rasterised.
2. **Adaptive scale.** Probe render at 1.5×, measure median glyph height, rescale toward the
   target, re-render only if the correction is material.
3. **Locate the section** by its heading; the bottom edge is the first `AFECTACIONES` / `TÍTULOS`
   below it, else a normalised fallback height. If the heading is missing, the whole page is
   searched and a `SECTION_FALLBACK_USED` warning is attached.
4. **Derive column geometry from the page** — never a hardcoded pixel rectangle:
   - cluster the x positions of colon tokens to find the column anchors;
   - for each anchor, walk left across the label's own words and take the **median** run start
     across rows (the median matters: a single stray em-dash bridged two columns and would have
     dragged a `min` far too far left);
   - a value cell ends where the next column's labels begin, which is what keeps the multi-word
     `Transporte interprovincial` intact.
   All of it is stored as fractions of page size, so it is resolution independent. Measured
   fractions from the reference document ship only as a fallback.
5. **Row banding**, then left-to-right ordering *within* each band. Sorting by raw `y` scrambles
   token order inside a row — on the sample, `Asientos` (y=1768) sorted ahead of its own `N°`
   (y=1769).
6. **Narrow label matching**, inside the field's own column only. The `N°` marker tolerates `N`,
   `Nº`, `Nro`, and the observed corruptions `N?`, `N*`, `N?*`; the rest match by accent-stripped
   prefix. Broad fuzzy matching is deliberately avoided so `N° Cilindros` can never bind to
   `N° Ejes`.
7. **Dual-pass read.** Every value is read twice — once from the page token grid (with the
   colon-anchor correction), once by re-OCR'ing the isolated cell. Agreement means the value is
   corroborated; disagreement keeps the parseable reading and raises `LOW_CONFIDENCE_FIELD`. The
   two readings are never averaged or reconciled. When both parse but differ, the plausible
   reading wins, then the higher-confidence one.
8. **Normalise and validate.** `Decimal` throughout, never binary float. `2,89` and `2.89` both
   become `2.89` — punctuation is never stripped globally, so `2.89` cannot collapse to `289`.
   Values failing a plausibility check are returned **exactly as read** with `valid: false`.

---

## 3. API

### `GET /health`
Unauthenticated liveness probe.
```json
{ "status": "ok" }
```

### `GET /ready`
Unauthenticated readiness probe. Validates configuration and dependencies **without running OCR**:
the API key is set, Tesseract and the configured language data are installed, and Google
credentials resolve. Returns `503` when any check fails.

### `POST /api/v1/vehicles/extract`
Requires `X-API-Key`. Request:
```json
{ "plate": "A0A952", "drive_file_id": "14k4VNYTJf4CQnZTTdswNs5COtylfZ_bp" }
```

Response (abridged — all 12 fields are always present):
```json
{
  "status": "success",
  "request": { "plate": "A0A952", "normalized_plate": "A0A952", "drive_file_id": "14k4..." },
  "document": { "filename": "A0A952.pdf", "mime_type": "application/pdf", "pages": 1 },
  "processing": {
    "native_text_available": false,
    "ocr_used": true,
    "target_section_found": true,
    "render_scale": 1.95,
    "processing_time_ms": 4300
  },
  "fields": {
    "NUM_CILINDROS": { "value": 4, "status": "extracted", "ocr_confidence": 96.0, "valid": true, "source_label": "N° Cilindros" },
    "PESO_NETO":     { "value": 2.89, "status": "extracted", "ocr_confidence": 96.0, "valid": true, "source_label": "Peso Neto" },
    "NUM_EJES":      { "value": 2, "status": "extracted", "ocr_confidence": 96.0, "valid": true, "source_label": "N° Ejes" },
    "NUM_PARTIDA":   { "value": "52172133", "status": "extracted", "ocr_confidence": 96.0, "valid": true, "source_label": "N° Partida" },
    "TIPO_USO":      { "value": "Transporte interprovincial", "status": "extracted", "ocr_confidence": 96.0, "valid": true, "source_label": "Tipo Uso" }
  },
  "values": {
    "NUM_CILINDROS": 4, "PESO_NETO": 2.89, "PESO_BRUTO": 4.6,
    "NUM_ASIENTOS": 20, "NUM_PASAJEROS": 19, "NUM_EJES": 2, "NUM_RUEDAS": 6,
    "LONGITUD": 6.99, "ANCHO": 1.99, "ALTURA": 2.76,
    "NUM_PARTIDA": "52172133", "TIPO_USO": "Transporte interprovincial"
  },
  "warnings": []
}
```

`values` is a flat mirror of `fields[*].value`, so n8n can map a Sheets column straight from
`{{ $json.values.NUM_EJES }}` instead of walking nested objects.

### The 12 fields

| Field | Type | Label in the document |
| --- | --- | --- |
| `NUM_CILINDROS` | integer \| null | N° Cilindros |
| `PESO_NETO` | decimal \| null | Peso Neto |
| `PESO_BRUTO` | decimal \| null | Peso Bruto |
| `NUM_ASIENTOS` | integer \| null | N° Asientos |
| `NUM_PASAJEROS` | integer \| null | N° Pasajer. |
| `NUM_EJES` | integer \| null | N° Ejes |
| `NUM_RUEDAS` | integer \| null | N° Ruedas |
| `LONGITUD` | decimal \| null | Longitud |
| `ANCHO` | decimal \| null | Ancho |
| `ALTURA` | decimal \| null | Altura |
| `NUM_PARTIDA` | **string** \| null | N° Partida |
| `TIPO_USO` | string \| null | Tipo Uso |

`NUM_PARTIDA` is a string because it is an identifier, not a quantity — leading zeros must survive
and no arithmetic is ever done on it.

> **`CILINDRADA` is intentionally not part of this contract.** It is redacted (`##########`) in
> effectively every document, so a column that is always empty was dropped rather than shipped.
> The generic mask detection that would have covered it still guards all 12 fields above.

### Field status values

| `status` | Meaning | `value` | `valid` |
| --- | --- | --- | --- |
| `extracted` | Read from the document | the value | `true` / `false` |
| `unavailable` | Present but redacted or unreadable | `null` | `null` |
| `not_found` | Label or value could not be located | `null` | `null` |

`valid: false` means the value failed a plausibility check. **It is still returned exactly as
read** — nothing is silently repaired.

`ocr_confidence` is a real Tesseract confidence or `null`. It is `null` on the native-text path,
where no OCR ran. It is never fabricated.

### Warnings

Warnings never fail the request: `PLATE_MISMATCH`, `FIELD_NOT_FOUND`, `FIELD_UNAVAILABLE`,
`VALIDATION_FAILED`, `UNPARSEABLE_VALUE`, `LOW_CONFIDENCE_FIELD`, `SECTION_FALLBACK_USED`.

### Errors

```json
{ "status": "error", "error": { "code": "INVALID_DOCUMENT_TYPE", "message": "Expected a PDF document." } }
```

| Code | HTTP |
| --- | --- |
| `INVALID_REQUEST` | 422 |
| `UNAUTHORIZED` | 401 |
| `DRIVE_FILE_NOT_FOUND` | 404 |
| `DRIVE_PERMISSION_DENIED` | 403 |
| `INVALID_DOCUMENT_TYPE` | 415 |
| `FILE_TOO_LARGE` | 413 |
| `PDF_INVALID` | 422 |
| `TARGET_SECTION_NOT_FOUND` | 422 |
| `OCR_TIMEOUT` | 504 |
| `OCR_FAILED` | 500 |
| `DOCUMENT_PROCESSING_FAILED` | 500 |
| `INTERNAL_ERROR` | 500 |

Python tracebacks are never returned.

---

## 4. Running it

### Docker (recommended)

```bash
mkdir -p secrets
cp /path/to/service-account.json secrets/google-service-account.json   # never commit this
chmod 600 secrets/google-service-account.json

export API_KEY="$(openssl rand -hex 32)"
export N8N_NETWORK="$(docker network ls --filter name=n8n --format '{{.Name}}' | head -1)"

docker compose up --build -d
docker compose exec vehicle-ocr python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000/ready').read())"
```

`compose.yaml` attaches to an **existing external** n8n network rather than creating one; set
`N8N_NETWORK` to whatever `docker network ls` reports. The service is only `expose`d, not
published, so it stays on the private network — publish a port only if you need host access.

### Local development

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
sudo apt-get install -y tesseract-ocr tesseract-ocr-spa   # or: dnf install tesseract tesseract-langpack-spa

cp .env.example .env   # then set API_KEY
uvicorn app.main:app --reload --port 8000
```

### curl

```bash
curl -s http://localhost:8000/health
curl -s http://localhost:8000/ready

curl -s -X POST http://localhost:8000/api/v1/vehicles/extract \
  -H 'Content-Type: application/json' \
  -H "X-API-Key: $API_KEY" \
  -d '{"plate":"A0A952","drive_file_id":"14k4VNYTJf4CQnZTTdswNs5COtylfZ_bp"}'

# Just the values, ready for a Sheets row:
curl -s -X POST http://localhost:8000/api/v1/vehicles/extract \
  -H 'Content-Type: application/json' -H "X-API-Key: $API_KEY" \
  -d '{"plate":"A0A952","drive_file_id":"14k4..."}' | jq '.values'
```

---

## 5. n8n integration

**HTTP Request node**

| Setting | Value |
| --- | --- |
| Method | `POST` |
| URL | `http://vehicle-ocr:8000/api/v1/vehicles/extract` |
| Authentication | None (the API key is sent as a header) |
| Send Headers | on — `Content-Type: application/json`, `X-API-Key: <your key>` |
| Send Body | on — JSON |

Store the key as an n8n credential or environment variable rather than typing it into the node.

Body:
```json
{
  "plate": "={{ $json.plate }}",
  "drive_file_id": "={{ $json.drive_file_id }}"
}
```

Mapping into the Google Sheets node:
```text
NUM_CILINDROS   ={{ $json.values.NUM_CILINDROS }}
PESO_NETO       ={{ $json.values.PESO_NETO }}
PESO_BRUTO      ={{ $json.values.PESO_BRUTO }}
NUM_ASIENTOS    ={{ $json.values.NUM_ASIENTOS }}
NUM_PASAJEROS   ={{ $json.values.NUM_PASAJEROS }}
NUM_EJES        ={{ $json.values.NUM_EJES }}
NUM_RUEDAS      ={{ $json.values.NUM_RUEDAS }}
LONGITUD        ={{ $json.values.LONGITUD }}
ANCHO           ={{ $json.values.ANCHO }}
ALTURA          ={{ $json.values.ALTURA }}
NUM_PARTIDA     ={{ $json.values.NUM_PARTIDA }}
TIPO_USO        ={{ $json.values.TIPO_USO }}
```

To flag rows needing review:
```text
={{ $json.warnings.length > 0 ? $json.warnings.map(w => w.code).join(', ') : '' }}
```

Set the node's **Timeout** to at least 60000 ms — OCR takes a few seconds per document — and send
documents sequentially (batch size 1), since OCR is CPU-bound.

---

## 6. Security

- `POST /api/v1/vehicles/extract` requires `X-API-Key`, compared with `secrets.compare_digest`.
  An unset key **fails closed** — it never means "allow everyone". Health and readiness stay open.
- The service account is **read-only** (`drive.readonly`) and supports Shared Drives. The key is
  mounted read-only at `/run/secrets/google-service-account.json`; it is never committed, never
  copied into the image, never logged, and never returned by the API.
- Downloads are streamed to disk in chunks inside a `TemporaryDirectory()`, which is removed when
  the request finishes. **PDFs are never persisted** (there is a test asserting this).
- MIME type and size are checked from Drive metadata **before** the file is downloaded.
- Logs are structured JSON with a `request_id`, and redact API keys, credentials and OCR text.
  Full OCR text and tokens are returned only when `RETURN_DEBUG_DATA=true`, which defaults off.
- Keep the service on the private Docker network with n8n; do not publish it publicly.

---

## 7. Tests

```bash
pytest                      # 150 tests, ~30s (the golden test runs real OCR)
pytest -k "not Golden"      # fast subset, ~1s
```

| File | Covers |
| --- | --- |
| `test_api.py` | Golden regression through the real pipeline, auth, error codes, temp-file cleanup, debug gating |
| `test_vehicle_characteristics.py` | Field extraction, colon fusion, masks, label variants, validation |
| `test_section_detection.py` | Section bounds, dynamic columns, fallback geometry, row banding |
| `test_normalization.py` | Plates, decimals, mask detection, label folding |

The golden test asserts the reference PDF always yields exactly:

```text
NUM_CILINDROS 4      PESO_NETO 2.89   PESO_BRUTO 4.6    NUM_ASIENTOS 20
NUM_PASAJEROS 19     NUM_EJES 2       NUM_RUEDAS 6      LONGITUD 6.99
ANCHO 1.99           ALTURA 2.76      NUM_PARTIDA "52172133"
TIPO_USO "Transporte interprovincial"
```

Several tests are explicit regressions for bugs found while building this against the real
document — the `INSCRIPCION` footer collision, row-band ordering, colon fusion, and the label
normaliser that must not strip the letter `o`.

---

## 8. Validated against real documents

The extractor was run end-to-end against **20 real SUNARP boletas** pulled from Google Drive,
spanning Mercedes Benz, Volvo, Toyota, Asia, Foton and Agrale Modasa.

| Result | |
| --- | --- |
| Documents processed | 20 / 20 (HTTP 200) |
| Fields extracted | **239 / 240** |
| Implausible values returned | 0 |
| Average time | ~4.8 s per document |

Eight documents — at least one of every make — were additionally verified **by reading the
rendered PDF directly** and comparing field by field. All values matched.

The single missing field is `NUM_ASIENTOS` on `A0R951`, where the source PDF prints
`BLANCO VERDE ESMERALDA AMARILLO` directly on top of the `N° Asientos` label. The label is
illegible to a human reader too. It is reported as `not_found` with a warning rather than guessed.
That is the intended behaviour: one row flagged for review beats a plausible-looking wrong number.

Remaining warnings across the 20 documents are `LOW_CONFIDENCE_FIELD` (9, all resolved to the
correct value and confirmed visually), `LABEL_FUZZY_MATCH` (1, correct), and `FIELD_NOT_FOUND`
(1, the case above).

## 9. Scope

Intentionally **not** included: a database, queue, cache, Celery, Kafka, Kubernetes, an LLM, a
vector store, a frontend, and any write path to Google Sheets. n8n orchestrates; this service
extracts. OCR is CPU-bound, so documents are processed one at a time
(`MAX_CONCURRENT_EXTRACTIONS`).

## 10. Project layout

```text
app/
├── main.py                             app factory, error rendering, request-id middleware
├── config.py                           environment-driven settings
├── api/routes/{health,vehicles}.py     the three endpoints
├── models/extraction.py                request/response models, the 12-field contract
├── services/
│   ├── drive.py                        read-only Drive metadata + chunked download
│   ├── pdf.py                          validation, native text, adaptive rasterisation
│   ├── ocr.py                          Tesseract tokens with boxes; isolated cell re-reads
│   ├── section_detector.py             section bounds and dynamic column geometry
│   ├── tokens.py                       the spatial token type and page-source protocol
│   └── vehicle_characteristics.py      label matching, dual-pass read, masks, validation
├── core/{security,exceptions,logging}.py
└── utils/normalization.py              plates, decimals, mask detection
```
