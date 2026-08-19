# GCW PDF Table Extract Service

Small stateless Flask service that extracts the "danh sách đơn vị chậm đóng BHXH"
table from a PDF using pdfplumber (real table/column detection, not raw text),
so n8n never has to run this through an LLM.

## Endpoints
- `GET /health` — liveness check
- `POST /upload` (multipart field `file`) — caches the PDF server-side for 1 hour,
  returns `{job_id, page_count}`
- `GET /extract/<job_id>?start=1&end=25` — extracts rows for that 1-indexed page
  range, returns `{rows: [...], count, total_pages}`
- `DELETE /job/<job_id>` — deletes the cached PDF early

All endpoints (except /health) require header `X-API-Key: <value of API_KEY env var>`.

## Deploy on Render (free tier)
1. Push this folder to a new GitHub repo (or Render can deploy from a public repo).
2. On Render: New -> Web Service -> connect the repo -> Render auto-detects
   `render.yaml`. Click Deploy.
3. Render generates a random `API_KEY` value automatically (see render.yaml).
   Copy it from Render's Environment tab after first deploy.
4. Your service URL will look like `https://gcw-pdf-extract.onrender.com`.
5. Free tier sleeps after 15 min idle; first request after sleeping takes
   ~30-50s to wake up (n8n should just wait for the response, no action needed).
