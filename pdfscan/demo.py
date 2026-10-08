"""Generate fictional PDFs for demos and regression tests."""
import io
from pathlib import Path
import pymupdf as fitz
from PIL import Image, ImageDraw

DEMO_RULES = {"text": ["NORTHSTAR STUDIO", "示例機構", "hello@example.test"],
              "caseSensitive": False, "flexible": True, "scope": "all",
              "ocr": True, "languages": "eng+chi_tra", "preset": "generic",
              "imageDigests": [], "email": False, "phone": False}

def create_demo(destination):
    destination = Path(destination)
    logo = Image.new("RGB", (150,150), "white")
    draw = ImageDraw.Draw(logo)
    draw.polygon([(75,10),(140,75),(75,140),(10,75)], fill="#157c77")
    draw.polygon([(75,38),(112,75),(75,112),(38,75)], fill="white")
    stream = io.BytesIO()
    logo.save(stream, "PNG")
    logo_bytes = stream.getvalue()
    teal = (.06,.45,.43)
    slate = (.15,.2,.25)
    def header(page, subtitle):
        page.insert_image(fitz.Rect(45,38,87,80), stream=logo_bytes)
        page.insert_text((104,55), "NORTHSTAR STUDIO", fontsize=18, color=teal)
        page.insert_text((104,75), subtitle, fontsize=10, color=slate)
        page.draw_line((45,102),(550,102), color=(.8,.85,.85), width=.7)
    with fitz.open() as doc:
        page = doc.new_page(width=595,height=842)
        header(page, "PROJECT NOTES  /  FICTIONAL DEMO")
        page.insert_text((45,143), "A document worth keeping.", fontsize=25, color=slate)
        page.insert_text((45,175), "Remove selected details. Keep the ideas, layout and illustrations.", fontsize=11, color=slate)
        page.insert_text((45,223), "Prepared by NORTHSTAR STUDIO", fontsize=12, color=slate)
        page.insert_text((45,250), "Contact: hello@example.test", fontsize=12, color=slate)
        page.insert_text((45,277), "Reference: DEMO-2048", fontsize=12, color=slate)
        page.insert_text((45,333), "01   THE CONTENT", fontsize=10, color=teal)
        page.insert_textbox(fitz.Rect(45,350,545,415),
                           "This is synthetic material. Names and contact details are invented. "
                           "The drawing below is useful content: text matching should preserve it.",
                           fontsize=12, lineheight=1.6, color=slate)
        page.draw_rect(fitz.Rect(45,450,550,655), color=(.83,.88,.88), fill=(.96,.98,.98))
        page.insert_text((66,482), "A simple process diagram", fontsize=12, color=slate)
        for x, label in [(70,"INPUT"),(242,"REVIEW"),(414,"OUTPUT")]:
            page.draw_rect(fitz.Rect(x,535,x+112,600),color=teal,width=1.3)
            page.insert_text((x+21,573),label,fontsize=12,color=teal)
        page.draw_line((182,568),(242,568),color=teal,width=1.3)
        page.draw_line((354,568),(414,568),color=teal,width=1.3)
        page.insert_text((45,734),"Signature: ______________________",fontsize=12,color=slate)
        page.insert_text((45,793),"SYNTHETIC SAMPLE  /  No real personal information",fontsize=9,color=(.4,.47,.5))
        page.insert_text((532,793),"01",fontsize=10,color=teal)
        base = fitz.open()
        scanned = base.new_page(width=595,height=842)
        scanned.insert_text((45,55), "NORTHSTAR STUDIO", fontsize=18, color=teal)
        scanned.insert_text((45,83), "SCANNED PAGE  /  OCR DEMO", fontsize=10, color=slate)
        scanned.draw_line((45,102),(550,102), color=(.8,.85,.85), width=.7)
        scanned.insert_text((45,150),"Text inside an image.",fontsize=25,color=slate)
        scanned.insert_text((45,198),"This page has no selectable text layer.",fontsize=12,color=slate)
        scanned.insert_text((45,242),"示例機構",fontname="china-t",fontsize=22,color=teal)
        scanned.insert_text((45,289),"NORTHSTAR STUDIO",fontsize=17,color=slate)
        scanned.insert_text((45,329),"hello@example.test",fontsize=14,color=slate)
        scanned.insert_text((45,391),"Keep this sentence and the chart below.",fontsize=12,color=slate)
        for i,h in enumerate([65,105,80,140]):
            scanned.draw_rect(fitz.Rect(85+i*100,630-h,133+i*100,630),fill=teal,color=teal)
        scanned.insert_text((45,793),"SYNTHETIC SAMPLE  /  Page rendered as a scan",fontsize=9,color=(.4,.47,.5))
        pix = scanned.get_pixmap(dpi=150,alpha=False)
        page = doc.new_page(width=595,height=842)
        page.insert_image(page.rect,stream=pix.tobytes("png"))
        base.close()
        page = doc.new_page(width=595,height=842)
        header(page,"ROTATED PAGE  /  MATCHING DEMO")
        page.insert_text((45,165),"NORTHSTAR STUDIO",fontsize=20,color=teal)
        page.insert_text((45,213),"Multi-word targets still match when this page is rotated.",fontsize=11,color=slate)
        page.insert_text((45,268),"Useful content should remain visible.",fontsize=12,color=slate)
        page.set_rotation(90)
        doc.set_metadata({"author":"FICTIONAL DEMO AUTHOR","title":"Synthetic demonstration"})
        doc.set_toc([[1,"Synthetic bookmark",1]])
        doc.embfile_add("synthetic.txt",b"Synthetic attachment used to test output stripping.")
        destination.parent.mkdir(parents=True,exist_ok=True)
        doc.save(destination,garbage=4,deflate=True)
    return DEMO_RULES.copy()
