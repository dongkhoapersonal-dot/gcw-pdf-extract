import os
import re
import time
import uuid
import glob
import gc
import threading

from flask import Flask, request, jsonify
import pdfplumber

app = Flask(__name__)

# Shared secret so random people on the internet can't hit this endpoint.
# Set API_KEY as an environment variable on Render; n8n sends it in the
# "X-API-Key" header.
API_KEY = os.environ.get("API_KEY", "changeme")

JOB_DIR = "/tmp/pdf_jobs"
os.makedirs(JOB_DIR, exist_ok=True)
JOB_TTL_SECONDS = 3600  # cached PDFs / finished jobs older than this get cleaned up

# In-memory job registry. A background thread processes the whole PDF once
# (opened a single time) instead of n8n calling us once per page-range --
# re-opening a 590-page PDF for every small chunk was the real bottleneck,
# not the page-extraction work itself.
JOBS = {}
JOBS_LOCK = threading.Lock()


def cleanup_old_jobs():
    now = time.time()
    for path in glob.glob(os.path.join(JOB_DIR, "*.pdf")):
        try:
            if now - os.path.getmtime(path) > JOB_TTL_SECONDS:
                os.remove(path)
        except OSError:
            pass
    with JOBS_LOCK:
        stale = [jid for jid, j in JOBS.items() if now - j.get("created_at", now) > JOB_TTL_SECONDS]
        for jid in stale:
            JOBS.pop(jid, None)


def clean_cell(value):
    if value is None:
        return ""
    value = value.replace("\n", " ").strip()
    value = re.sub(r"\s+", " ", value)
    return value


def clean_number(value):
    """Numbers sometimes get a stray internal space from pdfplumber's
    column-boundary detection, e.g. '5 7.971.868.654' -> '57.971.868.654'."""
    if value is None:
        return ""
    v = value.strip()
    if re.fullmatch(r"[\d.,\s]+", v):
        v = v.replace(" ", "")
    return v


def looks_like_header(row):
    # Match the STT column exactly (not substring!) -- a company name like
    # "DASTTECH" contains "STT" as a substring and would false-positive
    # against a looser check.
    return clean_cell(row[0]) == "STT"


def check_auth():
    return request.headers.get("X-API-Key") == API_KEY


def process_pdf(job_id, path):
    rows = []
    skipped_pages = []
    try:
        with pdfplumber.open(path) as pdf:
            total_pages = len(pdf.pages)
            with JOBS_LOCK:
                JOBS[job_id]["total_pages"] = total_pages

            for page_index, page in enumerate(pdf.pages):
                tables = page.extract_tables()
                if not tables:
                    skipped_pages.append(page_index + 1)
                else:
                    table = tables[0]
                    for raw_row in table:
                        if not raw_row or len(raw_row) < 9:
                            continue
                        if looks_like_header(raw_row):
                            continue
                        stt_raw = clean_cell(raw_row[0])
                        if not stt_raw or not stt_raw.isdigit():
                            continue
                        rows.append({
                            "STT": int(stt_raw),
                            "BHXH_co_so_quan_ly": clean_cell(raw_row[1]),
                            "Ma_don_vi": clean_cell(raw_row[2]),
                            "Ten_don_vi": clean_cell(raw_row[3]),
                            "Dia_chi": clean_cell(raw_row[4]),
                            "So_lao_dong": clean_number(clean_cell(raw_row[5])),
                            "So_thang_no": clean_number(clean_cell(raw_row[6])),
                            "Ty_le_no": clean_number(clean_cell(raw_row[7])),
                            "So_tien_cham_dong": clean_number(clean_cell(raw_row[8])),
                        })

                page.flush_cache()

                with JOBS_LOCK:
                    JOBS[job_id]["pages_done"] = page_index + 1

                if (page_index + 1) % 15 == 0:
                    gc.collect()

        with JOBS_LOCK:
            JOBS[job_id]["status"] = "done"
            JOBS[job_id]["rows"] = rows
            JOBS[job_id]["skipped_pages"] = skipped_pages

    except Exception as exc:  # noqa: BLE001
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "error"
            JOBS[job_id]["error"] = str(exc)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
        gc.collect()


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/upload", methods=["POST"])
def upload():
    if not check_auth():
        return jsonify({"error": "unauthorized"}), 401

    cleanup_old_jobs()

    if "file" not in request.files:
        return jsonify({"error": "missing 'file' in form-data"}), 400

    pdf_bytes = request.files["file"].read()
    if not pdf_bytes:
        return jsonify({"error": "empty file"}), 400

    job_id = uuid.uuid4().hex
    path = os.path.join(JOB_DIR, f"{job_id}.pdf")
    with open(path, "wb") as f:
        f.write(pdf_bytes)

    with JOBS_LOCK:
        JOBS[job_id] = {
            "status": "processing",
            "pages_done": 0,
            "total_pages": None,
            "rows": None,
            "created_at": time.time(),
        }

    thread = threading.Thread(target=process_pdf, args=(job_id, path), daemon=True)
    thread.start()

    return jsonify({"job_id": job_id, "status": "processing"})


@app.route("/status/<job_id>", methods=["GET"])
def status(job_id):
    if not check_auth():
        return jsonify({"error": "unauthorized"}), 401

    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "unknown or expired job_id"}), 404
        resp = {
            "status": job["status"],
            "pages_done": job["pages_done"],
            "total_pages": job.get("total_pages"),
        }
        if job["status"] == "done":
            resp["count"] = len(job["rows"])
            resp["skipped_pages"] = job.get("skipped_pages", [])
            resp["rows"] = job["rows"]
        elif job["status"] == "error":
            resp["error"] = job.get("error")

    return jsonify(resp)


@app.route("/job/<job_id>", methods=["DELETE"])
def delete_job(job_id):
    if not check_auth():
        return jsonify({"error": "unauthorized"}), 401
    path = os.path.join(JOB_DIR, f"{job_id}.pdf")
    if os.path.exists(path):
        os.remove(path)
    with JOBS_LOCK:
        JOBS.pop(job_id, None)
    return jsonify({"deleted": True})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
