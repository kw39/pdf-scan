"""Loopback-only review app, using Python's standard-library HTTP server."""
from __future__ import annotations
import hashlib
import json
import re
import secrets
import shutil
import sys
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse
from . import __version__
from .demo import create_demo
from .engine import MAX_BYTES, apply, detect, normalized, open_pdf, page_png, source_hash

ROOT = Path(__file__).resolve().parent.parent
WEB = Path(__file__).resolve().parent / "web"
MAX_JOBS = 60
MAX_BUNDLE = 50
STATIC = {"/": ("index.html", "text/html; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/i18n.js": ("i18n.js", "text/javascript; charset=utf-8"),
          "/style.css": ("style.css", "text/css; charset=utf-8"),
          "/favicon.svg": ("favicon.svg", "image/svg+xml")}
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' blob:; "
       "font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; "
       "frame-ancestors 'none'")

class Cancelled(Exception):
    """Raised inside engine callbacks when the user cancels a long task."""

class App:
    def __init__(self, runtime, port):
        self.runtime = Path(runtime).resolve()
        self.jobs_root = self.runtime / "jobs"
        self.jobs_root.mkdir(parents=True, exist_ok=True)
        self.tessdata = self.runtime / "tessdata"
        self.models = self.runtime / "models" / "ppocr"
        self.port = port
        self.token = secrets.token_urlsafe(32)
        self.jobs = {}
        self.bundles = {}
        self.lock = threading.RLock()
        self.work = threading.BoundedSemaphore(2)
        # Sessions live in memory, so folders left by an earlier run can never be reopened:
        # delete them (and the original PDFs inside) at startup.
        for path in self.jobs_root.iterdir():
            if path.is_dir() and re.fullmatch(r"[0-9a-f]{32}", path.name):
                shutil.rmtree(path, ignore_errors=True)

    def evict(self):
        """Free the least recently used idle session so reloads never lock the app."""
        idle = [j for j in self.jobs.values() if not j["busy"]]
        if not idle:
            raise ValueError("Several documents are still processing. Wait, then try again.")
        oldest = min(idle, key=lambda j: j["touched"])
        self.drop(oldest)

    def drop(self, job):
        with self.lock:
            self.jobs.pop(job["id"], None)
            self.clear_bundles()  # a zip may hold this document's cleaned copy
        shutil.rmtree(job["folder"], ignore_errors=True)

    def clear_bundles(self):
        with self.lock:
            for old in list(self.bundles.values()):
                shutil.rmtree(old.parent, ignore_errors=True)
            self.bundles.clear()

    def create(self, data=None, name="Synthetic demo.pdf"):
        with self.lock:
            while len(self.jobs) >= MAX_JOBS:
                self.evict()
            jobid = secrets.token_hex(16)
            folder = self.jobs_root / jobid
            folder.mkdir()
        source = folder / "source.pdf"
        try:
            rules = create_demo(source) if data is None else None
            if data is not None:
                if b"%PDF" not in data[:1024]:
                    raise ValueError("This file is not a PDF.")
                source.write_bytes(data)
            with open_pdf(source) as doc:
                pages = [{"number": i + 1, "width": p.rect.width, "height": p.rect.height,
                          "images": [{"digest": x["digest"].hex(), "rect": normalized(x["bbox"], p)}
                                     for x in p.get_image_info(hashes=True, xrefs=True)],
                          "text": bool(p.get_text("text").strip())}
                         for i, p in enumerate(doc)]
            safe_name = re.sub(r"[\x00-\x1f\x7f]", "", name.replace("\\", "/").split("/")[-1])[:150]
            job = {"id": jobid, "name": safe_name or "Document.pdf", "folder": folder, "source": source,
                   "pages": pages, "logo": None, "plan": None, "candidate": None, "lock": threading.RLock(),
                   "busy": False, "cancel": False, "progress": None, "touched": time.time()}
            with self.lock:
                self.jobs[jobid] = job
        except Exception:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        return {**self.summary(job), "demoRules": rules}

    def summary(self, job):
        return {"id": job["id"], "name": job["name"], "pages": job["pages"],
                "plan": job["plan"], "candidate": job["candidate"],
                "logo": hashlib.sha256(job["logo"].read_bytes()).hexdigest() if job["logo"] else ""}

    def job(self, jobid):
        with self.lock:
            if jobid not in self.jobs:
                raise ValueError("Document session is unavailable. Open the PDF again.")
            job = self.jobs[jobid]
            job["touched"] = time.time()
            return job

    def bundle(self, raw):
        """Zip several reviewed previews into one download. Each file must match its current preview."""
        items = raw.get("items")
        if (set(raw) - {"items", "reviewed"} or raw.get("reviewed") is not True or not isinstance(items, list)
                or not 1 <= len(items) <= MAX_BUNDLE):
            raise ValueError("Choose between 1 and 50 reviewed files to export.")
        if any(not isinstance(i, dict) or set(i) != {"id", "sha256"} for i in items) or len({i["id"] for i in items}) != len(items):
            raise ValueError("Invalid export list.")
        with self.lock:
            self.clear_bundles()
            bundleid = secrets.token_hex(16)
            folder = self.jobs_root / bundleid
            folder.mkdir()
        target = folder / "bundle.zip"
        try:
            # Copy each preview while holding its lock, then check the copy: what is zipped is what was checked.
            entries, candidates = [], []
            for n, item in enumerate(items):
                job = self.job(str(item["id"]))
                copy = folder / f"{n}.pdf"
                with job["lock"]:
                    candidate = job["candidate"]
                    if not candidate or item["sha256"] != candidate["sha256"]:
                        raise ValueError("A preview changed: " + job["name"] + ". Build its preview again.")
                    shutil.copyfile(job["folder"] / "candidate.pdf", copy)
                if source_hash(copy) != item["sha256"]:
                    raise ValueError("A preview changed: " + job["name"] + ". Build its preview again.")
                entries.append((copy, download_name(job["name"])))
                candidates.append(candidate)
            names, used = [], set()
            for _, name in entries:
                stem, unique, k = name[:-4], name, 2
                while unique.lower() in used:
                    unique, k = f"{stem} ({k}).pdf", k + 1
                used.add(unique.lower())
                names.append(unique)
            with zipfile.ZipFile(target, "w", zipfile.ZIP_STORED) as zf:
                for (path, _), name in zip(entries, names):
                    zf.write(path, name)
            for path, _ in entries:
                path.unlink(missing_ok=True)
        except Exception:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        # Only a complete, checked zip marks its files as reviewed.
        for candidate in candidates:
            candidate["reviewed"] = True
        with self.lock:
            self.bundles[bundleid] = target
        return {"downloadUrl": "/api/bundles/" + bundleid, "filename": f"Redacted PDFs ({len(entries)} files).zip",
                "files": names, "bytes": target.stat().st_size}

def whole(text, message):
    if not re.fullmatch(r"\d{1,6}", text or ""):
        raise ValueError(message)
    return int(text)

def download_name(name):
    stem = re.sub(r"\.pdf$", "", name, flags=re.I).strip() or "document"
    return stem + " (redacted).pdf"

def disposition(name):
    ascii_name = re.sub(r'[^A-Za-z0-9 ._()-]', "_", name) or "redacted.pdf"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name, safe='')}"

def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "PDFScan"
        sys_version = ""

        def log_message(self, *args):
            pass

        def send(self, data, content_type="application/json", code=200, filename=None, cache=False):
            if isinstance(data, (dict, list)):
                data = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "private, max-age=3600" if cache else "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header("Content-Security-Policy", CSP)
            if filename:
                self.send_header("Content-Disposition", disposition(filename))
            self.end_headers()
            self.wfile.write(data)

        def send_file(self, path, content_type, filename=None):
            """Stream a file instead of loading it into memory."""
            path = Path(path)
            size = path.stat().st_size
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header("Content-Security-Policy", CSP)
            if filename:
                self.send_header("Content-Disposition", disposition(filename))
            self.end_headers()
            with open(path, "rb") as stream:
                shutil.copyfileobj(stream, self.wfile, 1 << 20)

        def valid_host(self):
            return self.headers.get("Host") in (f"127.0.0.1:{app.port}", f"localhost:{app.port}")

        def body(self, limit=2 * 1024 * 1024):
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                raise ValueError("Invalid request length.")
            if not 0 < length <= limit:
                raise ValueError("The file is empty or larger than the limit." if limit > 2 * 1024 * 1024
                                 else "Request size is invalid.")
            data = self.rfile.read(length)
            if len(data) != length:
                raise ValueError("Incomplete request.")
            return data

        def json_body(self):
            if self.headers.get_content_type() != "application/json":
                raise ValueError("Expected JSON.")
            try:
                value = json.loads(self.body())
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise ValueError("Invalid JSON.") from exc
            if not isinstance(value, dict):
                raise ValueError("Expected an object.")
            return value

        def tracker(self, job):
            def progress(stage, done, total):
                job["progress"] = {"stage": stage, "done": done, "total": total}
                if job["cancel"]:
                    raise Cancelled()
            return progress

        def run(self, job, fn):
            job["busy"], job["cancel"], job["progress"] = True, False, None
            try:
                with app.work:
                    return fn(self.tracker(job))
            except Cancelled:
                raise ValueError("Cancelled. Nothing was changed.")
            finally:
                job["busy"], job["progress"] = False, None

        def route(self, method):
            if not self.valid_host():
                return self.send({"error": "Invalid local host."}, code=403)
            if method in ("POST", "DELETE"):
                if self.headers.get("X-Scan-Token") != app.token:
                    return self.send({"error": "Reload the local app before making changes."}, code=403)
                origin = self.headers.get("Origin")
                if origin and origin not in (f"http://127.0.0.1:{app.port}", f"http://localhost:{app.port}"):
                    return self.send({"error": "Requests must come from this local app."}, code=403)
            url = urlparse(self.path)
            path, query = url.path, parse_qs(url.query)
            if method == "GET" and path in STATIC:
                name, mime = STATIC[path]
                return self.send((WEB / name).read_bytes(), mime)
            font = re.fullmatch(r"/fonts/([a-z0-9-]+\.woff2)", path)
            if method == "GET" and font and (WEB / "fonts" / font[1]).is_file():
                return self.send((WEB / "fonts" / font[1]).read_bytes(), "font/woff2", cache=True)
            if method == "GET" and path == "/api/config":
                models = sorted(p.stem for p in app.tessdata.glob("*.traineddata"))
                from .ppocr import available
                return self.send({"token": app.token, "models": models, "version": __version__,
                                  "ocrEngine": "ppocr" if available(app.models) else "tesseract"})
            if method == "POST" and path == "/api/demo":
                return self.send(app.create())
            if method == "POST" and path == "/api/upload":
                if self.headers.get_content_type() != "application/pdf":
                    raise ValueError("Choose a PDF file.")
                return self.send(app.create(self.body(MAX_BYTES), unquote(self.headers.get("X-Filename", "Document.pdf"))))
            if method == "POST" and path == "/api/bundle":
                return self.send(app.bundle(self.json_body()))
            b = re.fullmatch(r"/api/bundles/([0-9a-f]{32})", path)
            if method == "GET" and b:
                target = app.bundles.get(b[1])
                if not target or not target.is_file():
                    raise ValueError("This zip is no longer available. Export again.")
                with zipfile.ZipFile(target) as zf:
                    count = len(zf.namelist())
                return self.send_file(target, "application/zip", filename=f"Redacted PDFs ({count} files).zip")
            m = re.fullmatch(r"/api/jobs/([0-9a-f]{32})(?:/(.*))?", path)
            if not m:
                return self.send({"error": "Not found."}, code=404)
            job = app.job(m[1])
            action = m[2] or ""
            # Lock-free status routes so the UI can poll while work is running.
            if method == "GET" and action == "progress":
                return self.send({"busy": job["busy"], "progress": job["progress"]})
            if method == "POST" and action == "cancel":
                job["cancel"] = job["busy"]
                return self.send({"cancelling": job["busy"]})
            if method == "GET" and action.startswith("original/"):
                num = whole(action.split("/")[-1], "Invalid page.")
                width = max(80, min(2600, whole(query.get("w", ["1300"])[0], "Invalid image width.")))
                return self.send(page_png(job["source"], num, width, trusted=True), "image/png")
            with job["lock"]:
                if method == "GET" and not action:
                    return self.send(app.summary(job))
                if method == "DELETE" and not action:
                    app.drop(job)
                    return self.send({"deleted": True})
                if method == "POST" and action == "logo":
                    from .visual import logo_png, MAX_LOGO_BYTES
                    data = logo_png(self.body(MAX_LOGO_BYTES))
                    logo = job["folder"] / "reference.png"
                    logo.write_bytes(data)
                    job["logo"] = logo
                    job["plan"] = None
                    job["candidate"] = None
                    return self.send({"sha256": hashlib.sha256(data).hexdigest()})
                if method == "GET" and action == "logo":
                    if not job["logo"]:
                        raise ValueError("No reference logo uploaded.")
                    return self.send(job["logo"].read_bytes(), "image/png")
                if method == "DELETE" and action == "logo":
                    if job["logo"]:
                        job["logo"].unlink(missing_ok=True)
                    job["logo"] = None
                    job["plan"] = None
                    job["candidate"] = None
                    return self.send({"deleted": True})
                if method == "POST" and action == "detect":
                    raw = self.json_body()
                    if set(raw) - {"config", "forceOcr"} or type(raw.get("forceOcr", False)) is not bool:
                        raise ValueError("Invalid detection request.")
                    plan = self.run(job, lambda progress: detect(
                        job["source"], raw.get("config", {}), cache_dir=job["folder"], tessdata=app.tessdata,
                        force_ocr=raw.get("forceOcr", False), logo_path=job["logo"], progress=progress,
                        models=app.models))
                    job["plan"] = plan
                    job["candidate"] = None
                    (job["folder"] / "candidate.pdf").unlink(missing_ok=True)
                    return self.send(plan)
                if method == "POST" and action == "preview":
                    raw = self.json_body()
                    if not job["plan"] or raw.get("planHash") != job["plan"]["planHash"]:
                        raise ValueError("Detect and review the current rules first.")
                    result = self.run(job, lambda progress: apply(
                        job["source"], job["plan"], raw.get("decisions", {}), job["folder"] / "candidate.pdf",
                        tessdata=app.tessdata, progress=progress, models=app.models))
                    result["revision"] = secrets.token_hex(8)
                    result["filename"] = download_name(job["name"])
                    job["candidate"] = result
                    return self.send(result)
                if method == "GET" and action.startswith("candidate/"):
                    parts = action.split("/")
                    candidate = job["candidate"]
                    if len(parts) != 3 or not candidate or parts[1] != candidate["revision"]:
                        raise ValueError("Preview changed. Generate it again.")
                    width = max(80, min(2600, whole(query.get("w", ["1300"])[0], "Invalid image width.")))
                    return self.send(page_png(job["folder"] / "candidate.pdf", whole(parts[2], "Invalid page."), width, trusted=True),
                                     "image/png")
                if method == "POST" and action in ("export", "approve-export"):
                    raw = self.json_body()
                    candidate = job["candidate"]
                    if not candidate or raw.get("sha256") != candidate["sha256"] or raw.get("reviewed") is not True:
                        raise ValueError("Inspect the current preview and acknowledge the review before exporting.")
                    path = job["folder"] / "candidate.pdf"
                    if source_hash(path) != candidate["sha256"]:
                        raise ValueError("Preview file changed. Generate it again.")
                    if action == "approve-export":
                        candidate["reviewed"] = True
                        return self.send({"downloadUrl": "/api/jobs/" + job["id"] + "/download/" + candidate["sha256"],
                                          "filename": download_name(job["name"])})
                    return self.send_file(path, "application/pdf", filename=download_name(job["name"]))
                if method == "GET" and action.startswith("download/"):
                    candidate = job["candidate"]
                    sha = action.split("/")[-1]
                    if not candidate or candidate.get("reviewed") is not True or sha != candidate["sha256"]:
                        raise ValueError("This reviewed download is unavailable. Generate and review the current preview.")
                    path = job["folder"] / "candidate.pdf"
                    if source_hash(path) != sha:
                        raise ValueError("Preview changed. Generate it again.")
                    return self.send_file(path, "application/pdf", filename=download_name(job["name"]))
                if method == "GET" and action == "report":
                    if not job["plan"]:
                        raise ValueError("Detect first.")
                    return self.send({"document": job["name"], "plan": job["plan"], "candidate": job["candidate"]})
            return self.send({"error": "Not found."}, code=404)

        def dispatch(self, method):
            try:
                self.route(method)
            except ValueError as exc:
                self.send({"error": str(exc)}, code=400)
            except (KeyError, TypeError):
                self.send({"error": "Invalid request."}, code=400)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:
                self.send({"error": "Processing failed. Check the PDF and local runtime."}, code=500)

        def do_GET(self): self.dispatch("GET")
        def do_POST(self): self.dispatch("POST")
        def do_DELETE(self): self.dispatch("DELETE")
    return Handler

class LocalServer(ThreadingHTTPServer):
    # On Windows, SO_REUSEADDR lets a second server share a busy port; refuse instead.
    allow_reuse_address = sys.platform != "win32"

def serve(port=8765, runtime=None, open_browser=False):
    app = App(runtime or Path.cwd() / ".runtime", port)
    try:
        server = LocalServer(("127.0.0.1", port), make_handler(app))
    except OSError as exc:
        raise OSError(f"Port {port} is already in use. Close the other app or choose another port with --port.") from exc
    server.daemon_threads = True
    print(f"PDF Scan {__version__} is ready at http://127.0.0.1:{port}", flush=True)
    if open_browser:
        import webbrowser
        try:
            webbrowser.open(f"http://127.0.0.1:{port}")
        except Exception:
            print("Browser could not open automatically. Open the address above manually.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
