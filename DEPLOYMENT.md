# Deployment guide — server + n8n

How to run this service next to n8n on `platform-dev-01` and drive ~6000 plates through it.

The service stays **private on the Docker network**. n8n reaches it at `http://vehicle-ocr:8000`.
Do **not** add it to Nginx Proxy Manager and do not publish a host port — nothing outside the
Docker network needs to reach it.

---

## Part 1 — Server setup

All commands run from `/srv/drtc/sunarp-vehicle-ocr-api`.

### 1.1 Confirm the checkout is current

```bash
git log --oneline -1
grep -q LABEL_FUZZY_MATCH app/models/extraction.py && echo "fixes present" || echo "OLD CODE — run: git pull"
```

`LABEL_FUZZY_MATCH` only exists in the validated version.

### 1.2 Find the Docker network n8n uses

```bash
docker ps --format '{{.Names}}\t{{.Image}}' | grep -i n8n
docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{"\n"}}{{end}}' <n8n_container>
```

NPM already reaches n8n as `http://n8n:5678`, so NPM and n8n share a network — join that one.

### 1.3 Create `.env`

```bash
cp .env.example .env
python3 -c "import secrets; print(secrets.token_hex(32))"   # copy this
nano .env                                                   # set API_KEY=<value>
echo 'N8N_NETWORK=<network from 1.2>' >> .env
chmod 600 .env
```

Keep that API key — n8n needs the identical value in Part 3.

### 1.4 Install the service-account key

From your workstation:

```bash
scp secrets/google-service-account.json \
    guillermo@platform-dev-01:/srv/drtc/sunarp-vehicle-ocr-api/secrets/
```

Then **on the server**:

```bash
mkdir -p secrets
sudo chown 1001:1001 secrets/google-service-account.json
sudo chmod 400 secrets/google-service-account.json
```

> **The `chown` is not optional.** The container runs as non-root uid **1001** (`app`, created in
> the Dockerfile). A key owned by `guillermo` with mode `600` is unreadable inside the container
> and `/ready` will report `credentials unavailable`. Owning it `1001:1001` mode `400` makes it
> readable by the service and by nobody else on the host.

### 1.5 Build and start

```bash
docker compose config >/dev/null && echo "compose OK"
docker compose up --build -d
docker compose logs -f --tail=50 vehicle-ocr   # Ctrl-C at "Application startup complete"
```

### 1.6 Verify from inside the n8n container

This is the check that matters — it proves n8n can resolve and reach the service:

```bash
docker exec -it <n8n_container> sh -c \
  "wget -qO- http://vehicle-ocr:8000/health; echo; wget -qO- http://vehicle-ocr:8000/ready"
```

Expect `{"status":"ok"}` then `"status":"ready"` with all three checks `ok`
(`api_key_configured`, `tesseract`, `google_credentials`). Anything else — see Troubleshooting.

### 1.7 One real extraction

```bash
docker compose exec vehicle-ocr python check_plate.py 14k4VNYTJf4CQnZTTdswNs5COtylfZ_bp --table-only
```

Expect `NUM_EJES 2`, `PESO_NETO 2.89`, `TIPO_USO Transporte interprovincial`, no warnings. This
confirms Drive access, OCR and the Spanish language pack all work on the server.

---

## Part 2 — Prepare the Drive-IDs spreadsheet

Results go into the **Drive-IDs** spreadsheet first, because plate and Drive ID already share a
row there — no cross-document join needed. The vehicle-records spreadsheet stays untouched until
this is proven (Part 6).

Add these **14 columns** to the right of the existing ones, spelled exactly — n8n matches columns
by header text:

```
NUM_CILINDROS  PESO_NETO  PESO_BRUTO  NUM_ASIENTOS  NUM_PASAJEROS  NUM_EJES  NUM_RUEDAS
LONGITUD  ANCHO  ALTURA  NUM_PARTIDA  TIPO_USO  ESTADO  AVISOS
```

`ESTADO` drives resumability and must start **empty** on every unprocessed row:

| `ESTADO` | Meaning |
| --- | --- |
| *(empty)* | Not processed yet — the workflow will pick it up |
| `OK` | All 12 fields extracted, no warnings |
| `REVISAR` | Extracted, but something is flagged — see `AVISOS` |
| `ERROR:<CODE>` | Request failed, e.g. `ERROR:DRIVE_FILE_NOT_FOUND` |

Format `NUM_PARTIDA` as **plain text** first, or Sheets turns `50081532` into `5.01E+07`.

Note: the n8n Google Sheets credential is **separate** from the Drive service account this API
uses. Make sure that credential can write to this spreadsheet.

---

## Part 3 — The n8n workflow

An importable starting point is in [`n8n/sunarp-extraction-workflow.json`](n8n/sunarp-extraction-workflow.json).
Import it, then set credentials and pick the spreadsheet on each Google Sheets node.

### Node chain

```
Trigger → Get rows → Filter (ESTADO empty) → Limit → Loop Over Items
                                                        ↓
                                                  HTTP Request
                                                   ↓ ok       ↓ error
                                            Update (OK)   Update (ERROR)
                                                   └─────┬─────┘
                                                         └──→ back to Loop
```

**1. Trigger** — `Manual` while testing; `Schedule Trigger` (every 20 min) for the real run.

**2. Get row(s) in sheet** — the Drive-IDs spreadsheet, no filter. Returns all rows in one API
call, each carrying `row_number`.

**3. Filter** — keep only pending rows: `{{ $json.ESTADO }}` **is empty**.

**4. Limit** — `10` for the smoke test, then `200` (≈17 min per execution).

**5. Loop Over Items** — **Batch Size 1**. OCR is CPU-bound and the service handles one document
at a time; parallel requests just queue.

**6. HTTP Request**

| Setting | Value |
| --- | --- |
| Method | `POST` |
| URL | `http://vehicle-ocr:8000/api/v1/vehicles/extract` |
| Authentication | Generic Credential → **Header Auth** |
| Send Body | on, JSON |
| Timeout | `120000` |
| Retry On Fail | on — 2 tries, 5000 ms |
| On Error | **Continue (using error output)** |

Create the Header Auth credential once: **Name** `X-API-Key`, **Value** = the key from `.env`.
That keeps the secret out of the workflow JSON.

Body:

```json
{
  "plate": "={{ $json.PLACA }}",
  "drive_file_id": "={{ $json.ID_DRIVE }}"
}
```

> Adjust `PLACA` / `ID_DRIVE` to your real header names. If you keep the existing `Edit Fields`
> node, the field is `id_drive`, so it becomes `"drive_file_id": "={{ $json.id_drive }}"`. The
> API field is `drive_file_id` — this rename is the easy thing to miss.

**7a. Update row(s) — success branch.** Match on **`row_number`** (unique, unlike a plate that
might repeat):

```
row_number      ={{ $('Loop Over Items').item.json.row_number }}
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
ESTADO          ={{ $json.warnings.length > 0 ? "REVISAR" : "OK" }}
AVISOS          ={{ $json.warnings.map(w => w.code).join(", ") }}
```

**7b. Update row(s) — error branch.** Same document and match column:

```
row_number  ={{ $('Loop Over Items').item.json.row_number }}
ESTADO      ={{ "ERROR:" + ($json.error?.error?.code || $json.error?.code || "REQUEST_FAILED") }}
AVISOS      ={{ $json.error?.error?.message || $json.error?.message || "" }}
```

**8.** Wire **both** update nodes back into `Loop Over Items`.

---

## Part 4 — Smoke test before the real run

1. Set `Limit` to **10**, run manually.
2. Check the sheet: 10 rows filled, `ESTADO` `OK` or `REVISAR`, no blanks left behind.
3. Open one Drive PDF and compare `NUM_EJES`, `PESO_NETO`, `NUM_PARTIDA` by eye.
4. Put a bad Drive ID in a spare test row — confirm it lands as `ERROR:DRIVE_FILE_NOT_FOUND`
   and the batch keeps going.
5. Re-run — already-processed rows must be skipped. That is resumability working.

**Expect some `REVISAR`.** Across the 20-document validation sample about 10 % of documents
carried a `LOW_CONFIDENCE_FIELD` warning, and **every one of those values was still correct** when
checked against the PDF. `REVISAR` means "worth a glance", not "wrong".

---

## Part 5 — The full run

Set `Limit` to `200` and switch to a `Schedule Trigger` every 20 minutes. Runs take ~17 min so
they never overlap; ~30 executions cover 6000 rows in roughly 10 hours, unattended and
restart-safe.

```bash
docker compose logs -f vehicle-ocr | grep request_completed    # one JSON line per document
docker stats --no-stream $(docker compose ps -q vehicle-ocr)   # ~1 core, a few hundred MB
```

Progress is visible in the sheet — count non-empty `ESTADO`. When the Filter yields zero rows,
you're done; sort by `ESTADO` to review `REVISAR` and `ERROR` rows.

**Turn the Schedule Trigger off when finished**, or it keeps waking to find nothing.

---

## Part 6 — Later: the vehicle-records spreadsheet

Once the above is proven, copy the finished values across. Different document, so this is a
second Google Sheets node matching on `PLACA` instead of `row_number`:

- `Get row(s)` from Drive-IDs where `ESTADO` is `OK` or `REVISAR`
- `Update row(s)` on the records document, **Column to match on: `PLACA`**, same 12 columns

Confirm `PLACA` is unique in the records sheet first — a duplicate plate would update only the
first match.

---

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `/ready` → `credentials unavailable` | Key unreadable by uid 1001 | `sudo chown 1001:1001 secrets/google-service-account.json` |
| n8n: `ECONNREFUSED` / `ENOTFOUND vehicle-ocr` | Not on the same Docker network | Fix `N8N_NETWORK` in `.env`, `docker compose up -d` |
| Every request `401 UNAUTHORIZED` | Key mismatch | `.env` `API_KEY` must equal the Header Auth credential value exactly |
| `403 DRIVE_PERMISSION_DENIED` | File not shared with the service account | Share the Drive folder with the service-account email |
| `415 INVALID_DOCUMENT_TYPE` | Row points at a non-PDF | Check that Drive ID |
| `NUM_PARTIDA` shows `5.01E+07` | Sheets auto-formatting | Format the column as plain text |
| Many `REVISAR` | Normal | Warnings flag rows worth a glance; values were correct in every sampled case |
| Sheets node can't see new columns | Cached schema | Click the refresh icon on the column mapping |
