import io
import json
from pathlib import Path
import pymupdf as fitz
import pytest
from PIL import Image
from pdfscan.engine import detect, apply, source_hash, validate_config
from pdfscan.demo import create_demo, DEMO_RULES

ROOT=Path(__file__).resolve().parent.parent

@pytest.fixture
def document(tmp_path):
    path=tmp_path/"synthetic.pdf"
    create_demo(path)
    return path

def rules(**kwargs):
    return {"text":["NORTHSTAR STUDIO"],"ocr":False,**kwargs}

def test_generic_preserves_pages_and_images(document,tmp_path):
    plan=detect(document,rules())
    assert len(plan["pages"])==3
    assert all(not p["removeProposed"] for p in plan["pages"])
    assert all(d["method"]!="image-match" for p in plan["pages"] for d in p["detections"])
    before=source_hash(document)
    result=apply(document,plan,{},tmp_path/"clean.pdf")
    assert result["outputPages"]==3
    assert source_hash(document)==before

@pytest.mark.parametrize("rotation",[0,90,180,270])
def test_multiword_match_on_rotated_page(tmp_path,rotation):
    path=tmp_path/"rotated.pdf"
    with fitz.open() as doc:
        page=doc.new_page()
        page.insert_text((55,80),"NORTHSTAR STUDIO",fontsize=20)
        page.set_rotation(rotation)
        doc.save(path)
    plan=detect(path,rules())
    assert len(plan["pages"][0]["detections"])==1
    detection=plan["pages"][0]["detections"][0]
    assert detection["matched"]=="NORTHSTAR STUDIO"
    result=apply(path,plan,{},tmp_path/"clean.pdf")
    with fitz.open(tmp_path/"clean.pdf") as out:
        image=Image.open(io.BytesIO(out[0].get_pixmap(dpi=200,alpha=False).tobytes("png")))
        r=detection["rect"]
        crop=image.crop((int(r[0]*image.width)+2,int(r[1]*image.height)+2,int(r[2]*image.width)-2,int(r[3]*image.height)-2))
        assert crop.convert("L").getextrema()==(255,255)
        # Check the complete original target, not just the detector's own rectangle.
        with fitz.open(path) as original:
            box=original[0].search_for("NORTHSTAR STUDIO")[0] * original[0].rotation_matrix
            bounds=(int(box.x0*200/72),int(box.y0*200/72),
                    int(box.x1*200/72+.999),int(box.y1*200/72+.999))
            before=Image.open(io.BytesIO(original[0].get_pixmap(dpi=200,alpha=False).tobytes("png")))
            assert before.crop(bounds).convert("L").getextrema()[0]<100
            assert image.crop(bounds).convert("L").getextrema()==(255,255)

def test_no_partial_word_false_match(tmp_path):
    path=tmp_path/"words.pdf"
    with fitz.open() as doc:
        p=doc.new_page()
        p.insert_text((30,60),"ACMEology ACME ACME-INC",fontsize=18)
        doc.save(path)
    plan=detect(path,rules(text=["ACME"]))
    # Letters inside a word never match (ACMEology); hyphen, slash and other
    # punctuation separate words, so the ACME in ACME-INC is found.
    rects=sorted(d["rect"][0] for d in plan["pages"][0]["detections"])
    assert len(rects)==2
    with fitz.open(path) as doc:
        page=doc[0]
        ology_end=page.search_for("ACMEology")[0].x1/page.rect.width
    assert all(r>ology_end for r in rects)

def test_case_and_spacing_options(tmp_path):
    path=tmp_path/"words.pdf"
    with fitz.open() as doc:
        p=doc.new_page()
        p.insert_text((30,60),"Northstar Studio",fontsize=18)
        doc.save(path)
    assert len(detect(path,rules())["pages"][0]["detections"])==1
    assert not detect(path,rules(caseSensitive=True))["pages"][0]["detections"]
    assert not detect(path,rules(text=["NorthstarStudio"],flexible=False))["pages"][0]["detections"]

def test_native_chinese(tmp_path):
    path=tmp_path/"chinese.pdf"
    with fitz.open() as doc:
        p=doc.new_page()
        p.insert_text((30,60),"示例機構",fontname="china-t",fontsize=22)
        doc.save(path)
    plan=detect(path,rules(text=["示例機構"]))
    assert plan["pages"][0]["detections"]

def test_header_scope_excludes_body(document):
    plan=detect(document,rules(scope="header"))
    assert len(plan["pages"][0]["detections"])==1

def test_selected_image_finds_identical_copies(document):
    empty=detect(document,rules(text=[]))
    digest=empty["pages"][0]["images"][0]["digest"]
    plan=detect(document,rules(text=[],imageDigests=[digest]))
    pages=[p["number"] for p in plan["pages"] if any(d["method"]=="image-match" for d in p["detections"])]
    assert pages==[1,3]

def test_restored_detection_and_manual_region(document,tmp_path):
    plan=detect(document,rules())
    first=plan["pages"][0]["detections"][0]
    decisions={"ignored":[first["id"]],"manual":[{"page":1,"rect":[.05,.8,.95,.9]}]}
    apply(document,plan,decisions,tmp_path/"clean.pdf")
    with fitz.open(tmp_path/"clean.pdf") as doc:
        im=Image.open(io.BytesIO(doc[0].get_pixmap(dpi=200,alpha=False).tobytes("png"))).convert("L")
        assert im.crop((int(first["rect"][0]*im.width)+2,int(first["rect"][1]*im.height)+2,
                        int(first["rect"][2]*im.width)-2,int(first["rect"][3]*im.height)-2)).getextrema()[0]<100
        assert im.crop((int(.06*im.width),int(.81*im.height),int(.94*im.width),int(.89*im.height))).getextrema()==(255,255)

def test_adjusted_detection_replaces_proposed_box(document,tmp_path):
    plan=detect(document,rules())
    first=plan["pages"][0]["detections"][0]
    x0,y0,x1,y1=first["rect"]
    moved=[.05,.85,.35,.95]
    apply(document,plan,{"adjusted":{first["id"]:moved},"output":{"fill":"black"}},tmp_path/"clean.pdf")
    with fitz.open(tmp_path/"clean.pdf") as doc, fitz.open(document) as source:
        im=Image.open(io.BytesIO(doc[0].get_pixmap(dpi=200,alpha=False).tobytes("png"))).convert("L")
        src=Image.open(io.BytesIO(source[0].get_pixmap(dpi=200,alpha=False).tobytes("png"))).convert("L")
        old=(int(x0*im.width)+2,int(y0*im.height)+2,int(x1*im.width)-2,int(y1*im.height)-2)
        # The original box is no longer covered; the moved box is filled.
        assert im.crop(old).tobytes()==src.crop(old).tobytes()
        assert im.crop((int(.06*im.width),int(.86*im.height),int(.34*im.width),int(.94*im.height))).getextrema()==(0,0)

def test_protected_area_preserves_original_pixels(document,tmp_path):
    plan=detect(document,rules(text=[]))
    decision={"manual":[{"page":1,"rect":[0,0,1,1]}],"protected":[{"page":1,"rect":[.05,.05,.95,.95]}]}
    apply(document,plan,decision,tmp_path/"clean.pdf")
    with fitz.open(tmp_path/"clean.pdf") as doc, fitz.open(document) as source:
        pix1=doc[0].get_pixmap(dpi=200,alpha=False)
        pix2=source[0].get_pixmap(dpi=200,alpha=False)
        im1=Image.open(io.BytesIO(pix1.tobytes("png")))
        im2=Image.open(io.BytesIO(pix2.tobytes("png")))
        box=(int(.06*im1.width),int(.06*im1.height),int(.94*im1.width),int(.94*im1.height))
        assert im1.crop(box).tobytes()==im2.crop(box).tobytes()

def test_strip_hidden_pdf_content(document,tmp_path):
    plan=detect(document,rules())
    output=tmp_path/"clean.pdf"
    apply(document,plan,{},output)
    with fitz.open(output) as doc:
        assert doc.embfile_count()==0
        assert doc.get_toc()==[]
        assert not any(p.get_text() or p.get_links() or list(p.annots() or []) or list(p.widgets() or []) for p in doc)
        assert all(not doc.metadata.get(k) for k in ["title","author","creator","subject","keywords","producer"])

def test_explicit_page_removal_and_empty_rejection(document,tmp_path):
    plan=detect(document,rules())
    result=apply(document,plan,{"removePages":[2]},tmp_path/"clean.pdf")
    assert result["pageMap"]==[{"original":1,"output":1},{"original":2,"output":None},{"original":3,"output":2}]
    with pytest.raises(ValueError,match="at least one"):
        apply(document,plan,{"removePages":[1,2,3]},tmp_path/"none.pdf")
    assert not (tmp_path/"none.pdf").exists()

def test_reject_changed_plan_and_overwriting_original(document,tmp_path):
    plan=detect(document,rules())
    with pytest.raises(ValueError,match="original"):
        apply(document,plan,{},document)
    plan["pages"][0]["detections"]=[]
    with pytest.raises(ValueError,match="plan changed"):
        apply(document,plan,{},tmp_path/"clean.pdf")

def test_stable_detection_ids(document):
    assert detect(document,rules())==detect(document,rules())

@pytest.mark.parametrize("raw",[
    {"unknown":True},{"text":[""]},{"text":["---"]},{"ocr":"yes"},
    {"scope":"somewhere"},{"languages":"../../bad"},
    {"imageDigests":["bad"]},{"text":["a"*201]}])
def test_reject_invalid_rules(raw):
    with pytest.raises(ValueError):
        validate_config(raw)

def test_reject_invalid_drawn_regions(document,tmp_path):
    plan=detect(document,rules())
    for decision in [
        {"manual":[{"page":1,"rect":[.5,.5,.2,.3]}]},
        {"protected":[{"page":4,"rect":[0,0,1,1]}]},
        {"removePages":[1],"keepPages":[1]},
        {"ignored":["unknown"]},
        {"manual":[{"page":True,"rect":[0,0,1,1]}]},
        {"adjusted":{"unknown":[0,0,1,1]}},
        {"adjusted":[]},
        {"adjusted":{plan["pages"][0]["detections"][0]["id"]:[.5,.5,.4,.6]}}]:
        with pytest.raises(ValueError):
            apply(document,plan,decision,tmp_path/"clean.pdf")

def test_ocr_scanned_text_and_cache(document,tmp_path):
    tessdata=ROOT/".runtime"/"tessdata"
    if not all((tessdata/(n+".traineddata")).exists() for n in ["eng","chi_tra"]):
        pytest.skip("Install OCR models for the OCR integration test.")
    cache=tmp_path/"cache"
    plan=detect(document,DEMO_RULES,cache_dir=cache,tessdata=tessdata)
    assert plan["ocr"]=={"reusedPages":0,"freshPages":3,"engine":"tesseract"}
    detected={d["matched"] for d in plan["pages"][1]["detections"]}
    assert "NORTHSTAR STUDIO" in detected
    assert "示例機構" in detected
    assert "hello@example.test" in detected
    repeated=detect(document,DEMO_RULES,cache_dir=cache,tessdata=tessdata)
    assert repeated["ocr"]=={"reusedPages":3,"freshPages":0,"engine":"tesseract"}
    assert [[d["id"] for d in p["detections"]] for p in plan["pages"]]==[[d["id"] for d in p["detections"]] for p in repeated["pages"]]
    forced=detect(document,DEMO_RULES,cache_dir=cache,tessdata=tessdata,force_ocr=True)
    assert forced["ocr"]["freshPages"]==3
    english=detect(document,{**DEMO_RULES,"languages":"eng"},cache_dir=cache,tessdata=tessdata)
    assert english["ocr"]["freshPages"]==3

def test_exam_page_proposals_are_opt_in(tmp_path):
    path=tmp_path/"instructions.pdf"
    with fitz.open() as doc:
        p=doc.new_page()
        for y,text in [(50,"Instructions"),(80,"Answer all questions."),(110,"Time allowed: 2 hours."),(140,"Write your answers in the answer book.")]:
            p.insert_text((30,y),text)
        p=doc.new_page()
        p.insert_text((30,70),"1. Calculate the area. (3 marks)")
        doc.save(path)
    generic=detect(path,rules(text=[]))
    exam=detect(path,rules(text=[],preset="exam"))
    assert not generic["pages"][0]["removeProposed"]
    assert exam["pages"][0]["removeProposed"]
    assert not exam["pages"][1]["removeProposed"]

def test_missing_ocr_models_blocks_generation(document,tmp_path):
    with pytest.raises(ValueError,match="Missing OCR model"):
        detect(document,{**rules(),"ocr":True},tessdata=tmp_path/"missing")


def test_adjacent_line_ink_is_preserved(tmp_path):
    path=tmp_path/"tight-lines.pdf"
    with fitz.open() as doc:
        page=doc.new_page()
        page.insert_text((50,60),"NORTHSTAR STUDIO",fontsize=20)
        page.insert_text((50,77),"Mathematics remains intact",fontsize=18)
        doc.save(path)
    plan=detect(path,rules())
    apply(path,plan,{},tmp_path/"clean.pdf")
    with fitz.open(path) as original, fitz.open(tmp_path/"clean.pdf") as clean:
        a=Image.open(io.BytesIO(original[0].get_pixmap(dpi=200,alpha=False).tobytes("png")))
        b=Image.open(io.BytesIO(clean[0].get_pixmap(dpi=200,alpha=False).tobytes("png")))
        box=(int(48*200/72),int(63*200/72),int(300*200/72),int(80*200/72))
        assert a.crop(box).tobytes()==b.crop(box).tobytes()

def test_useful_diagram_pixels_survive_redaction(document,tmp_path):
    plan=detect(document,rules())
    apply(document,plan,{},tmp_path/"clean.pdf")
    with fitz.open(document) as original,fitz.open(tmp_path/"clean.pdf") as clean:
        a=Image.open(io.BytesIO(original[0].get_pixmap(dpi=200,alpha=False).tobytes("png")))
        b=Image.open(io.BytesIO(clean[0].get_pixmap(dpi=200,alpha=False).tobytes("png")))
        box=(int(40*200/72),int(440*200/72),int(555*200/72),int(665*200/72))
        assert a.crop(box).tobytes()==b.crop(box).tobytes()

@pytest.mark.parametrize("rotation",[90,270])
def test_vertical_native_text_on_unrotated_page(tmp_path,rotation):
    path=tmp_path/"vertical-text.pdf"
    with fitz.open() as doc:
        page=doc.new_page()
        page.insert_text((200,350),"NORTHSTAR STUDIO",fontsize=20,rotate=rotation)
        doc.save(path)
    plan=detect(path,rules())
    assert len(plan["pages"][0]["detections"])==1
    apply(path,plan,{},tmp_path/"clean.pdf")
    with fitz.open(path) as original,fitz.open(tmp_path/"clean.pdf") as out:
        box=original[0].search_for("NORTHSTAR STUDIO")[0]
        bounds=(int(box.x0*200/72),int(box.y0*200/72),int(box.x1*200/72+.999),int(box.y1*200/72+.999))
        before=Image.open(io.BytesIO(original[0].get_pixmap(dpi=200,alpha=False).tobytes("png"))).convert("L")
        after=Image.open(io.BytesIO(out[0].get_pixmap(dpi=200,alpha=False).tobytes("png"))).convert("L")
        assert before.crop(bounds).getextrema()[0]<100
        assert after.crop(bounds).getextrema()==(255,255)

def test_drawn_area_inside_keep_area_is_still_removed(document,tmp_path):
    plan=detect(document,rules(text=[]))
    decision={"manual":[{"page":1,"rect":[.2,.2,.4,.3]}],"protected":[{"page":1,"rect":[.05,.05,.95,.95]}],"output":{"fill":"black"}}
    apply(document,plan,decision,tmp_path/"clean.pdf")
    with fitz.open(tmp_path/"clean.pdf") as doc:
        im=Image.open(io.BytesIO(doc[0].get_pixmap(dpi=200,alpha=False).tobytes("png"))).convert("L")
        assert im.crop((int(.21*im.width),int(.21*im.height),int(.39*im.width),int(.29*im.height))).getextrema()==(0,0)
