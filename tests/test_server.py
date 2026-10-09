import json
import threading
import urllib.request
import urllib.error
from pathlib import Path
from http.server import ThreadingHTTPServer
import pytest
from pdfscan.server import App, make_handler

@pytest.fixture
def server(tmp_path):
    app=App(tmp_path,0)
    http=ThreadingHTTPServer(("127.0.0.1",0),make_handler(app))
    app.port=http.server_address[1]
    thread=threading.Thread(target=http.serve_forever,daemon=True)
    thread.start()
    yield app,f"http://127.0.0.1:{app.port}"
    http.shutdown()
    http.server_close()

def call(server,path,method="GET",body=None,token=True,extra=None):
    app,url=server
    headers={"Content-Type":"application/json"}
    if token: headers["X-Scan-Token"]=app.token
    headers.update(extra or {})
    req=urllib.request.Request(url+path,data=json.dumps(body).encode() if body is not None else (b"" if method=="POST" else None),method=method,headers=headers)
    try:
        with urllib.request.urlopen(req) as response:
            data=response.read()
            return response.status,json.loads(data) if response.headers.get_content_type()=="application/json" else data
    except urllib.error.HTTPError as error:
        return error.code,json.loads(error.read())

def test_local_host_and_csrf(server):
    assert call(server,"/api/config",extra={"Host":"evil.invalid"})[0]==403
    assert call(server,"/api/demo","POST",token=False)[0]==403
    assert call(server,"/api/demo","POST",extra={"Origin":"https://evil.invalid"})[0]==403
    assert call(server,"/../pdfscan/engine.py")[0]==404

def test_export_requires_exact_preview_and_review(server):
    code,job=call(server,"/api/demo","POST")
    assert code==200
    prefix="/api/jobs/"+job["id"]
    code,plan=call(server,prefix+"/detect","POST",{"config":{"text":["NORTHSTAR STUDIO"],"ocr":False}})
    assert code==200
    assert call(server,prefix+"/export","POST",{"reviewed":True})[0]==400
    code,preview=call(server,prefix+"/preview","POST",{"planHash":plan["planHash"],"decisions":{}})
    assert code==200
    assert call(server,prefix+"/export","POST",{"reviewed":False,"sha256":preview["sha256"]})[0]==400
    assert call(server,prefix+"/export","POST",{"reviewed":True,"sha256":"wrong"})[0]==400
    code,data=call(server,prefix+"/export","POST",{"reviewed":True,"sha256":preview["sha256"]})
    assert code==200 and data.startswith(b"%PDF")
    call(server,prefix+"/detect","POST",{"config":{"text":["OTHER TARGET"],"ocr":False}})
    assert call(server,prefix+"/export","POST",{"reviewed":True,"sha256":preview["sha256"]})[0]==400
    assert call(server,prefix+"/original.pdf")[0]==404
    folder=server[0].jobs[job["id"]]["folder"]
    assert call(server,prefix,"DELETE")[0]==200
    assert not folder.exists()

def test_wrong_plan_hash_rejected(server):
    _,job=call(server,"/api/demo","POST")
    prefix="/api/jobs/"+job["id"]
    call(server,prefix+"/detect","POST",{"config":{"text":[],"ocr":False}})
    assert call(server,prefix+"/preview","POST",{"planHash":"old","decisions":{}})[0]==400


def test_browser_download_is_tied_to_current_review(server):
    _,job=call(server,"/api/demo","POST")
    prefix="/api/jobs/"+job["id"]
    _,plan=call(server,prefix+"/detect","POST",{"config":{"text":["NORTHSTAR STUDIO"],"ocr":False}})
    _,preview=call(server,prefix+"/preview","POST",{"planHash":plan["planHash"],"decisions":{}})
    url=prefix+"/download/"+preview["sha256"]
    assert call(server,url)[0]==400
    assert call(server,prefix+"/approve-export","POST",{"reviewed":False,"sha256":preview["sha256"]})[0]==400
    code,result=call(server,prefix+"/approve-export","POST",{"reviewed":True,"sha256":preview["sha256"]})
    assert code==200 and result["downloadUrl"]==url
    code,data=call(server,url)
    assert code==200 and data.startswith(b"%PDF")
    assert call(server,prefix+"/download/"+"0"*64)[0]==400
    call(server,prefix+"/preview","POST",{"planHash":plan["planHash"],"decisions":{"removePages":[2]}})
    assert call(server,url)[0]==400


def test_reference_upload_is_private_and_invalidates_preview(server):
    import io
    from PIL import Image,ImageDraw
    _,job=call(server,"/api/demo","POST")
    prefix="/api/jobs/"+job["id"]
    _,plan=call(server,prefix+"/detect","POST",{"config":{"ocr":False}})
    _,preview=call(server,prefix+"/preview","POST",{"planHash":plan["planHash"],"decisions":{}})
    image=Image.new("RGB",(80,80),"white");ImageDraw.Draw(image).ellipse((5,5,60,60),fill="black")
    buffer=io.BytesIO();image.save(buffer,"PNG")
    app,url=server
    req=urllib.request.Request(url+prefix+"/logo",data=buffer.getvalue(),method="POST",headers={"Content-Type":"image/png","X-Scan-Token":app.token})
    with urllib.request.urlopen(req) as response: uploaded=json.loads(response.read())
    assert len(uploaded["sha256"])==64
    assert call(server,prefix+"/export","POST",{"sha256":preview["sha256"],"reviewed":True})[0]==400
    code,data=call(server,prefix+"/logo")
    assert code==200 and data.startswith(b"\x89PNG")
    assert call(server,prefix+"/logo","DELETE",token=False)[0]==403
    assert call(server,prefix+"/logo","DELETE")[0]==200
    assert call(server,prefix+"/logo")[0]==400

def test_bundle_zips_current_previews_with_unique_names(server):
    import io, zipfile
    items=[]
    for _ in range(2):
        _,job=call(server,"/api/demo","POST")
        prefix="/api/jobs/"+job["id"]
        _,plan=call(server,prefix+"/detect","POST",{"config":{"text":["NORTHSTAR STUDIO"],"ocr":False}})
        _,preview=call(server,prefix+"/preview","POST",{"planHash":plan["planHash"],"decisions":{}})
        items.append({"id":job["id"],"sha256":preview["sha256"]})
    assert call(server,"/api/bundle","POST",{"items":items})[0]==400
    assert call(server,"/api/bundle","POST",{"items":[{**items[0],"sha256":"0"*64}],"reviewed":True})[0]==400
    assert call(server,"/api/bundle","POST",{"items":[items[0],items[0]],"reviewed":True})[0]==400
    assert call(server,"/api/bundle","POST",{"items":items,"reviewed":True},token=False)[0]==403
    code,result=call(server,"/api/bundle","POST",{"items":items,"reviewed":True})
    assert code==200 and result["files"]==["Synthetic demo (redacted).pdf","Synthetic demo (redacted) (2).pdf"]
    code,data=call(server,result["downloadUrl"])
    assert code==200
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert zf.namelist()==result["files"]
        assert all(zf.read(n).startswith(b"%PDF") for n in zf.namelist())
    # A newer export replaces the older zip.
    _,again=call(server,"/api/bundle","POST",{"items":items[:1],"reviewed":True})
    assert call(server,result["downloadUrl"])[0]==400
    assert call(server,again["downloadUrl"])[0]==200

def test_bad_numbers_and_types_give_plain_errors(server):
    _,job=call(server,"/api/demo","POST")
    prefix="/api/jobs/"+job["id"]
    for path in [prefix+"/original/abc",prefix+"/original/1?w=abc",prefix+"/original/-1"]:
        code,body=call(server,path)
        assert code==400 and "invalid literal" not in body["error"]
    code,body=call(server,"/api/bundle","POST",{"items":[{"id":["x"],"sha256":"y"}],"reviewed":True})
    assert code==400 and body["error"] in ("Invalid request.","Invalid export list.")

def test_deleting_a_document_removes_any_zip(server):
    _,job=call(server,"/api/demo","POST")
    prefix="/api/jobs/"+job["id"]
    _,plan=call(server,prefix+"/detect","POST",{"config":{"text":["NORTHSTAR STUDIO"],"ocr":False}})
    _,preview=call(server,prefix+"/preview","POST",{"planHash":plan["planHash"],"decisions":{}})
    _,result=call(server,"/api/bundle","POST",{"items":[{"id":job["id"],"sha256":preview["sha256"]}],"reviewed":True})
    assert call(server,result["downloadUrl"])[0]==200
    call(server,prefix,"DELETE")
    assert call(server,result["downloadUrl"])[0]==400
    assert not server[0].bundles

def test_startup_removes_leftover_sessions(tmp_path):
    from pdfscan.server import App
    stale=tmp_path/"jobs"/("a"*32)
    stale.mkdir(parents=True)
    (stale/"source.pdf").write_bytes(b"%PDF-1.4")
    App(tmp_path,0)
    assert not stale.exists()


def test_watermark_preview_keep_and_exact_export(server, tmp_path):
    import pymupdf as fitz
    from test_watermarks import document, samples
    src=tmp_path/'watermark.pdf';reference,original=document(src)
    job=server[0].create(src.read_bytes(),'Fictional watermark.pdf')
    prefix='/api/jobs/'+job['id']
    code,plan=call(server,prefix+'/detect','POST',{'config':{'watermarks':True,'text':['EXAMPLE SCHOOL'],'ocr':False}})
    assert code==200 and plan['pages'][0]['detections'][0]['method']=='watermark-text'
    mark=plan['pages'][0]['detections'][0]['id']
    _,preview=call(server,prefix+'/preview','POST',{'planHash':plan['planHash'],'decisions':{}})
    code,data=call(server,prefix+'/export','POST',{'reviewed':True,'sha256':preview['sha256']})
    assert code==200
    saved=tmp_path/'export.pdf';saved.write_bytes(data);assert samples(saved)==reference
    _,kept=call(server,prefix+'/preview','POST',{'planHash':plan['planHash'],'decisions':{'ignored':[mark]}})
    assert kept['sha256']!=preview['sha256']
    assert call(server,prefix+'/export','POST',{'reviewed':True,'sha256':preview['sha256']})[0]==400
    code,data=call(server,prefix+'/export','POST',{'reviewed':True,'sha256':kept['sha256']})
    assert code==200
    saved.write_bytes(data);assert samples(saved)==original
