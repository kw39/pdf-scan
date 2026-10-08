"""Regression tests for pattern detection, export options and session handling."""
import io
import json
import threading
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer
import pymupdf as fitz
import pytest
from PIL import Image
from pdfscan.engine import (apply, detect, hkid_valid, luhn_card, taiwan_id_valid, china_id_valid,
                            validate_output)
from pdfscan.demo import create_demo
from pdfscan.server import App, make_handler, download_name, disposition, MAX_JOBS

def make_pdf(path, lines):
    with fitz.open() as doc:
        page = doc.new_page()
        for i, text in enumerate(lines):
            page.insert_text((50, 80 + 30 * i), text, fontsize=12)
        doc.save(path)
    return path

def found(plan, label=None):
    return [d["matched"] for p in plan["pages"] for d in p["detections"]
            if label is None or d["rule"] == "pattern:" + label]

def test_phone_numbers_written_with_spaces(tmp_path):
    path = make_pdf(tmp_path / "phones.pdf", ["Call +852 2345 6789 or (02) 2712-3456 now",
                                              "Meeting on 2026-10-06 in room 12"])
    plan = detect(path, {"phone": True, "ocr": False})
    assert "+852 2345 6789" in found(plan, "phone")
    assert any("2712-3456" in m for m in found(plan, "phone"))
    assert not any("2026" in m for m in found(plan, "phone"))

def test_card_numbers_need_a_valid_checksum(tmp_path):
    path = make_pdf(tmp_path / "cards.pdf", ["Visa 4111 1111 1111 1111 approved",
                                             "Invoice 1234 5678 9012 3456 is not a card"])
    plan = detect(path, {"cards": True, "phone": True, "ocr": False})
    assert found(plan, "card") == ["4111 1111 1111 1111"]
    assert "4111 1111 1111 1111" not in found(plan, "phone")

def test_identity_numbers_need_a_valid_checksum(tmp_path):
    path = make_pdf(tmp_path / "ids.pdf", ["HKID A123456(3) and bad B123456(1)",
                                           "Taiwan A123456789 China 11010519491231002X",
                                           "SSN 123-45-6789 invalid 000-12-3456"])
    plan = detect(path, {"ids": True, "ocr": False})
    matched = " ".join(found(plan, "id"))
    for value in ("A123456(3)", "A123456789", "11010519491231002X", "123-45-6789"):
        assert value in matched
    assert "B123456" not in matched and "000-12" not in matched

def test_checksum_helpers():
    assert hkid_valid("A1234563") and not hkid_valid("A1234561")
    assert taiwan_id_valid("A123456789") and not taiwan_id_valid("A123456788")
    assert china_id_valid("11010519491231002X") and not china_id_valid("110105194912310021")
    assert luhn_card("4111-1111-1111-1111") and not luhn_card("1111 1111 1111 1111")

def test_patterns_are_opt_in(tmp_path):
    path = make_pdf(tmp_path / "quiet.pdf", ["Visa 4111 1111 1111 1111 +852 2345 6789"])
    assert found(detect(path, {"ocr": False})) == []

@pytest.fixture
def document(tmp_path):
    path = tmp_path / "synthetic.pdf"
    create_demo(path)
    return path

def test_black_fill_and_compact_quality(document, tmp_path):
    plan = detect(document, {"text": ["NORTHSTAR STUDIO"], "ocr": False})
    result = apply(document, plan, {"output": {"fill": "black", "quality": "compact"}}, tmp_path / "out.pdf")
    assert result["output"]["fill"] == "black" and result["imageOnly"]
    rect = plan["pages"][0]["detections"][0]["rect"]
    with fitz.open(tmp_path / "out.pdf") as out:
        assert out[0].get_images()[0][-1] != "" and not out[0].get_text().strip()
        im = Image.open(io.BytesIO(out[0].get_pixmap(dpi=100, alpha=False).tobytes("png"))).convert("L")
        cx, cy = int((rect[0] + rect[2]) / 2 * im.width), int((rect[1] + rect[3]) / 2 * im.height)
        assert im.getpixel((cx, cy)) < 40

def test_invalid_output_options():
    for bad in ({"fill": "red"}, {"quality": "max"}, {"searchable": "yes"}, {"other": 1}, []):
        with pytest.raises(ValueError):
            validate_output(bad)

def test_searchable_output_and_residual_check(document, tmp_path):
    tessdata = __import__("pathlib").Path(__file__).resolve().parent.parent / ".runtime" / "tessdata"
    # Without OCR, the scanned page keeps its target; the export check reports it.
    plan = detect(document, {"text": ["NORTHSTAR STUDIO"], "ocr": False})
    result = apply(document, plan, {"output": {"searchable": True}}, tmp_path / "missed.pdf", tessdata=tessdata)
    assert [r["page"] for r in result["residual"]] == [2]
    plan = detect(document, {"text": ["NORTHSTAR STUDIO"], "ocr": True}, tessdata=tessdata)
    result = apply(document, plan, {"output": {"searchable": True}}, tmp_path / "out.pdf", tessdata=tessdata)
    assert not result["imageOnly"] and result["residual"] == []
    with fitz.open(tmp_path / "out.pdf") as out:
        text = out[0].get_text()
        assert "document" in text.lower() and "NORTHSTAR" not in text
        assert not out.get_toc() and not out.metadata.get("author")
    keep = [d["id"] for d in plan["pages"][0]["detections"]]
    result = apply(document, plan, {"ignored": keep, "output": {"searchable": True}}, tmp_path / "leak.pdf",
                   tessdata=tessdata)
    assert result["residual"][0] == {"page": 1, "output": 1, "targets": ["NORTHSTAR STUDIO"]}

def test_progress_callback_can_cancel(document):
    seen = []
    def progress(stage, done, total):
        seen.append((stage, done, total))
        if done == 1:
            raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        detect(document, {"text": ["NORTHSTAR STUDIO"], "ocr": False}, progress=progress)
    assert seen[0] == ("detect", 0, 3)

def test_download_name_is_safe():
    assert download_name("Report Q3.PDF") == "Report Q3 (redacted).pdf"
    header = disposition(download_name("考試卷.pdf"))
    assert 'filename="' in header and "filename*=UTF-8''" in header and "\n" not in header

@pytest.fixture
def server(tmp_path):
    app = App(tmp_path, 0)
    http = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    app.port = http.server_address[1]
    threading.Thread(target=http.serve_forever, daemon=True).start()
    yield app, f"http://127.0.0.1:{app.port}"
    http.shutdown()
    http.server_close()

def call(server, path, method="GET", body=None, raw=None, headers=None):
    app, url = server
    hdrs = {"Content-Type": "application/json", "X-Scan-Token": app.token, **(headers or {})}
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else (b"" if method == "POST" else None))
    req = urllib.request.Request(url + path, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req) as response:
            payload = response.read()
            kind = response.headers.get_content_type()
            return response.status, (json.loads(payload) if kind == "application/json" else payload), response.headers
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read()), error.headers

def test_reloads_never_lock_the_app(server):
    app, _ = server
    first = call(server, "/api/demo", "POST")[1]["id"]
    for _ in range(MAX_JOBS + 2):
        assert call(server, "/api/demo", "POST")[0] == 200
    assert len(app.jobs) == MAX_JOBS
    assert first not in app.jobs

def test_session_resume_progress_and_named_download(server):
    _, job, _ = call(server, "/api/demo", "POST")
    prefix = "/api/jobs/" + job["id"]
    code, resumed, _ = call(server, prefix)
    assert code == 200 and resumed["name"] == job["name"] and resumed["plan"] is None
    assert call(server, prefix + "/progress")[1] == {"busy": False, "progress": None}
    _, plan, _ = call(server, prefix + "/detect", "POST", {"config": {"text": ["NORTHSTAR STUDIO"], "ocr": False}})
    _, preview, _ = call(server, prefix + "/preview", "POST",
                         {"planHash": plan["planHash"], "decisions": {"output": {"fill": "black"}}})
    assert preview["filename"] == "Synthetic demo (redacted).pdf" and preview["output"]["fill"] == "black"
    assert call(server, prefix)[1]["candidate"]["sha256"] == preview["sha256"]
    _, result, _ = call(server, prefix + "/approve-export", "POST", {"sha256": preview["sha256"], "reviewed": True})
    code, data, headers = call(server, result["downloadUrl"])
    assert code == 200 and data.startswith(b"%PDF")
    assert "Synthetic demo (redacted).pdf" in headers["Content-Disposition"]

def test_thumbnail_width_and_static_assets(server):
    _, job, _ = call(server, "/api/demo", "POST")
    code, png, _ = call(server, "/api/jobs/" + job["id"] + "/original/1?w=120")
    assert code == 200 and Image.open(io.BytesIO(png)).width == 120
    for asset in ("/", "/app.js", "/i18n.js", "/style.css", "/favicon.svg"):
        assert call(server, asset)[0] == 200
    assert call(server, "/fonts/../server.py")[0] == 404

def test_upload_rejects_non_pdf(server):
    code, body, _ = call(server, "/api/upload", "POST", raw=b"hello world, not a pdf",
                         headers={"Content-Type": "application/pdf", "X-Filename": "fake.pdf"})
    assert code == 400 and "not a PDF" in body["error"]
