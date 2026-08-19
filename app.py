import os
import re
import time
import uuid
import glob

from flask import Flask, request, jsonify
import pdfplumber

app = Flask(__name__)

# Shared secret so random people on the internet can't hit this endpoint.
# Set API_KEY as an environment variable on Render; n8n sends it in the
# "X-API-Key" header.
API_KEY = os.environ.get("API_KEY", "changeme")

JOB_DIR = "/tmp/pdf_jobs"
os.makedirs(JOB_DIR, exist_ok=True)
JOB_TTL_SECONDS = 3600  # cached PDFs older than this get cleaned up


def cleanup_old_jobs():
    now = time.time()
    for path in glob.glob(os.path.join(JOB_DIR, "*.pdf")):
        try:
            if now - os.path.getmtime(path) > JOB_TTL_SECONDS:
                os.remove(path)
        except OSError:
            pass


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

    try:
        with pdfplumber.open(path) as pdf:
            page_count = len(pdf.pages)
    except Exception as exc:  # noqa: BLE001
        os.remove(path)
        return jsonify({"error": f"invalid pdf: {exc}"}), 400

    return jsonify({"job_id": job_id, "page_count": page_count})


@app.route("/extract/<job_id>", methods=["GET"])
def extract(job_id):
    if not check_auth():
        return jsonify({"error": "unauthorized"}), 401

    path = os.path.join(JOB_DIR, f"{job_id}.pdf")
    if not os.path.exists(path):
        return jsonify({"error": "unknown or expired job_id"}), 404

    start = int(request.args.get("start", 1))
    end = int(request.args.get("end", start))

    rows = []
    skipped_pages = []

    with pdfplumber.open(path) as pdf:
        total_pages = len(pdf.pages)
        end = min(end, total_pages)
        for page_index in range(start - 1, end):
            page = pdf.pages[page_index]
            tables = page.extract_tables()
            if not tables:
                skipped_pages.append(page_index + 1)
                continue
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

    return jsonify({
        "job_id": job_id,
        "start": start,
        "end": end,
        "total_pages": total_pages,
        "count": len(rows),
        "skipped_pages": skipped_pages,
        "rows": rows,
    })


@app.route("/job/<job_id>", methods=["DELETE"])
def delete_job(job_id):
    if not check_auth():
        return jsonify({"error": "unauthorized"}), 401
    path = os.path.join(JOB_DIR, f"{job_id}.pdf")
    if os.path.exists(path):
        os.remove(path)
    return jsonify({"deleted": True})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
