"""Measure how completely private details are covered on the synthetic scans.

python benchmarks/ocr/evaluate.py <set dir> [--engine ppocr|tesseract] [--limit N]

Reports, per target kind and language: the share of targets whose ground-truth box
is at least 95% covered by removals, targets less than half covered (missed),
false alarms, seconds per page, and native OCR console warnings.
"""
import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from pdfscan import engine  # noqa: E402

GRID = (566, 400)

class Capture:
    """Capture C-level stdout/stderr, where native OCR libraries print."""
    def __enter__(self):
        self.tmp = tempfile.TemporaryFile()
        sys.stdout.flush(); sys.stderr.flush()
        self.saved = [os.dup(1), os.dup(2)]
        os.dup2(self.tmp.fileno(), 1); os.dup2(self.tmp.fileno(), 2)
        return self
    def __exit__(self, *exc):
        sys.stdout.flush(); sys.stderr.flush()
        os.dup2(self.saved[0], 1); os.dup2(self.saved[1], 2)
        for fd in self.saved:
            os.close(fd)
        self.tmp.seek(0)
        self.text = self.tmp.read().decode("utf-8", "replace")

def mask(rects):
    m = np.zeros(GRID, bool)
    for r in rects:
        m[int(r[1] * GRID[0]):int(np.ceil(r[3] * GRID[0])), int(r[0] * GRID[1]):int(np.ceil(r[2] * GRID[1]))] = True
    return m

def run(setdir, engine_name, limit=None):
    truth = json.loads((Path(setdir) / "truth.json").read_text("utf-8"))[:limit]
    models = ROOT / ".runtime" / "models" / "ppocr" if engine_name == "ppocr" else None
    tessdata = ROOT / ".runtime" / "tessdata"
    rows, warnings, seconds, false_pos = [], 0, 0.0, 0
    for doc in truth:
        texts = sorted({t["text"] for t in doc["targets"] if t["kind"] in ("name", "org")})
        config = {"text": texts, "phone": True, "email": True, "ids": True, "cards": True, "ocr": True}
        start = time.perf_counter()
        with Capture() as cap:
            plan = engine.detect(Path(setdir) / doc["file"], config, tessdata=tessdata, models=models)
        seconds += time.perf_counter() - start
        warnings += cap.text.count("too small to scale") + cap.text.count("cannot be recognized")
        dets = [d["rect"] for p in plan["pages"] for d in p["detections"]]
        covered, truth_mask = mask(dets), mask([t["box"] for t in doc["targets"]])
        false_pos += sum(1 for r in dets if (mask([r]) & truth_mask).sum() < .1 * max(1, mask([r]).sum()))
        for t in doc["targets"]:
            g = mask([t["box"]])
            rows.append({"kind": t["kind"], "lang": doc["lang"], "coverage": float((g & covered).sum() / max(1, g.sum()))})
    cov = np.array([r["coverage"] for r in rows])
    out = {"set": Path(setdir).name, "engine": engine_name, "pages": len(truth), "targets": len(rows),
           "covered@95": round(float((cov >= .95).mean()), 3), "missed(<50%)": int((cov < .5).sum()),
           "false_alarms": false_pos, "sec/page": round(seconds / max(1, len(truth)), 2), "ocr_warnings": warnings}
    for key in ("kind", "lang"):
        for value in sorted({r[key] for r in rows}):
            sel = np.array([r[key] == value for r in rows])
            out[f"{key}:{value}"] = round(float((cov[sel] >= .95).mean()), 3)
    return out

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("set")
    parser.add_argument("--engine", choices=("ppocr", "tesseract"), default="ppocr")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    print(json.dumps(run(args.set, args.engine, args.limit), ensure_ascii=False))
