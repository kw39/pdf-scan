"""Local PP-OCR text reader (detection + recognition) on ONNX Runtime.

Reads Traditional and Simplified Chinese and English, including text in ruled
tables that Tesseract's layout analysis skips. Models are PaddleOCR's PP-OCR
networks (Apache-2.0), installed by scripts/setup_ocr.py. Nothing is uploaded.

Returns PyMuPDF-style words: (x0, y0, x1, y1, text, block, line, word) in the
page's unrotated coordinate space, with per-word boxes taken from the CTC
time steps, so a name inside a longer line gets its own tight box.
"""
from __future__ import annotations

import math
import re
import threading
from pathlib import Path

import cv2
import numpy as np

MODEL_FILES = ("det.onnx", "rec.onnx", "dict.txt")
_CJK = r"⺀-⿟　-〿぀-ヿ㄀-ㄯㆠ-ㆿ㐀-䶿一-鿿豈-﫿＀-￯"
_TOKEN = re.compile(rf"[{_CJK}]|[^\s{_CJK}]+")

def available(model_dir) -> bool:
    return bool(model_dir) and all((Path(model_dir) / name).is_file() for name in MODEL_FILES)

class Reader:
    """Thread-safe, lazily loaded reader. One instance per model directory."""
    _instances: dict = {}
    _lock = threading.Lock()

    @classmethod
    def get(cls, model_dir):
        key = str(Path(model_dir).resolve())
        with cls._lock:
            if key not in cls._instances:
                cls._instances[key] = cls(key)
            return cls._instances[key]

    def __init__(self, model_dir, threads=None):
        import onnxruntime as ort  # imported lazily: only needed when OCR runs
        options = ort.SessionOptions()
        options.log_severity_level = 3  # keep the console quiet
        if threads:
            options.intra_op_num_threads = threads
        providers = ["CPUExecutionProvider"]
        model_dir = Path(model_dir)
        self.det = ort.InferenceSession(str(model_dir / "det.onnx"), options, providers=providers)
        self.rec = ort.InferenceSession(str(model_dir / "rec.onnx"), options, providers=providers)
        chars = (model_dir / "dict.txt").read_text("utf-8").splitlines()
        self.charset = ["<blank>"] + chars + [" "]
        self.run_lock = threading.Lock()

    # ---------- detection ----------
    def detect(self, bgr, limit=960, thresh=0.2, box_thresh=0.45, unclip=1.5):
        h, w = bgr.shape[:2]
        scale = 1.0
        if max(h, w) > limit * 3:  # keep memory bounded on very large renders
            scale = limit * 3 / max(h, w)
        rh, rw = max(32, int(round(h * scale / 32) * 32)), max(32, int(round(w * scale / 32) * 32))
        img = cv2.resize(bgr, (rw, rh)).astype(np.float32) / 255.0
        img = (img - np.array([0.485, 0.456, 0.406], np.float32)) / np.array([0.229, 0.224, 0.225], np.float32)
        tensor = img.transpose(2, 0, 1)[None]
        with self.run_lock:
            pred = self.det.run(None, {self.det.get_inputs()[0].name: tensor})[0][0, 0]
        bitmap = (pred > thresh).astype(np.uint8)
        contours, _ = cv2.findContours(bitmap, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        boxes = []
        sx, sy = w / rw, h / rh
        for contour in contours[:3000]:
            if len(contour) < 3:
                continue
            (cx, cy), (bw, bh), angle = cv2.minAreaRect(contour)
            if min(bw, bh) < 2:
                continue
            x, y, cw, ch = cv2.boundingRect(contour)
            mask = np.zeros((ch, cw), np.uint8)
            cv2.fillPoly(mask, [contour.reshape(-1, 2) - [x, y]], 1)
            if cv2.mean(pred[y:y + ch, x:x + cw], mask)[0] < box_thresh:
                continue
            area, perimeter = bw * bh, 2 * (bw + bh)
            d = area * unclip / max(perimeter, 1e-6)  # DB "unclip" for a rectangle
            pts = cv2.boxPoints(((cx, cy), (bw + 2 * d, bh + 2 * d), angle))
            pts[:, 0] = np.clip(pts[:, 0] * sx, 0, w - 1)
            pts[:, 1] = np.clip(pts[:, 1] * sy, 0, h - 1)
            pts = order(pts)
            if np.linalg.norm(pts[0] - pts[1]) > 3 and np.linalg.norm(pts[0] - pts[3]) > 3:
                boxes.append(pts)
        # Reading order: top to bottom, then left to right.
        boxes.sort(key=lambda b: (round(b[:, 1].mean() / 12), b[:, 0].min()))
        return boxes

    # ---------- recognition ----------
    def recognise(self, bgr, boxes, batch=8, height=48):
        return self._recognise([warp(bgr, quad) for quad in boxes], batch, height)

    def _recognise(self, crops, batch=8, height=48):
        order_ = sorted(range(len(crops)), key=lambda i: crops[i][0].shape[1] / max(1, crops[i][0].shape[0]))
        results = [None] * len(crops)
        for start in range(0, len(order_), batch):
            idx = order_[start:start + batch]
            ratios = [crops[i][0].shape[1] / max(1, crops[i][0].shape[0]) for i in idx]
            width = int(math.ceil(height * min(max(max(ratios), 320 / 48), 80)))
            tensor = np.zeros((len(idx), 3, height, width), np.float32)
            used = []
            for k, i in enumerate(idx):
                crop = crops[i][0]
                rw = min(width, int(math.ceil(height * crop.shape[1] / max(1, crop.shape[0]))))
                img = cv2.resize(crop, (max(1, rw), height)).astype(np.float32) / 255.0
                tensor[k, :, :, :rw] = ((img - 0.5) / 0.5).transpose(2, 0, 1)
                used.append(rw)
            with self.run_lock:
                out = self.rec.run(None, {self.rec.get_inputs()[0].name: tensor})[0]
            steps = out.shape[1]
            per_step = width / steps
            for k, i in enumerate(idx):
                results[i] = self.decode(out[k], per_step / max(1, used[k]))
        return results

    def decode(self, probs, step_fraction):
        """CTC greedy decode, keeping each character's span as a 0..1 fraction of the crop width.

        CTC peaks mark character centres, so edges are placed halfway between
        neighbouring centres, and the outer edges half a character pitch out.
        """
        best = probs.argmax(1)
        conf = probs.max(1)
        chars, previous = [], 0
        for t, idx in enumerate(best):
            if idx != 0 and idx != previous and idx < len(self.charset):
                chars.append([self.charset[idx], t, t, float(conf[t])])
            elif idx != 0 and idx == previous and chars:
                chars[-1][2] = t
            previous = idx
        if not chars:
            return []
        centres = [(c[1] + c[2] + 1) / 2 * step_fraction for c in chars]
        gaps = [b - a for a, b in zip(centres, centres[1:]) if b > a]
        pitch = float(np.median(gaps)) if gaps else 4 * step_fraction
        # Recover spaces the model dropped: a gap clearly wider than the pitch.
        if len(centres) > 2:
            for i in range(len(chars) - 1, 0, -1):
                a, b, gap = chars[i - 1][0], chars[i][0], centres[i] - centres[i - 1]
                if " " in (a, b):
                    continue
                if (gap > 1.75 * pitch and a.islower() and b.isupper()) or (gap > 2.8 * pitch and a.isalnum() and b.isalnum()):
                    mid = (centres[i] + centres[i - 1]) / 2
                    chars.insert(i, [" ", 0, 0, 1.0])
                    centres.insert(i, mid)
        spans = []
        for i, (c, _, _, p) in enumerate(chars):
            left = (centres[i - 1] + centres[i]) / 2 if i else centres[i] - pitch / 2
            right = (centres[i] + centres[i + 1]) / 2 if i + 1 < len(chars) else centres[i] + pitch / 2
            spans.append((c, max(0.0, left), min(1.0, right), p))
        return spans

    # ---------- page ----------
    def orientation(self, bgr, boxes, sample=12):
        """Quarter turns (counter-clockwise) that make the page upright.

        Reads the largest text boxes as they would look under each rotation and
        keeps the clearly most confident reading. Upright text reads with high
        confidence; sideways or upside-down text does not.
        """
        if not boxes:
            return 0
        biggest = sorted(boxes, key=lambda q: -cv2.contourArea(q.astype(np.float32)))[:sample]
        raws = [warp(bgr, q, auto_rotate=False)[0] for q in biggest]
        scores = []
        for k in range(4):
            if k == 1 and scores[0] >= 0.93:  # clearly upright already: skip the other turns
                return 0
            crops = [np.rot90(r, k).copy() for r in raws]
            crops = [c for c in crops if c.shape[1] >= 1.2 * c.shape[0]]  # horizontal lines only
            if len(crops) < max(2, len(raws) // 4):
                scores.append(0.0)
                continue
            read = self.recognise_crops(crops)
            confs = [sum(s[3] for s in r) / len(r) * min(1.0, len(r) / 3) for r in read if r]
            scores.append(float(np.mean(confs)) if confs else 0.0)
        best = int(np.argmax(scores))
        return best if best != 0 and scores[best] > scores[0] + 0.08 else 0

    def recognise_crops(self, crops, batch=8, height=48):
        boxes_free = [(c, False) for c in crops]
        return self._recognise(boxes_free, batch, height)

    def read(self, bgr, min_conf=0.5):
        """Return (quarter_turns, lines) where lines are (quad, spans) on the upright image."""
        boxes = self.detect(bgr)
        turns = self.orientation(bgr, boxes)
        if turns:
            bgr = np.rot90(bgr, turns).copy()
            boxes = self.detect(bgr)
        lines = []
        for quad, spans in zip(boxes, self.recognise(bgr, boxes)):
            if not spans:
                continue
            if sum(s[3] for s in spans) / len(spans) < min_conf:
                continue
            lines.append((quad, spans))
        return turns, lines

def order(pts):
    pts = np.asarray(pts, np.float32)
    s, diff = pts.sum(1), np.diff(pts, axis=1).ravel()
    tl, br = pts[np.argmin(s)], pts[np.argmax(s)]
    rest = [p for i, p in enumerate(pts) if i not in (np.argmin(s), np.argmax(s))]
    if len(rest) != 2:
        return pts
    tr, bl = (rest[0], rest[1]) if rest[0][0] - rest[0][1] > rest[1][0] - rest[1][1] else (rest[1], rest[0])
    return np.array([tl, tr, br, bl], np.float32)

def warp(bgr, quad, auto_rotate=True):
    w = int(max(np.linalg.norm(quad[0] - quad[1]), np.linalg.norm(quad[3] - quad[2])))
    h = int(max(np.linalg.norm(quad[0] - quad[3]), np.linalg.norm(quad[1] - quad[2])))
    w, h = max(w, 2), max(h, 2)
    m = cv2.getPerspectiveTransform(quad, np.float32([[0, 0], [w, 0], [w, h], [0, h]]))
    crop = cv2.warpPerspective(bgr, m, (w, h), borderMode=cv2.BORDER_REPLICATE, flags=cv2.INTER_CUBIC)
    vertical = auto_rotate and h >= 1.5 * w
    if vertical:
        crop = np.rot90(crop).copy()  # read top-to-bottom columns left-to-right
    return crop, vertical

def words_from_lines(lines, sx, sy):
    """Split lines into words (Latin by spaces, CJK per character) with boxes in page units."""
    words = []
    for line_no, (quad, spans) in enumerate(lines):
        vertical = np.linalg.norm(quad[0] - quad[3]) >= 1.5 * np.linalg.norm(quad[0] - quad[1])
        text = "".join(s[0] for s in spans)
        pos = 0
        for word_no, m in enumerate(_TOKEN.finditer(text)):
            part = spans[m.start():m.end()]
            pos = m.end()
            u0, u1 = part[0][1], part[-1][2]
            if vertical:  # rotated crop: reading direction runs down the left edge
                a, b = quad[0] + (quad[3] - quad[0]) * u0, quad[0] + (quad[3] - quad[0]) * u1
                c, d = quad[1] + (quad[2] - quad[1]) * u0, quad[1] + (quad[2] - quad[1]) * u1
            else:
                a, b = quad[0] + (quad[1] - quad[0]) * u0, quad[0] + (quad[1] - quad[0]) * u1
                c, d = quad[3] + (quad[2] - quad[3]) * u0, quad[3] + (quad[2] - quad[3]) * u1
            pts = np.array([a, b, c, d])
            x0, y0 = pts.min(0)
            x1, y1 = pts.max(0)
            word = (float(x0 * sx), float(y0 * sy), float(x1 * sx), float(y1 * sy), m.group(0), line_no, 0, word_no)
            if not vertical and u1 > u0:
                # Each character's span as a fraction of the word's width, left to right.
                word += ([[round((c[1] - u0) / (u1 - u0), 4), round((c[2] - u0) / (u1 - u0), 4)] for c in part],)
            words.append(word)
        del pos
    return words

def image_words(rgb, model_dir):
    """OCR an RGB array; returns words in its pixel coordinates."""
    bgr = cv2.cvtColor(np.ascontiguousarray(rgb), cv2.COLOR_RGB2BGR)
    turns, lines = Reader.get(model_dir).read(bgr)
    H, W = bgr.shape[:2]
    words = words_from_lines(lines, 1.0, 1.0)  # pixel coordinates on the upright image
    if turns:  # character spans run along x only on upright images
        return [(*to_original(w[:4], turns, W, H), *w[4:8]) for w in words]
    return words

def model_identity(model_dir):
    """Hashes that tie cached OCR results to the exact models used."""
    import hashlib
    return {name: hashlib.sha256((Path(model_dir) / name).read_bytes()).hexdigest() for name in MODEL_FILES}

def page_words(page, model_dir, dpi=200):
    """OCR one PyMuPDF page; coordinates match page.get_text('words')."""
    import pymupdf as fitz
    pix = page.get_pixmap(dpi=dpi, alpha=False, colorspace=fitz.csRGB)
    rgb = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, 3)
    words = image_words(rgb, model_dir)
    # Pixmaps are rendered in displayed orientation; scale to page units, then unrotate.
    shown = page.rect
    sx, sy = shown.width / pix.width, shown.height / pix.height
    words = [(w[0] * sx, w[1] * sy, w[2] * sx, w[3] * sy, *w[4:]) for w in words]
    if page.rotation:
        inv = page.derotation_matrix
        out = []
        for w in words:
            r = fitz.Rect(w[:4]) * inv
            out.append((r.x0, r.y0, r.x1, r.y1, *w[4:8]))  # spans no longer run along x
        words = out
    return words

def to_original(box, turns, W, H):
    """Map a box on np.rot90(image, turns) back to the original W x H image.

    One counter-clockwise np.rot90 sends original (x, y) to (y, W_prev - x), so each
    turn is undone with (u, v) -> (W_prev - v, u), where W_prev is the width before it.
    """
    x0, y0, x1, y1 = box
    pts = [(x0, y0), (x1, y0), (x0, y1), (x1, y1)]
    for j in range(turns % 4, 0, -1):
        width_before = W if (j - 1) % 2 == 0 else H
        pts = [(width_before - v, u) for u, v in pts]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)
