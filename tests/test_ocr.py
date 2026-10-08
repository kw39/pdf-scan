"""OCR engine tests: PP-OCR reading, orientation, matching tolerance and quiet fallback.

PP-OCR tests need the models from scripts/setup_ocr.py and are skipped without them.
Documents are rendered from fictional text with real CJK fonts, like a scan.
"""
import io
import os
import sys
from pathlib import Path

import pymupdf as fitz
import pytest
from PIL import Image, ImageDraw, ImageFont

from pdfscan import ppocr
from pdfscan.engine import (allowed_edits, apply, detect, match_key, quiet_native_output, within_edits,
                            validate_config)

ROOT = Path(__file__).resolve().parent.parent
MODELS = ROOT / ".runtime" / "models" / "ppocr"
TESS = ROOT / ".runtime" / "tessdata"
needs_models = pytest.mark.skipif(not ppocr.available(MODELS), reason="PP-OCR models not installed")
CJK_FONT = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
LATIN_FONT = "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"

def font(size, cjk=False):
    try:
        return ImageFont.truetype(CJK_FONT, size, index=3) if cjk else ImageFont.truetype(LATIN_FONT, size)
    except OSError:
        pytest.skip("test fonts unavailable")

def scanned_form(path, turn=0):
    """A ruled staff form, rendered as an image-only page (no text layer)."""
    im = Image.new("L", (1654, 2339), 248)
    d = ImageDraw.Draw(im)
    d.text((140, 120), "海港貿易有限公司", font=font(56, True), fill=20)
    rows = [("姓名", "陳大文"), ("Name", "Chan Tai Man"), ("HKID", "A123456(3)"), ("Phone", "+852 2345 6789")]
    for i, (label, value) in enumerate(rows):
        y = 300 + i * 90
        d.line((140, y, 1500, y), fill=30, width=3)
        d.text((170, y + 22), label, font=font(36, label == "姓名"), fill=20)
        d.text((620, y + 18), value, font=font(40, value == "陳大文"), fill=20)
    for x in (140, 600, 1500):  # vertical cell rules: what Tesseract skips
        d.line((x, 300, x, 300 + 4 * 90), fill=30, width=3)
    d.line((140, 660, 1500, 660), fill=30, width=3)
    d.text((140, 760), "查詢電話：+852 9876 5432", font=font(34, True), fill=20)
    if turn:
        im = im.rotate(turn, expand=True)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=80)
    with fitz.open() as doc:
        w, h = (842, 595) if turn in (90, 270) else (595, 842)
        page = doc.new_page(width=w, height=h)
        page.insert_image(page.rect, stream=buf.getvalue())
        doc.save(path)
    return path

def found(plan):
    return {(d["matched"] if d["rule"].startswith("text:") else d["rule"]) for p in plan["pages"] for d in p["detections"]}

@needs_models
def test_ppocr_reads_table_cells_and_chinese(tmp_path):
    path = scanned_form(tmp_path / "form.pdf")
    plan = detect(path, {"text": ["陳大文", "Chan Tai Man", "海港貿易有限公司"], "phone": True, "ids": True},
                  models=MODELS, tessdata=TESS)
    assert plan["ocr"]["engine"] == "ppocr"
    hits = found(plan)
    assert {"陳大文", "Chan Tai Man", "海港貿易有限公司", "pattern:id", "pattern:phone"} <= hits
    phones = [d for p in plan["pages"] for d in p["detections"] if d["rule"] == "pattern:phone"]
    assert len(phones) == 2  # including the one written straight after Chinese text

@needs_models
@pytest.mark.parametrize("turn", [90, 180, 270])
def test_ppocr_handles_sideways_and_upside_down_scans(tmp_path, turn):
    path = scanned_form(tmp_path / f"turned{turn}.pdf", turn)
    plan = detect(path, {"text": ["Chan Tai Man", "陳大文"], "ids": True}, models=MODELS, tessdata=TESS)
    assert {"Chan Tai Man", "陳大文", "pattern:id"} <= found(plan)

@needs_models
def test_ppocr_word_boxes_cover_the_target(tmp_path):
    path = scanned_form(tmp_path / "form.pdf")
    plan = detect(path, {"text": ["Chan Tai Man"]}, models=MODELS, tessdata=TESS)
    rect = next(d["rect"] for p in plan["pages"] for d in p["detections"] if d["matched"] == "Chan Tai Man")
    out = tmp_path / "clean.pdf"
    apply(path, plan, {}, out)
    with fitz.open(out) as doc:
        im = Image.open(io.BytesIO(doc[0].get_pixmap(dpi=150).tobytes("png"))).convert("L")
    # The value cell starts at x=620/1654 of the scan; the removed box must reach it and leave no ink.
    x0, y0, x1, y1 = (int(rect[0] * im.width), int(rect[1] * im.height), int(rect[2] * im.width), int(rect[3] * im.height))
    assert rect[0] <= 620 / 1654
    assert im.crop((x0 + 2, y0 + 2, x1 - 2, y1 - 2)).getextrema()[0] > 200

@needs_models
def test_ppocr_cache_identity_and_reuse(tmp_path):
    path = scanned_form(tmp_path / "form.pdf")
    first = detect(path, {"text": ["陳大文"]}, models=MODELS, tessdata=TESS, cache_dir=tmp_path / "c")
    second = detect(path, {"text": ["陳大文"]}, models=MODELS, tessdata=TESS, cache_dir=tmp_path / "c")
    assert first["ocr"]["freshPages"] == 1 and second["ocr"]["reusedPages"] == 1
    assert found(first) == found(second)

@needs_models
def test_searchable_export_uses_ppocr_and_checks_leftovers(tmp_path):
    path = scanned_form(tmp_path / "form.pdf")
    plan = detect(path, {"text": ["Chan Tai Man"]}, models=MODELS, tessdata=TESS)
    ids = [d["id"] for p in plan["pages"] for d in p["detections"]]
    result = apply(path, plan, {"ignored": ids, "output": {"searchable": True}}, tmp_path / "out.pdf", models=MODELS)
    assert result["residual"] and result["residual"][0]["targets"] == ["Chan Tai Man"]
    with fitz.open(tmp_path / "out.pdf") as doc:
        assert "Chan" in doc[0].get_text()

def test_falls_back_to_tesseract_without_models(tmp_path):
    path = scanned_form(tmp_path / "form.pdf")
    plan = detect(path, {"text": ["Phone"]}, models=tmp_path / "missing", tessdata=TESS)
    assert plan["ocr"]["engine"] == "tesseract"

def test_traditional_and_simplified_forms_match():
    c = validate_config({})
    assert match_key("學號 吳雅婷", c) == match_key("学号 吴雅婷", c)
    assert match_key("陳大文", c) != match_key("陳大明", c)

def test_close_match_limits():
    assert allowed_edits("chantaiman") == 1 and allowed_edits("lokfusecondaryschool") == 2
    assert allowed_edits("陳大文") == 0 and allowed_edits("真光女書院") == 1 and allowed_edits("chan") == 0
    assert within_edits("lokfusecondatyschool", "lokfusecondaryschool", 2)
    assert not within_edits("wongsiuming", "chantaiman", 1)

def test_close_matches_only_apply_to_ocr_text(tmp_path):
    path = tmp_path / "typed.pdf"
    with fitz.open() as doc:
        doc.new_page().insert_text((60, 80), "Lok Fu Secondaty School", fontsize=14)
        doc.save(path)
    assert found(detect(path, {"text": ["Lok Fu Secondary School"], "ocr": False})) == set()

def test_quiet_native_output_silences_c_level_writes(capfd):
    with quiet_native_output():
        os.write(1, b"Image too small to scale!!\n")
        os.write(2, b"Line cannot be recognized!!\n")
    print("visible")
    out, err = capfd.readouterr()
    assert "too small" not in out + err and "cannot be recognized" not in out + err and "visible" in out

def _ink(path, rect, dpi=200):
    with fitz.open(path) as doc:
        page = doc[0]
        clip = fitz.Rect(rect[0] * page.rect.width, rect[1] * page.rect.height,
                         rect[2] * page.rect.width, rect[3] * page.rect.height)
        im = Image.open(io.BytesIO(page.get_pixmap(dpi=dpi, clip=clip).tobytes("png"))).convert("L")
    return im.getextrema()[0]

@pytest.mark.parametrize("line,target,keep", [
    ("2026-DSE-ECON 1-2", "DSE", ["2026", "ECON"]),
    ("Ref DSE2026/ECON", "DSE", ["ECON"]),
])
def test_codes_inside_hyphenated_words_typed(tmp_path, line, target, keep):
    path = tmp_path / "typed.pdf"
    with fitz.open() as doc:
        doc.new_page().insert_text((300, 60), line, fontsize=12, fontname="tiro")
        doc.save(path)
    plan = detect(path, {"text": [target], "ocr": False})
    dets = [d for p in plan["pages"] for d in p["detections"]]
    assert [d["matched"] for d in dets] == [target]
    out = tmp_path / "out.pdf"
    apply(path, plan, {}, out)
    with fitz.open(path) as doc:
        page = doc[0]
        hit = page.search_for(target)[0]
        kept = [page.search_for(k)[0] for k in keep]
    with fitz.open(out) as doc:
        text_gone = Image.open(io.BytesIO(doc[0].get_pixmap(dpi=200, clip=hit).tobytes("png"))).convert("L").getextrema()[0]
        survivors = [Image.open(io.BytesIO(doc[0].get_pixmap(dpi=200, clip=r).tobytes("png"))).convert("L").getextrema()[0]
                     for r in kept]
    assert text_gone > 200  # every pixel of the target is gone
    assert all(v < 100 for v in survivors)  # neighbouring parts of the code stay readable

def test_chinese_without_spaces_and_no_partial_letters(tmp_path):
    path = tmp_path / "zh.pdf"
    with fitz.open() as doc:
        page = doc.new_page()
        page.insert_text((60, 80), "2026年DSE考試 category", fontsize=14, fontname="china-t")
        doc.save(path)
    assert {d["matched"] for p in detect(path, {"text": ["DSE", "考試", "cat"], "ocr": False})["pages"]
            for d in p["detections"]} == {"DSE", "考試"}

@needs_models
def test_codes_inside_words_on_scans(tmp_path):
    im = Image.new("L", (1654, 2339), 250)
    ImageDraw.Draw(im).text((900, 120), "2026-DSE-ECON 1-2", font=font(34), fill=10)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=75)
    path = tmp_path / "scan.pdf"
    with fitz.open() as doc:
        page = doc.new_page(width=595, height=842)
        page.insert_image(page.rect, stream=buf.getvalue())
        doc.save(path)
    plan = detect(path, {"text": ["DSE"]}, models=MODELS, tessdata=TESS)
    rect = next(d["rect"] for p in plan["pages"] for d in p["detections"] if d["matched"] == "DSE")
    out = tmp_path / "out.pdf"
    apply(path, plan, {}, out)
    assert _ink(out, rect) > 200
    width = (rect[2] - rect[0]) * 1654
    assert width < 34 * 3 * 0.9  # about three letters wide, not the whole code
