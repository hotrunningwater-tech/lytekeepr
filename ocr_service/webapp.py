"""
Local MVP testing interface for the OCR service.

Run with:
    ./.venv/bin/python -m ocr_service.webapp

Then open http://127.0.0.1:5050 in a browser.

This is a thin web layer only. All OCR work happens through the public
ocr_document() function in ocr_service.service — nothing here reimplements
OCR-provider or PDF-conversion logic. This file's only job is to serve a
simple upload/test page, call ocr_document(), and serve back the files it
already saved.
"""

import re

from flask import Flask, jsonify, request, send_file

from .config import OCRServiceConfig
from .service import ocr_document

config = OCRServiceConfig.from_env()
STORAGE_ROOT = config.storage_root
JOBS_DIR = STORAGE_ROOT / "jobs"
JOBS_DIR.mkdir(parents=True, exist_ok=True)

JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")
PAGE_FILENAME_RE = re.compile(r"^page-\d{3}\.png$")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024  # 100 MB


def _job_dir(job_id: str):
    if not JOB_ID_RE.match(job_id):
        raise ValueError("Invalid job id")
    return JOBS_DIR / job_id


@app.get("/")
def index():
    return INDEX_HTML.replace("__STORAGE_ROOT__", str(STORAGE_ROOT))


@app.post("/api/ocr")
def api_ocr():
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    upload = request.files["file"]
    filename = upload.filename or "upload"
    file_bytes = upload.read()

    # The only OCR-related call in this file. Splitting into pages,
    # converting to PNG, calling the provider, and saving everything all
    # happen inside ocr_document().
    result = ocr_document(file_bytes, filename=filename, config=config)

    response = result.to_dict()
    response["links"] = {
        "original": f"/api/jobs/{result.job_id}/original",
        "text": f"/api/jobs/{result.job_id}/text",
        "json": f"/api/jobs/{result.job_id}/json",
        "pages": [
            f"/api/jobs/{result.job_id}/pages/page-{i:03d}.png"
            for i in range(1, result.page_count + 1)
        ],
        "folder": str(_job_dir(result.job_id)),
    }
    return jsonify(response)


@app.get("/api/jobs/<job_id>/original")
def get_original(job_id):
    original_dir = _job_dir(job_id) / "original"
    files = list(original_dir.glob("*"))
    if not files:
        return jsonify({"error": "Not found"}), 404
    return send_file(files[0], as_attachment=True, download_name=files[0].name)


@app.get("/api/jobs/<job_id>/text")
def get_text(job_id):
    path = _job_dir(job_id) / "results" / "extracted-text.txt"
    if not path.exists():
        return jsonify({"error": "Not found"}), 404
    return send_file(path, as_attachment=True, download_name="extracted-text.txt")


@app.get("/api/jobs/<job_id>/json")
def get_json(job_id):
    path = _job_dir(job_id) / "results" / "result.json"
    if not path.exists():
        return jsonify({"error": "Not found"}), 404
    return send_file(path, as_attachment=True, download_name="result.json")


@app.get("/api/jobs/<job_id>/pages/<page_filename>")
def get_page(job_id, page_filename):
    if not PAGE_FILENAME_RE.match(page_filename):
        return jsonify({"error": "Invalid page filename"}), 400
    path = _job_dir(job_id) / "pages" / page_filename
    if not path.exists():
        return jsonify({"error": "Not found"}), 404
    return send_file(path, download_name=page_filename)


INDEX_HTML = """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>OCR Service — Local Test</title>
<style>
  body { font-family: -apple-system, sans-serif; max-width: 820px; margin: 40px auto; padding: 0 20px; color: #222; }
  h1 { font-size: 20px; margin-bottom: 4px; }
  .format-note { color: #666; font-size: 13px; margin-bottom: 18px; }
  #dropzone { border: 2px dashed #999; border-radius: 10px; padding: 40px; text-align: center; cursor: pointer; color: #666; }
  #dropzone.drag { border-color: #333; background: #f5f5f5; }
  #fileinfo { margin: 14px 0; font-size: 14px; }
  #processBtn { padding: 10px 20px; font-size: 14px; cursor: pointer; }
  #processBtn:disabled { opacity: 0.5; cursor: default; }
  #status { margin: 14px 0; font-weight: 600; }
  .status-processing { color: #b8860b; }
  .status-success { color: #1a7f37; }
  .status-partial { color: #b8860b; }
  .status-failed { color: #c0392b; }
  #meta { font-size: 13px; color: #444; margin: 8px 0; }
  #links a, #links button { margin-right: 10px; }
  #pageLinks a { margin-right: 8px; font-size: 12px; }
  .page { border: 1px solid #ddd; border-radius: 6px; padding: 10px 14px; margin: 8px 0; }
  .page h4 { margin: 0 0 6px; font-size: 13px; color: #555; }
  pre { white-space: pre-wrap; word-wrap: break-word; background: #fafafa; padding: 10px; border-radius: 6px; }
  #resultArea { display: none; margin-top: 24px; }
  #storageRoot { font-size: 12px; color: #888; margin-top: 30px; }
</style>
</head>
<body>

<h1>OCR Service — Local Test Interface</h1>
<p class="format-note">PDF is currently the only supported format.</p>
<p>Drop a PDF, review it, then explicitly process it. Nothing is sent until you click Process.</p>

<div id="dropzone">Drop a PDF here, or click to choose a file</div>
<input type="file" id="fileInput" style="display:none" accept=".pdf">

<div id="fileinfo"></div>
<button id="processBtn" disabled>Process Document</button>

<div id="status"></div>

<div id="resultArea">
  <h3>Result</h3>
  <div id="meta"></div>
  <div id="links"></div>
  <div id="pageLinks"></div>
  <h4>Full extracted text</h4>
  <pre id="fullText"></pre>
  <h4>Page-by-page</h4>
  <div id="pages"></div>
  <br>
  <button id="resetBtn">Process Another Document</button>
</div>

<div id="storageRoot">Storage root: __STORAGE_ROOT__</div>

<script>
const dropzone = document.getElementById('dropzone');
const fileInput = document.getElementById('fileInput');
const fileinfo = document.getElementById('fileinfo');
const processBtn = document.getElementById('processBtn');
const statusEl = document.getElementById('status');
const resultArea = document.getElementById('resultArea');
const metaEl = document.getElementById('meta');
const linksEl = document.getElementById('links');
const pageLinksEl = document.getElementById('pageLinks');
const fullTextEl = document.getElementById('fullText');
const pagesEl = document.getElementById('pages');
const resetBtn = document.getElementById('resetBtn');

let selectedFile = null;

function humanSize(bytes) {
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024*1024) return (bytes/1024).toFixed(1) + ' KB';
  return (bytes/1024/1024).toFixed(2) + ' MB';
}

function setFile(file) {
  selectedFile = file;
  fileinfo.innerHTML = `<strong>${file.name}</strong> &middot; ${humanSize(file.size)}`;
  processBtn.disabled = false;
  statusEl.textContent = '';
  resultArea.style.display = 'none';
}

dropzone.addEventListener('click', () => fileInput.click());
dropzone.addEventListener('dragover', (e) => { e.preventDefault(); dropzone.classList.add('drag'); });
dropzone.addEventListener('dragleave', () => dropzone.classList.remove('drag'));
dropzone.addEventListener('drop', (e) => {
  e.preventDefault();
  dropzone.classList.remove('drag');
  if (e.dataTransfer.files.length) setFile(e.dataTransfer.files[0]);
});
fileInput.addEventListener('change', () => {
  if (fileInput.files.length) setFile(fileInput.files[0]);
});

processBtn.addEventListener('click', async () => {
  if (!selectedFile) return;
  processBtn.disabled = true;
  statusEl.textContent = 'Processing…';
  statusEl.className = 'status-processing';
  resultArea.style.display = 'none';

  const formData = new FormData();
  formData.append('file', selectedFile);

  try {
    const res = await fetch('/api/ocr', { method: 'POST', body: formData });
    const data = await res.json();

    if (!res.ok) {
      statusEl.textContent = 'Failed: ' + (data.error || 'unknown error');
      statusEl.className = 'status-failed';
      processBtn.disabled = false;
      return;
    }

    statusEl.textContent = 'Status: ' + data.status;
    statusEl.className = 'status-' + data.status;

    if (data.status === 'failed' && data.errors && data.errors.length) {
      statusEl.textContent += ' — ' + data.errors.join('; ');
    }

    metaEl.innerHTML = `Job ID: <strong>${data.job_id}</strong> &middot; Pages: <strong>${data.page_count}</strong>`;

    linksEl.innerHTML = `
      <a href="${data.links.original}" target="_blank">Download original PDF</a>
      <a href="${data.links.text}" target="_blank">Download extracted-text.txt</a>
      <a href="${data.links.json}" target="_blank">Download result.json</a>
      <div style="margin-top:6px;color:#666;font-size:12px;">${data.links.folder}</div>
    `;

    pageLinksEl.innerHTML = 'Page images: ' + (data.links.pages || [])
      .map((url, i) => `<a href="${url}" target="_blank">page-${String(i+1).padStart(3,'0')}.png</a>`)
      .join('');

    fullTextEl.textContent = data.full_text || '(no text extracted)';

    pagesEl.innerHTML = '';
    (data.pages || []).forEach(p => {
      const div = document.createElement('div');
      div.className = 'page';
      div.innerHTML = `<h4>Page ${p.page_number}</h4><pre>${(p.text || '').replace(/</g,'&lt;')}</pre>`;
      pagesEl.appendChild(div);
    });

    resultArea.style.display = 'block';
    processBtn.disabled = false;
  } catch (err) {
    statusEl.textContent = 'Failed: ' + err;
    statusEl.className = 'status-failed';
    processBtn.disabled = false;
  }
});

resetBtn.addEventListener('click', () => {
  selectedFile = null;
  fileInput.value = '';
  fileinfo.textContent = '';
  processBtn.disabled = true;
  statusEl.textContent = '';
  resultArea.style.display = 'none';
});
</script>

</body>
</html>
"""


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5050, debug=False)
