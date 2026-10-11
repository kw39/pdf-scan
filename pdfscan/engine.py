"""Standalone engine. PDF text is data, never executable instructions."""
from __future__ import annotations
import hashlib
import io
import json
import math
import os
import re
import sys
import tempfile
import threading
import unicodedata
from contextlib import contextmanager
from pathlib import Path

import numpy as np
import pymupdf as fitz
from PIL import Image, ImageDraw
from . import ppocr
from . import primitives as p
from . import watermarks as w

ENGINE = "pdfscan-raster-1"
MAX_PAGES = 500
MAX_BYTES = 50 * 1024 * 1024
LIMIT_SIDE = 14400
MAX_PIXELS_SIDE = 5000

def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False).encode()).hexdigest()

def source_hash(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()

def rect_valid(rect):
    return (isinstance(rect, list) and len(rect) == 4 and
            all(type(v) in (int, float) and math.isfinite(v) and 0 <= v <= 1 for v in rect)
            and rect[0] < rect[2] and rect[1] < rect[3])

def normalized(box, page, pad=0):
    box = (fitz.Rect(box) * page.rotation_matrix) & page.rect
    return [max(0, box.x0 / page.rect.width - pad), max(0, box.y0 / page.rect.height - pad),
            min(1, box.x1 / page.rect.width + pad), min(1, box.y1 / page.rect.height + pad)]

def render_dpi(page, requested=200):
    return max(1, min(requested, int(MAX_PIXELS_SIDE * 72 / max(page.rect.width, page.rect.height))))

def open_pdf(path):
    if Path(path).stat().st_size > MAX_BYTES:
        raise ValueError("PDF exceeds the 50 MiB limit.")
    doc = fitz.open(path)
    if not doc.is_pdf or doc.is_encrypted or not 1 <= len(doc) <= MAX_PAGES:
        doc.close()
        raise ValueError("Use an unencrypted PDF with 1-500 pages.")
    for page in doc:
        if max(page.rect.width, page.rect.height) > LIMIT_SIDE:
            doc.close()
            raise ValueError("Page dimensions exceed the processing limit.")
    return doc

def validate_config(raw):
    if not isinstance(raw, dict):
        raise ValueError("Rules must be an object.")
    allowed = {"text", "caseSensitive", "flexible", "scope", "ocr", "languages", "preset",
               "imageDigests", "email", "phone", "cards", "ids", "autoLogos", "urls", "qr", "watermarks", "logoSha256"}
    if set(raw) - allowed:
        raise ValueError("Unknown rule fields: " + ", ".join(sorted(set(raw) - allowed)))
    c = {"text": [], "caseSensitive": False, "flexible": True, "scope": "all",
         "ocr": True, "languages": "eng+chi_tra", "preset": "generic",
         "imageDigests": [], "email": False, "phone": False, "cards": False, "ids": False,
         "autoLogos": False, "urls": False, "qr": False, "watermarks": False, "logoSha256": "", **raw}
    if (not isinstance(c["text"], list) or len(c["text"]) > 100 or
            any(not isinstance(t, str) or not t.strip() or len(t) > 200 for t in c["text"])):
        raise ValueError("Provide at most 100 nonempty text targets, 200 characters each.")
    c["text"] = list(dict.fromkeys(t.strip() for t in c["text"]))
    for k in ("caseSensitive", "flexible", "ocr", "email", "phone", "cards", "ids", "autoLogos", "urls", "qr", "watermarks"):
        if type(c[k]) is not bool:
            raise ValueError(k + " must be true or false.")
    if c["scope"] not in ("all", "header", "footer"):
        raise ValueError("Invalid page scope.")
    if c["preset"] not in ("generic", "exam"):
        raise ValueError("Invalid preset.")
    if not isinstance(c["languages"], str) or not re.fullmatch(r"[a-z][a-z0-9_]*(?:\+[a-z][a-z0-9_]*)*", c["languages"]):
        raise ValueError("Invalid OCR languages.")
    if (not isinstance(c["imageDigests"], list) or len(c["imageDigests"]) > 100 or
            any(not isinstance(d, str) or not re.fullmatch(r"[0-9a-f]{32}", d) for d in c["imageDigests"])):
        raise ValueError("Invalid image selections.")
    if not isinstance(c["logoSha256"],str) or (c["logoSha256"] and not re.fullmatch(r"[0-9a-f]{64}",c["logoSha256"])):
        raise ValueError("Invalid reference logo identity.")
    for t in c["text"]:
        if not match_key(t, c):
            raise ValueError("Text targets must include a letter, number or symbol that can be matched.")
    return c

_ZH_FOLD = None

def zh_fold():
    """Traditional -> Simplified character folding (OpenCC data), so 學號 matches 学號."""
    global _ZH_FOLD
    if _ZH_FOLD is None:
        data = json.loads((Path(__file__).parent / "data" / "zh_fold.json").read_text("utf-8"))
        _ZH_FOLD = str.maketrans(data["from"], data["to"])
    return _ZH_FOLD

def match_key(text, c):
    text = unicodedata.normalize("NFKC", text)
    if not c["caseSensitive"]:
        text = text.casefold()
    if c["flexible"]:
        return re.sub(r"[^\w\u3400-\u9fff]", "", text.translate(zh_fold()))
    return re.sub(r"\s+", " ", text).strip()

def allowed_edits(key):
    """How many OCR character errors a close match may contain."""
    cjk = sum("\u3400" <= ch <= "\u9fff" for ch in key)
    if cjk * 2 >= len(key):
        return 1 if len(key) >= 4 else 0  # 3-character names must match exactly
    return 0 if len(key) < 6 else 1 if len(key) < 12 else 2

def within_edits(a, b, limit):
    """Bounded Levenshtein distance check."""
    if abs(len(a) - len(b)) > limit:
        return False
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i] + [0] * len(b)
        best = i
        for j, cb in enumerate(b, 1):
            current[j] = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb))
            best = min(best, current[j])
        if best > limit:
            return False
        previous = current
    return previous[-1] <= limit

def in_scope(rect, scope):
    return scope == "all" or (scope == "header" and rect[3] <= .25) or (scope == "footer" and rect[1] >= .75)

@contextmanager
def quiet_native_output():
    """Silence console chatter from native OCR libraries (e.g. Tesseract's
    "Image too small to scale!!"). Python's own output is unaffected afterwards."""
    with _QUIET_LOCK:
        try:
            sys.stdout.flush()
            sys.stderr.flush()
            saved = [os.dup(1), os.dup(2)]
            null = os.open(os.devnull, os.O_WRONLY)
        except (OSError, ValueError, AttributeError):
            yield
            return
        try:
            os.dup2(null, 1)
            os.dup2(null, 2)
            yield
        finally:
            os.dup2(saved[0], 1)
            os.dup2(saved[1], 2)
            for fd in (*saved, null):
                os.close(fd)

_QUIET_LOCK = threading.Lock()

def ocr_engine(models):
    """PP-OCR when its models are installed; Tesseract otherwise."""
    return "ppocr" if models and ppocr.available(models) else "tesseract"

class OcrCache:
    """Disposable private cache; its key includes source, engine, models, languages and runtime."""
    def __init__(self, source, cache_dir, languages, tessdata, force=False, models=None):
        self.path = Path(cache_dir) / "ocr.json" if cache_dir else None
        self.languages = languages
        self.tessdata = str(tessdata) if tessdata else None
        self.models = str(models) if models else None
        self.engine = ocr_engine(models)
        self.cached = None
        self.fresh = []
        self.hits = 0
        self.misses = 0
        if self.engine == "ppocr":
            hashes = ppocr.model_identity(models)
        else:
            tess = Path(tessdata or os.environ.get("TESSDATA_PREFIX", ""))
            hashes = {}
            for lang in languages.split("+"):
                model = tess / (lang + ".traineddata")
                if not model.is_file():
                    raise ValueError("Missing OCR model: " + lang + ". Run the OCR setup script.")
                hashes[lang] = source_hash(model)
        self.identity = {"source": source_hash(source), "engine": self.engine,
                         "languages": languages if self.engine == "tesseract" else "eng+chi",
                         "models": hashes, "runtime": list(fitz.version), "dpi": 200,
                         "maxSide": MAX_PIXELS_SIDE, "schema": 2}
        if self.path and self.path.exists() and not force:
            try:
                if self.path.stat().st_size > 64 * 1024 * 1024:
                    raise ValueError("Large cache")
                obj = json.loads(self.path.read_text("utf-8"))
                words = obj["words"]
                with open_pdf(source) as doc:
                    count = len(doc)
                if obj["identity"] == self.identity and len(words) == count and obj["checksum"] == fingerprint(words):
                    if not all(isinstance(page, list) and len(page) < 200000 and
                               all(isinstance(w, list) and len(w) in (8, 9) and isinstance(w[4], str) and
                                   all(type(v) in (int, float) and math.isfinite(v) for v in w[:4]) and
                                   all(type(v) is int for v in w[5:8]) and
                                   (len(w) == 8 or (isinstance(w[8], list) and len(w[8]) == len(w[4]) and
                                    all(isinstance(q, list) and len(q) == 2 and all(type(v) in (int, float) for v in q) for q in w[8])))
                                   for w in page) for page in words):
                        raise ValueError("Invalid words")
                    self.cached = words
            except (ValueError, KeyError, TypeError, OSError):
                pass

    def words(self, page):
        if self.cached is not None:
            self.hits += 1
            return self.cached[page.number]
        try:
            if self.engine == "ppocr":
                words = ppocr.page_words(page, self.models, dpi=render_dpi(page))
            else:
                with quiet_native_output():
                    tp = page.get_textpage_ocr(language=self.languages, dpi=render_dpi(page),
                                               full=True, tessdata=self.tessdata)
                words = page.get_text("words", textpage=tp, sort=False)
        except Exception as exc:
            raise ValueError("OCR could not finish. Check the selected models and PDF.") from exc
        self.misses += 1
        self.fresh.append([list(w) for w in words])
        return words

    def save(self):
        if not self.path or self.cached is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps({"identity": self.identity, "words": self.fresh,
                              "checksum": fingerprint(self.fresh)}, ensure_ascii=False, allow_nan=False)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(encoded, encoding="utf-8")
        temporary.replace(self.path)

def overlap(a, b):
    a, b = fitz.Rect(a), fitz.Rect(b)
    return (a & b).get_area() / max(1e-9, min(a.get_area(), b.get_area()))

def add_detection(page_report, source_sha, rect, rule, label, method, matched=None, **extra):
    if not rect_valid(rect):
        return
    object_method = method in ('watermark-text', 'watermark-image')
    if any(d['rule'] == rule and
           (all(abs(a-b) < 1e-7 for a,b in zip(d['rect'],rect)) if object_method
            else overlap(d['rect'],rect) > .65) for d in page_report['detections']):
        return
    identity = {"source": source_sha, "page": page_report["number"], "rule": rule,
                "rect": [round(v, 5) for v in rect]}
    detection = {"id": fingerprint(identity)[:24], "rect": rect, "rule": rule,
                 "label": label, "method": method, "matched": matched, **extra}
    page_report["detections"].append(detection)

_PIECE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]|[^\W\d_\u3400-\u9fff\uf900-\ufaff]+|\d+|[^\w\s]+|_+")
_HELV = None

def char_widths(text):
    """Approximate advance widths: Helvetica metrics for Latin, one em for CJK."""
    global _HELV
    if _HELV is None:
        _HELV = fitz.Font("helv")
    out = []
    for ch in text:
        if "\u2e80" <= ch <= "\u9fff" or "\uf900" <= ch <= "\uffef":
            out.append(1.0)
        else:
            out.append(max(.25, _HELV.text_length(ch, fontsize=1)))
    return out

def split_words(line):
    """Split words at punctuation, letter/digit changes and CJK characters.

    "INV-ACME-2026" -> "INV", "-", "ACME", "-", "2026", so a target can be a code
    inside a longer token. Letters inside a word are never split, so "cat" still
    does not match "category". Each piece records its source word and character
    range. Its box uses OCR character positions when the reader provides them,
    otherwise font width ratios; typed text is refined later with exact search.
    """
    out = []
    for index, w in enumerate(line):
        text = w[4]
        pieces = list(_PIECE.finditer(text))
        if len(pieces) <= 1:
            out.append((*w[:8], (index, 0, len(text))))
            continue
        x0, y0, x1, y1 = w[:4]
        spans = w[8] if len(w) > 8 and isinstance(w[8], list) and len(w[8]) == len(text) else None
        horizontal = (x1 - x0) >= (y1 - y0)
        length = (x1 - x0) if horizontal else (y1 - y0)
        if spans is None or not horizontal:
            widths = char_widths(text)
            total = sum(widths) or 1.0
            edges = [0.0]
            for wd in widths:
                edges.append(edges[-1] + wd / total)
            spans = [[edges[i], edges[i + 1]] for i in range(len(text))]
            pad = 0.12
        else:
            pad = 0.06
        for m in pieces:
            a, b = spans[m.start()][0], spans[m.end() - 1][1]
            unit = (b - a) / max(1, m.end() - m.start())
            a, b = max(0.0, a - pad * unit) * length, min(1.0, b + pad * unit) * length
            box = (x0 + a, y0, x0 + b, y1) if horizontal else (x0, y0 + a, x1, y0 + b)
            out.append((*box, m.group(0), *w[5:8], (index, m.start(), m.end())))
    return out

def exact_native_box(page, line, pieces, start, end, estimate):
    """Exact rectangle for a match inside typed text, using the PDF's own search."""
    (i0, s0, _), (i1, _, e1) = pieces[start][8], pieces[end][8]
    if i0 == i1:
        raw = line[i0][4][s0:e1]
    else:
        raw = " ".join([line[i0][4][s0:]] + [w[4] for w in line[i0 + 1:i1]] + [line[i1][4][:e1]])
    if not raw.strip():
        return estimate
    margin = max(estimate.height, 2)
    clip = fitz.Rect(estimate.x0 - margin, estimate.y0 - margin, estimate.x1 + margin, estimate.y1 + margin)
    try:
        hits = [h for h in page.search_for(raw, clip=clip) if h.intersects(estimate)]
    except Exception:
        return estimate
    if not hits:
        return estimate
    best = max(hits, key=lambda h: (h & estimate).get_area())
    return best | estimate if best.width < estimate.width * .5 else best

def exact_pattern_box(page, line, spans, match, fallback):
    """Locate a native ID substring without including its enclosing word label."""
    estimate = fitz.Rect()
    for word, (a, b) in zip(line, spans):
        if a >= match.end() or b <= match.start():
            continue
        box = fitz.Rect(word[:4])
        widths = char_widths(word[4])
        total = sum(widths) or 1.0
        lo, hi = max(0, match.start() - a), min(len(widths), match.end() - a)
        if box.width >= box.height:
            estimate |= fitz.Rect(box.x0 + box.width * sum(widths[:lo]) / total, box.y0,
                                  box.x0 + box.width * sum(widths[:hi]) / total, box.y1)
        else:
            estimate |= box
    margin = max(fallback.height, 2)
    clip = fallback + (-margin, -margin, margin, margin)
    try:
        hits = [hit for hit in page.search_for(match.group(0), clip=clip) if hit.intersects(fallback)]
    except Exception:
        return fallback
    if not hits:
        return fallback
    best = max(hits, key=lambda hit: (hit & estimate).get_area())
    # A search can return separate rectangles for mixed-font fragments. Retain
    # the complete word bounds unless this rectangle contains the entire ID.
    compact = lambda value: re.sub(r"\s+", "", value).casefold()
    return best if compact(match.group(0)) in compact(page.get_textbox(best)) else fallback


def match_text(words, page, report, sha, c, method):
    # Block/line IDs describe logical text order, including rotated native text.
    # No unrotated-y threshold is used to split displayed lines.
    lines = {}
    for w in words:
        lines.setdefault(tuple(w[5:7]), []).append(w)
    targets = [(t, match_key(t, c), "text:" + fingerprint([t, c["caseSensitive"], c["flexible"]])[:16])
               for t in c["text"]]
    # OCR text can contain small reading errors; allow close matches there only.
    fuzzy = method == "ocr" and c["flexible"]
    edits = {tk: (allowed_edits(tk) if fuzzy else 0) for _, tk, _ in targets}
    longest = max((len(tk) + edits[tk] for _, tk, _ in targets), default=0)
    for line in lines.values():
        pieces = split_words(line) if targets else line
        candidates = []  # (edits, -length, start, end, target, rule, box, text)
        for start in range(len(pieces)):
            if not match_key(pieces[start][4], c):
                continue  # never start a match on bare punctuation such as "-"
            box = fitz.Rect(pieces[start][:4])
            combined = ""
            for end in range(start, min(len(pieces), start + 60)):
                w = pieces[end]
                combined += (" " if combined else "") + w[4]
                box |= fitz.Rect(w[:4])
                key = match_key(combined, c)
                if len(key) > longest:
                    break
                if not match_key(w[4], c):
                    continue  # nor end one there
                for target, tk, rule in targets:
                    if key == tk:
                        candidates.append((0, -len(key), start, end, target, rule, fitz.Rect(box), combined))
                    elif edits[tk] and within_edits(key, tk, edits[tk]):
                        distance = next(d for d in range(1, edits[tk] + 1) if within_edits(key, tk, d))
                        candidates.append((distance, -len(key), start, end, target, rule, fitz.Rect(box), combined))
        # Exact matches first; then the closest, longest close matches that don't overlap them.
        taken = []
        for distance, _, start, end, target, rule, box, text in sorted(candidates, key=lambda x: x[:2]):
            if distance and any(r == rule and not (end < s0 or start > e0) for r, s0, e0 in taken):
                continue
            taken.append((rule, start, end))
            first, last = pieces[start][8], pieces[end][8]
            starts_word, ends_word = first[1] == 0, last[2] == len(line[last[0]][4])
            padded = box
            if method in ("text", "decoded-text") and not (starts_word and ends_word):
                padded = exact_native_box(page, line, pieces, start, end, box)
            elif method == "ocr":
                # OCR boxes hug glyph centres: widen at word edges; inside a word
                # (e.g. "ACME" in "INV-ACME-2026") keep close to the neighbours.
                h = box.height
                padded = fitz.Rect(box.x0 - (.18 if starts_word else .04) * h, box.y0 - .08 * h,
                                   box.x1 + (.18 if ends_word else .04) * h, box.y1 + .08 * h)
            rect = normalized(padded, page, .002)
            if not in_scope(rect, c["scope"]):
                continue
            parts = [normalized(q[:4], page) for q in pieces[start:end + 1]]
            if distance:
                add_detection(report, sha, rect, rule, "Close match", method, target,
                              read=text, approximate=True, _parts=parts)
            else:
                add_detection(report, sha, rect, rule, "Text match", method, target, _parts=parts)
        patterns = active_patterns(c)
        if not patterns:
            continue
        # Patterns run on the whole logical line so values written with spaces
        # ("+852 2345 6789", "4111 1111 1111 1111") are found as one candidate.
        spans, text = [], ""
        for w in line:
            if text:
                text += " "
            spans.append((len(text), len(text) + len(w[4])))
            text += w[4]
        for label, pattern, check in patterns:
            for m in pattern.finditer(text):
                value = m.group(0)
                if check and not check(value):
                    continue
                if label == "phone" and c["cards"] and luhn_card(value):
                    continue
                picked = [w for w, (a, b) in zip(line, spans) if a < m.end() and b > m.start()]
                if not picked:
                    continue
                box = fitz.Rect(picked[0][:4])
                for w in picked[1:]:
                    box |= fitz.Rect(w[:4])
                native_id = label == "id" and method in ("text", "decoded-text")
                if native_id:
                    box = exact_pattern_box(page, line, spans, m, box)
                rect = normalized(box, page, .002)
                if in_scope(rect, c["scope"]):
                    extra = {"_parts": [normalized(box, page)]} if native_id else {}
                    add_detection(report, sha, rect, "pattern:" + label, PATTERN_LABELS[label], method, value, **extra)

def digits(value):
    return re.sub(r"\D", "", value)

def luhn_card(value):
    number = digits(value)
    if not 13 <= len(number) <= 19 or len(set(number)) == 1:
        return False
    total = 0
    for i, ch in enumerate(reversed(number)):
        n = int(ch)
        if i % 2:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
    return total % 10 == 0

def phone_like(value):
    number = digits(value)
    if not 7 <= len(number) <= 15:
        return False
    # ISO dates and simple ranges are common false positives.
    return not re.fullmatch(r"\s*(?:19|20)\d\d[-./](?:0?[1-9]|1[0-2])[-./]\d{1,2}\s*", value)

def hkid_valid(value):
    m = re.fullmatch(r"([A-Z]{1,2})(\d{6})\s?\(?([0-9A])\)?", value.upper())
    if not m:
        return False
    letters = m[1].rjust(2, " ")
    total = sum((9 - i) * (36 if ch == " " else ord(ch) - 55) for i, ch in enumerate(letters))
    total += sum((7 - i) * int(d) for i, d in enumerate(m[2]))
    check = (11 - total % 11) % 11
    return m[3] == ("A" if check == 10 else str(check))

def taiwan_id_valid(value):
    codes = "ABCDEFGHJKLMNPQRSTUVXYWZIO"
    value = value.upper()
    if not re.fullmatch(r"[A-Z][12]\d{8}", value):
        return False
    n = codes.index(value[0]) + 10
    total = n // 10 + (n % 10) * 9 + sum(int(d) * w for d, w in zip(value[1:9], range(8, 0, -1))) + int(value[9])
    return total % 10 == 0

def china_id_valid(value):
    value = value.upper()
    if not re.fullmatch(r"\d{17}[\dX]", value):
        return False
    weights = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
    check = "10X98765432"[sum(int(d) * w for d, w in zip(value, weights)) % 11]
    return value[-1] == check

def ssn_valid(value):
    a, b, c = value.split("-")
    return a not in ("000", "666") and not a.startswith("9") and b != "00" and c != "0000"

def id_valid(value):
    compact = value.replace(" ", "")
    return (hkid_valid(compact) or taiwan_id_valid(compact) or china_id_valid(compact)
            or (re.fullmatch(r"\d{3}-\d{2}-\d{4}", compact) is not None and ssn_valid(compact)))

PATTERN_LABELS = {"url": "URL candidate", "email": "Email candidate", "phone": "Phone candidate",
                  "card": "Payment card number", "id": "ID number"}
PATTERNS = {
    "url": (re.compile(r"(?i)(?:https?://|www\.)[^\s<>]+|(?<![@A-Za-z0-9.])(?:[a-z0-9-]+\.)+(?:com|org|net|edu|gov|io|co|hk|uk|cn|tw|test)(?:/[^\s<>]*)?"), None),
    "email": (re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), None),
    # Lookarounds are ASCII-only: "電話+852 2345 6789" must still match after Chinese text.
    "phone": (re.compile(r"(?<![A-Za-z0-9+])\+?\(?\d[\d ().-]{5,}\d(?![A-Za-z0-9])"), phone_like),
    "card": (re.compile(r"(?<![\d-])(?:\d[ -]?){12,18}\d(?![\d-])"), luhn_card),
    "id": (re.compile(r"(?<![A-Za-z0-9])(?:[A-Za-z]{1,2}\d{6}\s?\(?[0-9Aa]\)?|[A-Za-z][12]\d{8}|\d{17}[\dXx]|\d{3}-\d{2}-\d{4})(?![A-Za-z0-9])"), id_valid),
}

def active_patterns(c):
    keys = [("url", "urls"), ("email", "email"), ("phone", "phone"), ("card", "cards"), ("id", "ids")]
    return [(label, *PATTERNS[label]) for label, option in keys if c[option]]

def watermark_rule(region):
    if region['reason'].startswith('Scanner-app'):
        return 'watermark:scanner'
    if region['reason'].startswith('Website'):
        return 'watermark:website'
    return 'watermark:text:' + fingerprint(w.compact(region['match']))[:16]


def watermark_findings(page, config, repeats, words=()):
    """Verified proposals and uncertain labels; PDF operation offsets stay private."""
    regions, candidates, warnings = w.plan(page, config['text'], repeats)
    margin, notes = w.ocr_footer_regions(page, words, candidates) if words else ([], [])
    flattened = w.scan_text_candidates(page, words, config['text']) if words else []
    warnings += notes
    warnings += ['Possible text watermark retained in scan/content; inspect manually: ' + r['match']
                 for r in flattened]
    return ([r for r in [*regions, *margin] if in_scope(r['rect'], config['scope'])],
            [*candidates, *flattened], warnings)


def watermark_duplicate(detection, candidate):
    """Avoid a box redaction for the same watermark identified by another rule."""
    rule = detection['rule']
    if not (rule.startswith('text:') or rule in ('pattern:url', 'pattern:hyperlink')):
        return False
    if overlap(detection['rect'], candidate['rect']) <= .1:
        return False
    label = candidate['match']
    matched = w.compact(detection.get('matched') or '')
    labels = {w.compact(label), *(w.compact(m.group()) for m in w.SCANNER.finditer(label))}
    if matched in labels:
        return True
    if rule in ('pattern:url', 'pattern:hyperlink'):
        def url_key(value):
            return w.compact(re.sub(r'^(?:https?://)?(?:www\.)?', '', value, flags=re.I))
        return url_key(detection.get('matched') or '') in {url_key(m.group()) for m in w.DOMAIN.finditer(label)}
    return False


def add_watermarks(report, sha, regions):
    for region in regions:
        add_detection(report, sha, region['rect'], watermark_rule(region), region['reason'],
                      region['method'], region['match'])


def detect(source, config, *, cache_dir=None, tessdata=None, force_ocr=False, logo_path=None, progress=None,
           models=None):
    """Create a review plan. ``progress(stage, done, total)`` may raise to cancel."""
    c = validate_config(config)
    reference=None
    if logo_path:
        from .visual import logo_png
        data=Path(logo_path).read_bytes()
        reference=logo_png(data)
        identity=hashlib.sha256(reference).hexdigest()
        if c["logoSha256"] and c["logoSha256"]!=identity:
            raise ValueError("Reference logo changed. Detect again with the current logo.")
        c["logoSha256"]=identity
    elif c["logoSha256"]:
        raise ValueError("Upload the reference logo before finding targets.")
    sha = source_hash(source)
    report = {"schema": 1, "engine": ENGINE, "sourceSha256": sha, "config": c, "pages": [],
              "warnings": ["Review every page. OCR and heuristic detections can miss targets."],
              "ocr": {"reusedPages": 0, "freshPages": 0}}
    cache = OcrCache(source, cache_dir, c["languages"], tessdata, force_ocr, models) if c["ocr"] else None
    prepared = []
    selected = set(c["imageDigests"])
    logo_digests = set()
    image_cache = {}
    with open_pdf(source) as doc:
        repeats = w.repeated_labels(doc) if c["watermarks"] else {}
        for i, page in enumerate(doc):
            if progress:
                progress("detect", i, len(doc))
            pr = {"number": i + 1, "width": page.rect.width, "height": page.rect.height,
                  "detections": [], "images": [], "removeProposed": False, "warnings": []}
            native = page.get_text("words", sort=False)
            ocr = cache.words(page) if cache else []
            watermark_regions, watermark_candidates = [], []
            if c["watermarks"]:
                watermark_regions, watermark_candidates, notes = watermark_findings(page, c, repeats, ocr)
                pr['warnings'].extend(notes)
            match_text(native, page, pr, sha, c, "text")
            if ocr:
                match_text(ocr, page, pr, sha, c, "ocr")
            decoded = p.unshift_words(native)
            if decoded != [tuple(w) for w in native]:
                match_text(decoded, page, pr, sha, c, "decoded-text")
            native_lines = [line for block in page.get_text("dict", flags=0)["blocks"]
                            if block.get("type") == 0 for line in block.get("lines", [])]
            sideways = page.rotation in (90, 270) or any(abs(line.get("dir", (1, 0))[1]) > .05 for line in native_lines)
            if sideways:
                # Horizontal ink-band fitting is not valid for sideways text.
                # Keep its complete native/OCR bounds instead of trimming letters.
                for d in pr["detections"]:
                    d.pop("_parts", None)
            for d in pr["detections"]:
                rect = d["rect"]
                if (rect[3]-rect[1])*page.rect.height > 1.25*(rect[2]-rect[0])*page.rect.width:
                    d.pop("_parts", None)
            if any("_parts" in d for d in pr["detections"]):
                pix = page.get_pixmap(dpi=render_dpi(page), alpha=False)
                gray = Image.open(io.BytesIO(pix.tobytes("png"))).convert("L")
                for d in pr["detections"]:
                    parts = d.pop("_parts", None)
                    if parts:
                        if d["method"] in ("text", "decoded-text") and d["rule"] == "pattern:id":
                            # Native ID characters have exact bounds; one pixel
                            # covers their antialiased edges without a tall font box.
                            d["rect"] = p.snap_to_ink(gray, d["rect"], parts, pad=1)
                            # Italic glyphs can protrude beyond native advance bounds.
                            # Follow only ink touching the left edge, with a bounded reach.
                            rect = d["rect"]
                            left = max(0, math.floor(rect[0] * gray.width))
                            top = max(0, math.floor(rect[1] * gray.height))
                            bottom = min(gray.height, math.ceil(rect[3] * gray.height))
                            reach = max(1, int(max(q[3] - q[1] for q in parts) * gray.height * .2))
                            stop = max(0, left - reach)
                            while left > stop and gray.crop((left, top, left + 1, bottom)).getextrema()[0] < 255:
                                left -= 1
                            rect[0] = min(rect[0], left / gray.width)
                        else:
                            d["rect"] = p.snap_to_ink(gray, d["rect"], parts)
                        if d["method"] == "text" and d["rule"].startswith("text:"):
                            # Trim only empty native-text padding. Ink in the margin
                            # (including italic overhangs) retains the full old bounds.
                            rect = d["rect"]
                            left = rect[0] + max(0, .002 - 1 / gray.width)
                            strip = (int(rect[0] * gray.width), int(rect[1] * gray.height),
                                     int(left * gray.width), min(gray.height, math.ceil(rect[3] * gray.height)))
                            if strip[2] > strip[0] and strip[3] > strip[1] and gray.crop(strip).getextrema() == (255, 255):
                                rect[0] = left
                        d["id"] = fingerprint({"source": sha, "page": pr["number"], "rule": d["rule"],
                                               "rect": [round(v, 5) for v in d["rect"]]})[:24]
            if c["watermarks"]:
                # A text-target rectangle would hide questions under a watermark.
                # Verified objects get their own review proposal; uncertain marks stay intact.
                pr['detections'] = [d for d in pr['detections'] if not any(
                    watermark_duplicate(d, r) for r in watermark_candidates)]
                add_watermarks(pr, sha, watermark_regions)
            if not c["ocr"]:
                pr["warnings"].append("OCR disabled: image text has not been checked.")
            elif not native:
                pr["warnings"].append("Scanned page: inspect OCR detections and artwork carefully.")
            if render_dpi(page) < 200:
                pr["warnings"].append("Large page rendered at reduced resolution.")
            images = page.get_image_info(hashes=True, xrefs=True)
            for info in images:
                rect = normalized(info["bbox"], page)
                if rect_valid(rect) and not p.blank_image(page, info["xref"], image_cache):
                    pr["images"].append({"digest": info["digest"].hex(), "rect": rect})
            if c["urls"]:
                for link in page.get_links():
                    if link.get("uri") and link.get("from"):
                        rect=normalized(fitz.Rect(link["from"])*page.derotation_matrix,page,.002)
                        if in_scope(rect,c["scope"]):
                            add_detection(pr,sha,rect,"pattern:hyperlink","Hyperlink area","hyperlink",link["uri"])
            if c["qr"] or c["autoLogos"] or reference:
                from .visual import rendered,qr_regions,reference_regions,possible_logos
                gray=rendered(page)
                if c["qr"]:
                    for rect in qr_regions(gray):
                        if in_scope(rect,c["scope"]):
                            add_detection(pr,sha,rect,"visual:qr","QR code","qr-code")
                if reference:
                    for rect,score in reference_regions(gray,reference):
                        if in_scope(rect,c["scope"]):
                            add_detection(pr,sha,rect,"visual:reference:"+c["logoSha256"],"Reference logo match","logo-reference",confidence=round(score,3))
                    pr["warnings"].append("Reference logo matching checks similar shapes at different sizes and right-angle rotations. Review matches and misses.")
                if c["autoLogos"]:
                    readable=[normalized(word[:4],page) for word in [*native,*ocr]]
                    for rect in possible_logos(gray,readable):
                        if in_scope(rect,c["scope"]) and not any(d["method"] in ("qr-code","logo-reference","logo-heuristic") and overlap(d["rect"],rect)>.65 for d in pr["detections"]):
                            add_detection(pr,sha,rect,"visual:logo","Possible logo","logo-heuristic",heuristic=True)
                    for info in images:
                        rect=normalized(info["bbox"],page)
                        if rect_valid(rect) and .015<rect[2]-rect[0]<.3 and .015<rect[3]-rect[1]<.18 and (rect[3]<.25 or rect[1]>.75) and not p.blank_image(page,info["xref"],image_cache):
                            if in_scope(rect,c["scope"]) and not any(d["method"] in ("qr-code","logo-reference","logo-heuristic") and overlap(d["rect"],rect)>.65 for d in pr["detections"]):
                                add_detection(pr,sha,rect,"visual:embedded-logo","Possible embedded logo","logo-heuristic",heuristic=True)
                    pr["warnings"].append("Automatic logos are guesses from compact header/footer artwork, not brand recognition. Restore diagrams or decorative artwork that should stay.")
            if c["preset"] == "exam":
                text = p.decode_page_text(page.get_text()) if native else " ".join(w[4] for w in ocr)
                _, flat = p.flatten(text)
                instruction_only, cues = p.instruction_page(text, p.has_diagram(page))
                question = p.question_content(flat, bool(p.FORMULA_REFERENCE.search(flat)))
                heading = any(d["rule"].startswith("text:") and d["rect"][1] < .25 for d in pr["detections"])
                cover = heading and bool(re.search(r"exam|mock|試卷|考試", text, re.I)) and not question and len(text) < 450
                pr["removeProposed"] = instruction_only or cover
                if pr["removeProposed"]:
                    pr["warnings"].append("Exam preset proposes removing an instruction/cover page. Verify there is no useful content.")
                if heading:
                    for info in images:
                        box = fitz.Rect(info["bbox"]) * page.rotation_matrix
                        digest = info["digest"].hex()
                        if p.blank_image(page, info["xref"], image_cache):
                            continue
                        header = box.height < page.rect.height * .22 and box.width < page.rect.width * .4 and box.y1 < page.rect.height * .25
                        cover_crest = (instruction_only or cover) and p.crest_sized(box, page)
                        if not (header or cover_crest):
                            continue
                        # Only local OCR readings; no external image service.
                        readings = [p.image_text(fitz.Rect(info["bbox"]), [*native, *ocr])]
                        close, colour = close_image_read(page, info["bbox"], c, tessdata, models)
                        readings += close
                        if p.exam_label_image(fitz.Rect(info["bbox"]), [*native, *ocr], c["text"], close) or p.form_field_image(box, readings, c["text"], colour):
                            pr["warnings"].append("Exam label or answer form preserved; verify the image.")
                        else:
                            logo_digests.add(digest)
                if not native and heading:
                    propose_scan_clusters(page, [*native, *ocr], pr, sha)
            if c['watermarks']:
                pr['detections'] = [d for d in pr['detections'] if not any(
                    watermark_duplicate(d,r) for r in watermark_candidates)]
            prepared.append((page.number, images))
            report["pages"].append(pr)
        known_images = {im["digest"] for pr in report["pages"] for im in pr["images"]}
        if selected - known_images:
            raise ValueError("A selected repeated image is unavailable in this PDF.")
        for number, images in prepared:
            page = doc[number]
            pr = report["pages"][number]
            for info in images:
                digest = info["digest"].hex()
                if digest in selected or digest in logo_digests:
                    object_region = w.image_plan(page, info) if c['watermarks'] else None
                    add_detection(pr, sha, normalized(info["bbox"], page),
                                  "image:" + digest, "Selected repeated image" if digest in selected else "Possible crest or heading logo",
                                  "watermark-image" if object_region else "image-match", digest,
                                  heuristic=digest not in selected)
        if all(pr["removeProposed"] for pr in report["pages"]):
            for pr in report["pages"]:
                pr["removeProposed"] = False
                pr["warnings"].append("Automatic page removal suppressed because it would empty the PDF.")
    if progress:
        progress("detect", len(report["pages"]), len(report["pages"]))
    if cache:
        cache.save()
        report["ocr"] = {"reusedPages": cache.hits, "freshPages": cache.misses, "engine": cache.engine}
    report["planHash"] = fingerprint(report)
    return report

def close_image_read(page, bbox, c, tessdata, models=None):
    if not c["ocr"]:
        return [], 1.0
    try:
        clip = fitz.Rect(bbox) * page.rotation_matrix & page.rect
        pix = page.get_pixmap(dpi=min(300, render_dpi(page)), clip=clip, alpha=False)
        rgb = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
        sat = rgb.convert("HSV").getchannel("S").point(lambda v: 255 if v > 90 else 0)
        colour = sat.histogram()[255] / (rgb.width * rgb.height)
        if min(rgb.size) < 16:  # slivers and rules carry no readable text
            return [], colour
        readings = []
        for im in (rgb, p.strip_rules(rgb.convert("L"), 30).convert("RGB")):
            if ocr_engine(models) == "ppocr":
                words = ppocr.image_words(np.asarray(im), models)
                readings.append(" ".join(w[4] for w in words))
                continue
            buf = io.BytesIO()
            im.save(buf, "PNG")
            with quiet_native_output():
                data = fitz.Pixmap(buf.getvalue()).pdfocr_tobytes(language=c["languages"], tessdata=str(tessdata) if tessdata else None)
            with fitz.open("pdf", data) as o:
                readings.append(o[0].get_text())
        return readings, colour
    except Exception:
        return [], 1.0

def propose_scan_clusters(page, words, pr, sha):
    left = min(d["rect"][0] for d in pr["detections"] if d["rect"][1] < .25)
    pix = page.get_pixmap(matrix=fitz.Matrix(.7 * p.page_scale(page), .7 * p.page_scale(page)), alpha=False)
    small = Image.open(io.BytesIO(pix.tobytes("png"))).convert("L")
    from PIL import ImageFilter
    mask = small.point(lambda v: 255 if v < 160 else 0).filter(ImageFilter.MaxFilter(5))
    width, height = mask.size
    lx, ly = int(left * width) - 4, int(height * .13)
    pixels, seen = mask.load(), set()
    readable = p.readable_boxes(words, page)
    for y in range(2, ly):
        for x in range(2, max(2, lx)):
            if (x, y) in seen or not pixels[x, y]:
                continue
            todo, points = [(x, y)], []
            seen.add((x, y))
            while todo:
                a, b = todo.pop()
                points.append((a, b))
                for nx, ny in ((a-1,b), (a+1,b), (a,b-1), (a,b+1)):
                    if 1 <= nx < lx and 1 <= ny < ly and (nx, ny) not in seen and pixels[nx, ny]:
                        seen.add((nx, ny))
                        todo.append((nx, ny))
            x0, x1 = min(q[0] for q in points), max(q[0] for q in points)
            y0, y1 = min(q[1] for q in points), max(q[1] for q in points)
            if x1-x0 > width*.035 and y1-y0 > height*.023 and len(points) > 100:
                share, _ = p.text_share(points, width, height, readable)
                if share < .85:
                    add_detection(pr, sha, [max(0,(x0-3)/width),max(0,(y0-3)/height),
                                           min(1,(x1+4)/width),min(1,(y1+4)/height)],
                                  "exam:scan-cluster", "Possible scanned emblem", "scan-header-cluster",
                                  heuristic=True)

def validate_plan(plan):
    if not isinstance(plan, dict) or "planHash" not in plan:
        raise ValueError("Invalid detection plan.")
    if fingerprint({k: v for k, v in plan.items() if k != "planHash"}) != plan["planHash"]:
        raise ValueError("The detection plan changed. Detect again.")
    return plan

def validate_decisions(plan, raw):
    if not isinstance(raw, dict) or set(raw) - {"ignored", "manual", "protected", "removePages", "keepPages", "adjusted", "output"}:
        raise ValueError("Invalid review decisions.")
    d = {"ignored": [], "manual": [], "protected": [], "removePages": [], "keepPages": [], "adjusted": {}, **raw}
    d["output"] = validate_output(d.get("output", {}))
    count = len(plan["pages"])
    ids = {r["id"] for pr in plan["pages"] for r in pr["detections"]}
    if not isinstance(d["ignored"], list) or any(not isinstance(i, str) or i not in ids for i in d["ignored"]):
        raise ValueError("Unknown restored detection.")
    # Boxes the reviewer moved or resized: detection id -> replacement rectangle.
    if not isinstance(d["adjusted"], dict) or len(d["adjusted"]) > 20000:
        raise ValueError("An adjusted box is invalid. Reset it and try again.")
    for key, rect in d["adjusted"].items():
        if key not in ids or not rect_valid(rect):
            raise ValueError("An adjusted box is invalid. Reset it and try again.")
    for field in ("removePages", "keepPages"):
        if not isinstance(d[field], list) or any(type(i) is not int or not 1 <= i <= count for i in d[field]):
            raise ValueError("Invalid page decision.")
    if set(d["removePages"]) & set(d["keepPages"]):
        raise ValueError("A page cannot be both kept and removed.")
    for field in ("manual", "protected"):
        if not isinstance(d[field], list) or len(d[field]) > 1000:
            raise ValueError("Too many drawn regions.")
        for r in d[field]:
            if not isinstance(r, dict) or set(r) != {"page", "rect"} or type(r["page"]) is not int or not 1 <= r["page"] <= count or not rect_valid(r["rect"]):
                raise ValueError("Invalid drawn region.")
    return d

OUTPUT_DEFAULTS = {"fill": "white", "quality": "lossless", "searchable": False}

def validate_output(raw):
    if not isinstance(raw, dict) or set(raw) - set(OUTPUT_DEFAULTS):
        raise ValueError("Invalid export options.")
    o = {**OUTPUT_DEFAULTS, **raw}
    if o["fill"] not in ("white", "black") or o["quality"] not in ("lossless", "compact") or type(o["searchable"]) is not bool:
        raise ValueError("Invalid export options.")
    return o

def cleaned_image(page, rects, protected=(), fill="white", drawn=(), watermark_regions=()):
    """Fill boxes, restore Keep areas, then fill drawn areas that sit inside a Keep area.

    Nesting decides overlaps: a Keep area inside a drawn Remove area stays (remove all
    but this), and a Remove area drawn inside a Keep area is removed (keep all but this)."""
    pix = page.get_pixmap(dpi=render_dpi(page), alpha=False)
    original = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
    if watermark_regions:
        pix = w.render(page, watermark_regions, render_dpi(page))
        im = Image.open(io.BytesIO(pix.tobytes('png'))).convert('RGB')
    else:
        im = original.copy()
    draw = ImageDraw.Draw(im)
    def box(r):
        return (max(0, math.floor(r[0]*im.width)), max(0, math.floor(r[1]*im.height)),
                min(im.width, math.ceil(r[2]*im.width)), min(im.height, math.ceil(r[3]*im.height)))
    inside = lambda r: any(k[0] <= r[0] and k[1] <= r[1] and r[2] <= k[2] and r[3] <= k[3] for k in protected)
    nested = [r for r in drawn if inside(r)]
    for rect in [*rects, *(r for r in drawn if not inside(r))]:
        x0, y0, x1, y1 = box(rect)
        draw.rectangle((x0,y0,x1-1,y1-1), fill=fill)
    # Keep regions are an explicit user decision; automatic restoration is disabled.
    for rect in protected:
        b = box(rect)
        im.paste(original.crop(b), b[:2])
    for rect in nested:
        x0, y0, x1, y1 = box(rect)
        draw.rectangle((x0,y0,x1-1,y1-1), fill=fill)
    return im

def encode_page(im, quality):
    stream = io.BytesIO()
    if quality == "compact":
        im.save(stream, "JPEG", quality=82, optimize=True, progressive=True, subsampling="4:2:0")
    else:
        im.save(stream, "PNG", optimize=False)
    return stream.getvalue()

def ocr_words(im, languages, tessdata, models=None):
    """OCR the already-cleaned pixels. Only visible, kept content can be read."""
    if ocr_engine(models) == "ppocr":
        try:
            return ppocr.image_words(np.asarray(im.convert("RGB")), models)
        except Exception as exc:
            raise ValueError("Searchable export could not read the cleaned pages.") from exc
    stream = io.BytesIO()
    im.save(stream, "PNG")
    try:
        with quiet_native_output():
            data = fitz.Pixmap(stream.getvalue()).pdfocr_tobytes(language=languages,
                                                                tessdata=str(tessdata) if tessdata else None)
    except Exception as exc:
        raise ValueError("Searchable export needs the OCR models. Run the OCR setup script.") from exc
    with fitz.open("pdf", data) as doc:
        page = doc[0]
        sx, sy = im.width / page.rect.width, im.height / page.rect.height
        return [(w[0] * sx, w[1] * sy, w[2] * sx, w[3] * sy, *w[4:]) for w in page.get_text("words", sort=False)]

_FONTS = {}

def font_for(text):
    name = "helv" if all(ord(ch) < 0x250 for ch in text) else "cjk"
    if name not in _FONTS:
        _FONTS[name] = fitz.Font(name)
    return _FONTS[name]

def add_text_layer(dest, words, width, height):
    """Invisible text aligned to OCR word boxes, for search, copy and screen readers."""
    writer = fitz.TextWriter(dest.rect)
    sx, sy = dest.rect.width / width, dest.rect.height / height
    count = 0
    for w in words:
        text = w[4].strip()
        if not text:
            continue
        box = fitz.Rect(w[0] * sx, w[1] * sy, w[2] * sx, w[3] * sy)
        if box.is_empty or box.height < 1:
            continue
        font = font_for(text)
        size = box.height * .9
        length = font.text_length(text, fontsize=size)
        if length > box.width > 0:
            size *= box.width / length
        try:
            writer.append((box.x0, box.y1 - box.height * .18), text, font=font, fontsize=max(1, size))
            count += 1
        except Exception:
            continue
    if count:
        writer.write_text(dest, render_mode=3)
    return count

def residual_targets(words, config):
    """Report configured text targets still readable in the cleaned output."""
    found = set()
    lines = {}
    for w in words:
        lines.setdefault(tuple(w[5:7]), []).append(w)
    lines = {k: [w[4] for w in split_words(v)] for k, v in lines.items()}
    keys = {match_key(t, config): t for t in config.get("text", [])}
    for line in lines.values():
        for start in range(len(line)):
            combined = ""
            for end in range(start, min(len(line), start + 40)):
                combined += (" " if combined else "") + line[end]
                key = match_key(combined, config)
                if key in keys:
                    found.add(keys[key])
    return sorted(found)

def apply(source, plan, decisions, output, *, tessdata=None, progress=None, models=None):
    validate_plan(plan)
    if source_hash(source) != plan["sourceSha256"]:
        raise ValueError("The source PDF changed. Detect again.")
    if Path(source).resolve() == Path(output).resolve():
        raise ValueError("The original cannot be overwritten.")
    d = validate_decisions(plan, decisions)
    out = d["output"]
    ignored = set(d["ignored"])
    removed = {pr["number"] for pr in plan["pages"] if pr["removeProposed"]} - set(d["keepPages"])
    removed |= set(d["removePages"])
    if len(removed) >= len(plan["pages"]):
        raise ValueError("Keep at least one page.")
    if out["searchable"] and ocr_engine(models) == "tesseract":
        tess = Path(tessdata or os.environ.get("TESSDATA_PREFIX", ""))
        for lang in plan["config"]["languages"].split("+"):
            if not (tess / (lang + ".traineddata")).is_file():
                raise ValueError("Searchable export needs the OCR model: " + lang + ". Run the OCR setup script.")
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    page_map, residual = [], []
    total = len(plan["pages"])
    with open_pdf(source) as doc, fitz.open() as public:
        repeats = w.repeated_labels(doc) if plan['config'].get('watermarks') else {}
        for pr in plan["pages"]:
            num = pr["number"]
            if progress:
                progress("apply", num - 1, total)
            if num in removed:
                page_map.append({"original": num, "output": None})
                continue
            page = doc[num-1]
            drawn = [r["rect"] for r in d["manual"] if r["page"] == num]
            keep = [r["rect"] for r in d["protected"] if r["page"] == num]
            rects, objects = [], []
            active = [r for r in pr['detections'] if r['id'] not in ignored]
            native_objects = [r for r in active if r['method'] == 'watermark-text'
                              and r['id'] not in d['adjusted'] and not any(overlap(r['rect'], k) > 0 for k in keep)]
            verified = []
            if native_objects:
                verified, _, _ = watermark_findings(page, plan['config'], repeats)
            images = page.get_image_info(hashes=True, xrefs=True) if any(r['method'] == 'watermark-image' for r in active) else []
            for region in active:
                if region['id'] in d['adjusted']:
                    # Editing a proposal deliberately selects pixel removal at the edited box.
                    rects.append(d['adjusted'][region['id']])
                    continue
                if region['method'] not in ('watermark-text', 'watermark-image'):
                    rects.append(region['rect'])
                    continue
                # Keep areas cancel the whole object, including areas only partly covered.
                if any(overlap(region['rect'], k) > 0 for k in keep):
                    continue
                if region['method'] == 'watermark-text':
                    matches = [r for r in verified if '_operation' in r and watermark_rule(r) == region['rule']
                               and all(abs(a-b) < 1e-7 for a,b in zip(r['rect'],region['rect']))]
                else:
                    matches = []
                    excluded = {tuple(r['_operation']) for r in objects}
                    for info in images:
                        if info['digest'].hex() != region['matched'] or not all(
                                abs(a-b) < 1e-7 for a,b in zip(normalized(info['bbox'],page),region['rect'])):
                            continue
                        match = w.image_plan(page, info, excluded)
                        if match is not None:
                            matches.append(match)
                            excluded.add(tuple(match['_operation']))
                if not matches:
                    raise ValueError('Watermark removal could not be verified. Find again or keep this proposal.')
                objects.extend(matches)
            im = cleaned_image(page, rects, keep, out["fill"], drawn, objects)
            dest = public.new_page(width=page.rect.width, height=page.rect.height)
            dest.insert_image(dest.rect, stream=encode_page(im, out["quality"]))
            if out["searchable"]:
                words = ocr_words(im, plan["config"]["languages"], tessdata, models)
                add_text_layer(dest, words, im.width, im.height)
                left = residual_targets(words, plan["config"])
                if left:
                    residual.append({"page": num, "output": len(public), "targets": left})
            page_map.append({"original": num, "output": len(public)})
        if progress:
            progress("apply", total, total)
        public.set_metadata({})
        if out["searchable"]:
            public.subset_fonts()
        fd, tmp = tempfile.mkstemp(suffix=".pdf", dir=output.parent)
        os.close(fd)
        try:
            public.save(tmp, garbage=4, deflate=True)
            with fitz.open(tmp) as check:
                if (len(check) != len(public) or check.get_toc() or check.embfile_count() or
                    any((not out["searchable"] and page.get_text().strip()) or list(page.annots() or [])
                        or list(page.widgets() or []) or page.get_links() for page in check)):
                    raise ValueError("Cleaned PDF validation failed.")
                for name in ("title", "author", "subject", "keywords", "creator", "producer"):
                    if check.metadata.get(name):
                        raise ValueError("Metadata was not removed.")
            os.replace(tmp, output)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    return {"sha256": source_hash(output), "planHash": plan["planHash"],
            "decisionHash": fingerprint(d), "pageMap": page_map, "removedPages": sorted(removed),
            "outputPages": total - len(removed), "imageOnly": not out["searchable"],
            "output": out, "bytes": output.stat().st_size, "residual": residual}

def page_png(source, number, max_width=1300, trusted=False):
    """Render a page preview. ``trusted`` skips limits for files already validated."""
    with (fitz.open(source) if trusted else open_pdf(source)) as doc:
        if type(number) is not int or not 1 <= number <= len(doc):
            raise ValueError("Invalid page.")
        page = doc[number-1]
        scale = min(max_width / page.rect.width, 1.6 * max_width / page.rect.height, 4)
        return page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False).tobytes("png")
