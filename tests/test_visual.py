import io
import cv2
import numpy as np
import pymupdf as fitz
import pytest
from PIL import Image, ImageDraw
from pdfscan.engine import detect,apply,source_hash,normalized,overlap,validate_config
from pdfscan.visual import logo_png

def logo():
    image=Image.new("RGB",(160,120),"white")
    draw=ImageDraw.Draw(image)
    draw.polygon([(15,100),(80,10),(145,100)],fill="#185978")
    draw.ellipse((50,40,110,100),fill="white")
    draw.rectangle((73,52,86,108),fill="#cc823c")
    out=io.BytesIO();image.save(out,"PNG");return out.getvalue()

def qr():
    code=cv2.QRCodeEncoder_create().encode("https://example.test/private")
    code=cv2.copyMakeBorder(code,4,4,4,4,cv2.BORDER_CONSTANT,value=255)
    code=cv2.resize(code,(240,240),interpolation=cv2.INTER_NEAREST)
    out=io.BytesIO();Image.fromarray(code).save(out,"PNG");return out.getvalue()

def test_urls_and_hyperlink_rectangles(tmp_path):
    path=tmp_path/"links.pdf"
    doc=fitz.open();page=doc.new_page()
    page.insert_text((60,100),"https://example.test/remove")
    page.insert_text((60,160),"Visit website")
    page.insert_link({"kind":fitz.LINK_URI,"from":fitz.Rect(60,145,140,165),"uri":"https://example.test/hidden"})
    doc.save(path);doc.close()
    plan=detect(path,{"ocr":False,"urls":True})
    ds=plan["pages"][0]["detections"]
    assert any(d["rule"]=="pattern:url" for d in ds)
    assert any(d["method"]=="hyperlink" for d in ds)
    assert not detect(path,{"ocr":False})["pages"][0]["detections"]
    before=source_hash(path);out=tmp_path/"clean.pdf"
    apply(path,plan,{},out)
    assert source_hash(path)==before
    with fitz.open(out) as clean:assert not clean[0].get_links()

def test_qr_in_native_and_scanned_rotated_pages(tmp_path):
    path=tmp_path/"qr.pdf"
    doc=fitz.open();page=doc.new_page(width=400,height=500)
    page.insert_image(fitz.Rect(60,70,180,190),stream=qr())
    scan=page.get_pixmap(dpi=144).tobytes("png")
    page=doc.new_page(width=400,height=500);page.insert_image(page.rect,stream=scan);page.set_rotation(90)
    doc.save(path);doc.close()
    plan=detect(path,{"ocr":False,"qr":True})
    assert all(any(d["method"]=="qr-code" for d in p["detections"]) for p in plan["pages"])
    assert all(d["matched"] is None for p in plan["pages"] for d in p["detections"])
    with fitz.open(path) as original:
        for i,page in enumerate(original):
            expected=normalized(fitz.Rect(65,75,175,185),page)
            assert any(overlap(d["rect"],expected)>.8 for d in plan["pages"][i]["detections"])
    out=tmp_path/"clean.pdf";apply(path,plan,{},out)
    with fitz.open(out) as clean:
        for page in clean:
            pix=page.get_pixmap(dpi=144,colorspace=fitz.csGRAY)
            gray=np.frombuffer(pix.samples,dtype=np.uint8).reshape(pix.height,pix.width)
            assert not cv2.QRCodeDetector().detect(gray)[0]

def test_reference_logo_repeated_scaled_and_scanned(tmp_path):
    reference=tmp_path/"logo.png";reference.write_bytes(logo())
    path=tmp_path/"logos.pdf"
    doc=fitz.open();page=doc.new_page(width=400,height=500)
    page.insert_image(fitz.Rect(50,40,130,100),stream=logo())
    page.insert_image(fitz.Rect(230,200,350,290),stream=logo())
    scan=page.get_pixmap(dpi=144).tobytes("png")
    page=doc.new_page(width=400,height=500);page.insert_image(page.rect,stream=scan);page.set_rotation(90)
    doc.save(path);doc.close()
    plan=detect(path,{"ocr":False},logo_path=reference)
    assert len(plan["config"]["logoSha256"])==64
    for p in plan["pages"]:
        matches=[d for d in p["detections"] if d["method"]=="logo-reference"]
        assert len(matches)==2
        assert all(d["confidence"]>=.87 for d in matches)
    with pytest.raises(ValueError,match="changed"):
        detect(path,{"ocr":False,"logoSha256":"0"*64},logo_path=reference)

def test_auto_logo_is_opt_in_and_preserves_body_diagram(tmp_path):
    path=tmp_path/"layout.pdf";doc=fitz.open();page=doc.new_page(width=400,height=500)
    page.insert_image(fitz.Rect(30,25,90,70),stream=logo())
    page.insert_image(fitz.Rect(150,260,290,365),stream=logo())
    doc.save(path);doc.close()
    assert not detect(path,{"ocr":False})["pages"][0]["detections"]
    ds=detect(path,{"ocr":False,"autoLogos":True})["pages"][0]["detections"]
    assert ds and all(d["rect"][3]<.25 for d in ds)
    assert all(d["heuristic"] for d in ds)

def test_logo_input_validation_and_metadata_stripping():
    clean=logo_png(logo())
    assert logo_png(clean)==clean
    assert not Image.open(io.BytesIO(clean)).info
    with pytest.raises(ValueError):logo_png(b"not an image")
    out=io.BytesIO();Image.new("RGB",(100,100),"white").save(out,"PNG")
    with pytest.raises(ValueError,match="blank"):logo_png(out.getvalue())
    for key in ("qr","urls","autoLogos"):
        with pytest.raises(ValueError):validate_config({key:"yes"})


@pytest.mark.parametrize("rotation",[0,90,180,270])
def test_hyperlink_coordinates_follow_display_rotation(tmp_path,rotation):
    path=tmp_path/"rotated.pdf";doc=fitz.open();page=doc.new_page(width=400,height=500)
    box=fitz.Rect(60,145,140,165)
    page.insert_text((60,160),"Visit website")
    page.insert_link({"kind":fitz.LINK_URI,"from":box,"uri":"https://example.test"});page.set_rotation(rotation)
    doc.save(path);doc.close()
    plan=detect(path,{"ocr":False,"urls":True})
    with fitz.open(path) as doc:expected=normalized(box,doc[0],.002)
    detection=next(d for d in plan["pages"][0]["detections"] if d["method"]=="hyperlink")
    assert detection["rect"]==pytest.approx(expected)

def test_logo_search_in_blank_document_has_no_matches(tmp_path):
    path=tmp_path/"blank.pdf";doc=fitz.open();doc.new_page();doc.save(path);doc.close()
    reference=tmp_path/"logo.png";reference.write_bytes(logo())
    assert not detect(path,{"ocr":False,"qr":True,"autoLogos":True},logo_path=reference)["pages"][0]["detections"]


def test_reference_matches_take_priority_over_logo_guesses(tmp_path):
    path=tmp_path/"logo.pdf";doc=fitz.open();page=doc.new_page(width=400,height=500)
    page.insert_image(fitz.Rect(30,25,110,85),stream=logo());doc.save(path);doc.close()
    reference=tmp_path/"logo.png";reference.write_bytes(logo())
    ds=detect(path,{"ocr":False,"autoLogos":True},logo_path=reference)["pages"][0]["detections"]
    assert len(ds)==1 and ds[0]["method"]=="logo-reference"
