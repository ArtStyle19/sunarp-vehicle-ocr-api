# n8n workflow

`sunarp-extraction-workflow.json` is an importable starting point for the resumable extraction
run. See [`../DEPLOYMENT.md`](../DEPLOYMENT.md) for the full guide.

## Import

n8n → **Workflows** → **Import from File** (or paste the JSON into a new canvas).

## Then finish these four things in the UI

The JSON cannot carry credentials or your spreadsheet, so after importing:

1. **Google Sheets credential** — set it on all three Sheets nodes
   (`Get rows`, `Write result`, `Write error`).
2. **Spreadsheet + tab** — replace `PUT_DRIVE_IDS_SPREADSHEET_ID_HERE` and the `Hoja 1` tab name
   on those same three nodes. Easiest is to switch the selector to *From list* and pick it.
3. **Header Auth credential** on `Extract characteristics` — **Name** `X-API-Key`,
   **Value** = the `API_KEY` from the server's `.env`.
4. **Column mapping** — open `Write result` and click the refresh icon on the column list so n8n
   loads your real headers, then confirm **Column to match on** is `row_number`. Do the same on
   `Write error`.

## Check the field names match your sheet

The HTTP node body reads `PLACA` and `ID_DRIVE`:

```json
{ "plate": "={{ $json.PLACA }}", "drive_file_id": "={{ $json.ID_DRIVE }}" }
```

Change those two to your actual column headers. Note the API field is **`drive_file_id`** — if you
feed it from an `Edit Fields` node that emits `id_drive`, it becomes
`"drive_file_id": "={{ $json.id_drive }}"`.

## Before the real run

- `Limit` is set to **10** for the smoke test. Raise it to **200** afterwards.
- `Loop Over Items` must stay at **Batch Size 1** — OCR is CPU-bound and the service handles one
  document at a time.
- Swap the manual trigger for a **Schedule Trigger** every 20 minutes for the unattended run, and
  turn it off when the sheet is fully processed.

## Version note

Written against n8n 1.x node versions (`googleSheets` 4.5, `httpRequest` 4.2, `filter` 2.2,
`splitInBatches` 3). If your n8n is older and a node imports blank, build that one node by hand
from the table in `DEPLOYMENT.md` — the expressions there are the same.
