"""Synthetic watermark review/export regressions; no private sample documents."""
import io
import json

import pymupdf as fitz
import pytest
from PIL import Image, ImageDraw
from pdfscan import engine, watermarks

CONFIG = {'text': ['EXAMPLE SCHOOL'], 'watermarks': True, 'ocr': False}


def document(path, *, form=True, rotation=0, mixed=None, copies=1, label='EXAMPLE SCHOOL'):
    with fitz.open() as doc:
        page=doc.new_page(width=595,height=842)
        page.insert_text((50,300),'Calculate x + 2 = 4. (3 marks)')
        page.draw_rect(fitz.Rect(45,270,380,330))
        page.set_rotation(rotation)
        reference=page.get_pixmap(dpi=200,alpha=False).samples
        if form:
            with fitz.open() as mark:
                mp=mark.new_page(width=595,height=842)
                mp.insert_font(fontname='hebo')
                x=mark.get_new_xref();mark.update_object(x,'<<>>')
                # One visible span split over Tj paints: only its enclosing Do is removable.
                mark.update_stream(x,b'q 0.7 g BT /hebo 42 Tf 1 0 0 1 50 542 Tm (EXAMPLE ) Tj (SCHOOL) Tj ET Q')
                mp.set_contents(x)
                if mixed=='text': mp.insert_text((55,380),'Useful question inside the form')
                if mixed=='graphic': mp.draw_rect(fitz.Rect(50,350,250,400))
                for _ in range(copies): page.show_pdf_page(page.rect * page.derotation_matrix,mark,0)
        else:
            page.insert_text((50,300),label,fontsize=32,color=(.7,.7,.7))
        original=page.get_pixmap(dpi=200,alpha=False).samples
        doc.save(path)
    return reference,original


def samples(path):
    with fitz.open(path) as doc:
        # Inspect the encoded page pixels, without an extra resize from PDF rendering.
        pix=fitz.Pixmap(doc,doc[0].get_images()[0][0])
        return pix.samples


def export(path,plan,tmp_path,decisions=None,name='clean.pdf'):
    out=tmp_path/name;result=engine.apply(path,plan,decisions or {},out)
    return samples(out),result


@pytest.mark.parametrize('form',[False,True])
@pytest.mark.parametrize('rotation',[0,90,180,270])
def test_object_removal_preserves_questions_pixel_exact(tmp_path,form,rotation):
    src=tmp_path/'exam.pdf';reference,_=document(src,form=form,rotation=rotation)
    before=engine.source_hash(src)
    plan=engine.detect(src,CONFIG)
    assert len(plan['pages'][0]['detections'])==1
    assert plan['pages'][0]['detections'][0]['method']=='watermark-text'
    assert '_operation' not in json.dumps(plan)
    pixels,result=export(src,plan,tmp_path,{'output':{'fill':'black'}})
    assert pixels==reference
    assert result['outputPages']==1 and result['removedPages']==[] and result['imageOnly']
    assert engine.source_hash(src)==before
    with fitz.open(tmp_path/'clean.pdf') as out:
        assert not out[0].get_text() and not out.get_toc() and not out.embfile_count()
        assert not out[0].get_links() and not list(out[0].annots() or [])
        assert not out.metadata['author']


def test_same_form_twice_and_keep_restore(tmp_path):
    src=tmp_path/'copies.pdf';reference,original=document(src,copies=2)
    plan=engine.detect(src,CONFIG);marks=plan['pages'][0]['detections']
    assert len(marks)==1  # one review box controls coincident identical marks
    assert export(src,plan,tmp_path)[0]==reference
    assert export(src,plan,tmp_path,{'ignored':[marks[0]['id']]})[0]==original


@pytest.mark.parametrize('mixed',['text','graphic'])
def test_mixed_forms_are_retained_instead_of_whitening_questions(tmp_path,mixed):
    src=tmp_path/'mixed.pdf';_,original=document(src,mixed=mixed)
    plan=engine.detect(src,CONFIG)
    assert not plan['pages'][0]['detections']
    assert any('retained' in note for note in plan['pages'][0]['warnings'])
    assert export(src,plan,tmp_path)[0]==original


def test_opt_in_and_old_config_compatibility(tmp_path):
    src=tmp_path/'original.pdf';document(src,form=False)
    plan=engine.detect(src,{'text':['EXAMPLE SCHOOL'],'ocr':False})
    assert not plan['config']['watermarks']
    assert all(d['method']=='text' for d in plan['pages'][0]['detections'])
    plan['config'].pop('watermarks')
    plan['planHash']=engine.fingerprint({k:v for k,v in plan.items() if k!='planHash'})
    assert export(src,plan,tmp_path)[1]['outputPages']==1
    with pytest.raises(ValueError,match='watermarks must'):
        engine.validate_config({'watermarks':'yes'})


def test_partly_protected_object_is_kept_whole(tmp_path):
    src=tmp_path/'keep.pdf';_,original=document(src)
    plan=engine.detect(src,CONFIG);r=plan['pages'][0]['detections'][0]['rect']
    small=[r[0],r[1],(r[0]+r[2])/2,(r[1]+r[3])/2]
    assert export(src,plan,tmp_path,{'protected':[{'page':1,'rect':small}]})[0]==original


def test_box_edit_is_explicit_pixel_removal_and_reset_restores_object_mode(tmp_path):
    src=tmp_path/'edit.pdf';reference,original=document(src)
    plan=engine.detect(src,CONFIG);id=plan['pages'][0]['detections'][0]['id']
    rect=[.7,.7,.8,.8]
    pixels,_=export(src,plan,tmp_path,{'adjusted':{id:rect},'output':{'fill':'black'}})
    with fitz.open(src) as doc: pix=doc[0].get_pixmap(dpi=200,alpha=False)
    original_image=Image.frombytes('RGB',(pix.width,pix.height),original)
    edited=Image.frombytes('RGB',original_image.size,pixels)
    assert edited.crop((0,0,pix.width,int(.65*pix.height))).tobytes()==original_image.crop((0,0,pix.width,int(.65*pix.height))).tobytes()
    assert edited.getpixel((int(.75*pix.width),int(.75*pix.height)))==(0,0,0)
    assert export(src,plan,tmp_path,{'adjusted':{}})[0]==reference


def test_manual_remove_and_page_choices_still_apply(tmp_path):
    src=tmp_path/'pages.pdf';reference,_=document(src)
    with fitz.open(src) as doc:
        doc.new_page().insert_text((50,70),'Second page useful text')
        doc.save(tmp_path/'two.pdf')
    src=tmp_path/'two.pdf';plan=engine.detect(src,CONFIG)
    pixels,result=export(src,plan,tmp_path,{'manual':[{'page':1,'rect':[.7,.7,.8,.8]}], 'removePages':[2], 'output':{'fill':'black'}})
    assert result['pageMap']==[{'original':1,'output':1},{'original':2,'output':None}]
    with fitz.open(src) as doc: pix=doc[0].get_pixmap(dpi=200,alpha=False)
    image=Image.frombytes('RGB',(pix.width,pix.height),pixels)
    assert image.getpixel((int(.75*pix.width),int(.75*pix.height)))==(0,0,0)
    assert image.crop((0,0,pix.width,int(.65*pix.height))).tobytes()==Image.frombytes('RGB',image.size,reference).crop((0,0,pix.width,int(.65*pix.height))).tobytes()


@pytest.mark.parametrize('label',['Scanned with CamScanner','www.example.org'])
def test_scanner_and_domain_footer_detection_and_scope(tmp_path,label):
    src=tmp_path/'footer.pdf'
    with fitz.open() as doc:
        p=doc.new_page();p.insert_text((50,300),'Useful questions')
        ref=p.get_pixmap(dpi=200,alpha=False).samples
        p.insert_text((50,810),label);doc.save(src)
    plan=engine.detect(src,{'watermarks':True,'ocr':False})
    assert len(plan['pages'][0]['detections'])==1
    assert export(src,plan,tmp_path)[0]==ref
    assert not engine.detect(src,{'watermarks':True,'ocr':False,'scope':'header'})['pages'][0]['detections']


def test_unknown_repeated_publisher_is_warning_only(tmp_path):
    src=tmp_path/'publisher.pdf'
    with fitz.open() as doc:
        for _ in range(2): doc.new_page().insert_text((50,300),'UNKNOWN PUBLISHER',fontsize=32,color=(.7,.7,.7))
        doc.save(src)
    plan=engine.detect(src,{'watermarks':True,'ocr':False})
    assert not any(p['detections'] for p in plan['pages'])
    assert any('Possible publisher watermark retained' in w for w in plan['pages'][0]['warnings'])


def test_selected_logo_object_preserves_underlying_question(tmp_path):
    src=tmp_path/'logo.pdf'
    im=Image.new('RGB',(80,80),'white');ImageDraw.Draw(im).ellipse((5,5,75,75),outline='black',width=5)
    stream=io.BytesIO();im.save(stream,'PNG')
    with fitz.open() as doc:
        p=doc.new_page();p.insert_text((50,300),'Useful question under the logo')
        ref=p.get_pixmap(dpi=200,alpha=False).samples
        p.insert_image(fitz.Rect(50,275,130,355),stream=stream.getvalue())
        digest=p.get_image_info(hashes=True,xrefs=True)[0]['digest'].hex()
        original=p.get_pixmap(dpi=200,alpha=False).samples;doc.save(src)
    plan=engine.detect(src,{'watermarks':True,'imageDigests':[digest],'ocr':False})
    mark=plan['pages'][0]['detections'][0]
    assert mark['method']=='watermark-image'
    assert export(src,plan,tmp_path)[0]==ref
    assert export(src,plan,tmp_path,{'ignored':[mark['id']]})[0]==original


def test_isolated_scanned_footer_and_keep(tmp_path,monkeypatch):
    src=tmp_path/'scan.pdf'
    with fitz.open() as original:
        p=original.new_page();p.insert_text((50,300),'Useful questions')
        p.insert_text((50,810),'Scanned with CamScanner')
        words=p.get_text('words');png=p.get_pixmap(dpi=200,alpha=False).tobytes('png')
    with fitz.open() as doc:
        doc.new_page().insert_image(fitz.Rect(0,0,595,842),stream=png);doc.save(src)
    monkeypatch.setattr(engine,'ocr_engine',lambda models:'ppocr')
    monkeypatch.setattr(engine.ppocr,'model_identity',lambda models:{})
    monkeypatch.setattr(engine.OcrCache,'words',lambda self,page:words)
    plan=engine.detect(src,{'watermarks':True})
    marks=plan['pages'][0]['detections'];assert len(marks)==1 and marks[0]['method']=='watermark-ocr'
    cleaned,_=export(src,plan,tmp_path)
    kept,_=export(src,plan,tmp_path,{'ignored':[marks[0]['id']]})
    assert cleaned!=kept
    with fitz.open(src) as doc: pix=doc[0].get_pixmap(dpi=200,alpha=False)
    assert cleaned[:pix.width*3*int(pix.height*.88)]==kept[:pix.width*3*int(pix.height*.88)]


def test_inline_image_content_refused():
    with pytest.raises(ValueError,match='Inline image'):
        watermarks.text_operations(b'BI /W 1 /H 1 ID xyz EI')


def test_changed_source_and_cancel_are_rejected(tmp_path):
    src=tmp_path/'change.pdf';document(src);plan=engine.detect(src,CONFIG)
    def cancel(*args): raise RuntimeError('cancelled')
    with pytest.raises(RuntimeError,match='cancelled'): engine.detect(src,CONFIG,progress=cancel)
    with fitz.open(src) as doc:
        doc[0].insert_text((50,500),'Changed source');doc.save(tmp_path/'changed.pdf')
    with pytest.raises(ValueError,match='changed|match'): engine.apply(tmp_path/'changed.pdf',plan,{},tmp_path/'bad.pdf')


def test_url_rule_does_not_box_overlapping_website_watermark(tmp_path):
    src=tmp_path/'website.pdf';ref,_=document(src,form=False,label='www.example.org')
    plan=engine.detect(src,{'watermarks':True,'urls':True,'ocr':False})
    assert [d['method'] for d in plan['pages'][0]['detections']]==['watermark-text']
    assert export(src,plan,tmp_path)[0]==ref


def test_repeated_selected_image_invocations_are_all_removed(tmp_path):
    src=tmp_path/'logos.pdf'
    logo=Image.new('RGB',(80,80),'white');ImageDraw.Draw(logo).ellipse((5,5,75,75),fill='black')
    data=io.BytesIO();logo.save(data,'PNG')
    with fitz.open() as doc:
        page=doc.new_page();page.insert_text((50,300),'Question beneath both logos')
        ref=page.get_pixmap(dpi=200,alpha=False).samples
        for _ in range(2):page.insert_image(fitz.Rect(50,275,130,355),stream=data.getvalue())
        digest=page.get_image_info(hashes=True,xrefs=True)[0]['digest'].hex();doc.save(src)
    plan=engine.detect(src,{'watermarks':True,'imageDigests':[digest],'ocr':False})
    assert len(plan['pages'][0]['detections'])==1
    assert export(src,plan,tmp_path)[0]==ref


def test_overlapping_separate_forms_keep_separate_review_ids(tmp_path):
    src=tmp_path/'offset-forms.pdf'
    with fitz.open() as doc,fitz.open() as mark:
        p=doc.new_page();p.insert_text((50,300),'Question under both marks')
        ref=p.get_pixmap(dpi=200,alpha=False).samples
        mp=mark.new_page();mp.insert_text((50,300),'EXAMPLE SCHOOL',fontsize=32,color=(.7,.7,.7))
        p.show_pdf_page(p.rect,mark,0)
        p.show_pdf_page(fitz.Rect(0,10,595,852),mark,0)
        doc.save(src)
    plan=engine.detect(src,CONFIG);marks=plan['pages'][0]['detections']
    assert len(marks)==2 and len({m['id'] for m in marks})==2
    assert export(src,plan,tmp_path)[0]==ref
    assert export(src,plan,tmp_path,{'ignored':[marks[0]['id']]})[0]!=ref


def test_blended_scanned_wording_is_retained(tmp_path,monkeypatch):
    src=tmp_path/'blended.pdf'
    with fitz.open() as original:
        p=original.new_page();p.insert_text((50,300),'Calculate x + 2 = 4.')
        p.insert_text((50,300),'EXAMPLE SCHOOL',fontsize=42,color=(.7,.7,.7))
        words=p.get_text('words');png=p.get_pixmap(dpi=200,alpha=False).tobytes('png')
    with fitz.open() as doc:doc.new_page().insert_image(fitz.Rect(0,0,595,842),stream=png);doc.save(src)
    monkeypatch.setattr(engine,'ocr_engine',lambda models:'ppocr')
    monkeypatch.setattr(engine.ppocr,'model_identity',lambda models:{})
    monkeypatch.setattr(engine.OcrCache,'words',lambda self,page:words)
    plan=engine.detect(src,{'watermarks':True,'text':['EXAMPLE SCHOOL']})
    assert not plan['pages'][0]['detections']
    assert any('retained in scan/content' in note for note in plan['pages'][0]['warnings'])


def test_searchable_output_reads_cleaned_pixels(tmp_path):
    models=__import__('pathlib').Path(__file__).resolve().parents[1]/'.runtime/models/ppocr'
    if not engine.ppocr.available(models): pytest.skip('PP-OCR models unavailable')
    src=tmp_path/'searchable.pdf';document(src,form=False)
    plan=engine.detect(src,CONFIG);out=tmp_path/'searchable-clean.pdf'
    result=engine.apply(src,plan,{'output':{'searchable':True}},out,models=models)
    with fitz.open(out) as doc:
        text=doc[0].get_text().upper()
        assert 'CALCULATE' in text and 'EXAMPLE' not in text and 'SCHOOL' not in text
    assert not result['imageOnly'] and not result['residual']


@pytest.mark.parametrize('mode', [4, 5, 6, 7])
def test_text_clipping_is_refused_for_text_and_image_probes(mode):
    content = f'BT /F1 42 Tf {mode} Tr (EXAMPLE SCHOOL) Tj ET /Image Do'.encode()
    for image in (False, True):
        with pytest.raises(ValueError, match='Text clipping'):
            watermarks.text_operations(content, image=image)


def test_text_clipping_retains_watermark_and_unrelated_pixels(tmp_path):
    src = tmp_path / 'clip.pdf'
    with fitz.open() as doc:
        page = doc.new_page(width=595, height=842)
        page.insert_font(fontname='helv')
        xref = doc.get_new_xref()
        doc.update_object(xref, '<<>>')
        doc.update_stream(xref, b'q 0.7 g BT /helv 42 Tf 4 Tr 1 0 0 1 50 542 Tm '
                               b'(EXAMPLE SCHOOL) Tj ET 0.2 g 0 0 595 842 re f Q')
        page.set_contents(xref)
        original = page.get_pixmap(dpi=200, alpha=False).samples
        doc.save(src)
    plan = engine.detect(src, CONFIG)
    assert not plan['pages'][0]['detections']
    assert any('Watermark objects retained' in note for note in plan['pages'][0]['warnings'])
    assert export(src, plan, tmp_path)[0] == original


def test_clipping_operator_text_inside_strings_is_not_an_operator():
    assert len(watermarks.text_operations(b'BT (4 Tr) Tj ET')) == 1
