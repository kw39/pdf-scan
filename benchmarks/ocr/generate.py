"""Synthetic scanned-document benchmark with exact ground truth.

Pages imitate what PDF Scan users share: exam papers, forms and letters in
English and Traditional Chinese, with tables, answer boxes, ruled lines and
margin rules, degraded like real scans. Every private detail is recorded with
its bounding box so detection recall can be measured exactly.

python benchmarks/ocr/generate.py <outdir> <seed> <count> [--stress]

Fonts: Liberation, DejaVu, FreeFont and Noto CJK (Debian/Ubuntu: fonts-liberation
fonts-dejavu fonts-freefont-ttf fonts-noto-cjk). All names and numbers are fictional.
"""
import io
import json
import math
import random
import sys
from pathlib import Path

import numpy as np
import pymupdf as fitz
from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H = 2480, 3508  # A4 at 300 dpi
LATIN = ["/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
         "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
         "/usr/share/fonts/truetype/freefont/FreeSerif.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf"]
LATIN_BOLD = ["/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf",
              "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
CJK = [("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", 3),
       ("/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc", 3),
       ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", 4)]
CJK_BOLD = [("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", 3),
            ("/usr/share/fonts/opentype/noto/NotoSerifCJK-Bold.ttc", 3)]

EN_NAMES = ["Chan Tai Man", "Wong Siu Ming", "Emily Lau", "Daniel Ho Ka Lok", "Cheung Wing Yan", "Michael Tsang",
            "Lee Hoi Ting", "Kwok Chun Hei", "Sarah Fung", "Leung Pak Kin"]
ZH_NAMES = ["陳大文", "黃小明", "李嘉欣", "張志強", "林美玲", "何家樂", "鄭詠欣", "周子健", "吳雅婷", "梁柏堅"]
EN_ORGS = ["St. Clement's College", "Lok Fu Secondary School", "Harbour Lane Trading Ltd", "Kowloon True Light School",
           "Northpoint Methodist College", "Silvermine Logistics Ltd"]
ZH_ORGS = ["聖基道書院", "樂富中學", "海港貿易有限公司", "真光女書院", "北角循道學校", "銀礦物流有限公司"]
EN_FILL = ["Answer all questions in the spaces provided.", "Show your working clearly and give exact values.",
           "The diagram below is not drawn to scale.", "Calculate the area of the shaded region.",
           "Explain why the experiment must be repeated three times.", "Write down the coordinates of the turning point.",
           "Please return this form to the general office by Friday.", "Thank you for your continued support.",
           "The meeting will be held in the school hall after lunch.", "Parents are reminded to sign the reply slip below."]
ZH_FILL = ["回答所有問題，答案須寫在指定位置。", "必須清楚列出計算步驟。", "下圖並非按比例繪畫。", "求陰影部分的面積。",
           "試解釋為何實驗須重複三次。", "請於星期五前把回條交回校務處。", "家長會將於午膳後在禮堂舉行。", "多謝各位家長一直以來的支持。"]

def hkid(rng):
    letter = rng.choice("ACDEHKMPRVYZ")
    digits = "".join(str(rng.randint(0, 9)) for _ in range(6))
    total = 9 * 36 + 8 * (ord(letter) - 55) + sum((7 - i) * int(d) for i, d in enumerate(digits))
    check = (11 - total % 11) % 11
    return f"{letter}{digits}({'A' if check == 10 else check})"

def luhn(rng):
    body = [4] + [rng.randint(0, 9) for _ in range(14)]
    total = 0
    for i, d in enumerate(reversed(body)):
        total += (d * 2 - 9 if d * 2 > 9 else d * 2) if i % 2 == 0 else d
    body.append((10 - total % 10) % 10)
    s = "".join(map(str, body))
    return " ".join(s[i:i + 4] for i in range(0, 16, 4))

def phone(rng):
    n = f"{rng.choice('235679')}{rng.randint(100, 999)} {rng.randint(1000, 9999)}"
    return rng.choice([f"+852 {n}", n, f"(852) {n}"])

STRESS = {"on": False}

class Page:
    def __init__(self, rng, lang):
        self.rng, self.lang = rng, lang
        self.im = Image.new("L", (W, H), 255)
        self.d = ImageDraw.Draw(self.im)
        self.targets = []  # {"kind","text","box":[x0,y0,x1,y1] px}
        self.latin = rng.choice(LATIN)
        self.cjk = rng.choice(CJK)

    def font(self, size, cjk=False, bold=False):
        if cjk:
            path, idx = self.rng.choice(CJK_BOLD) if bold else self.cjk
            return ImageFont.truetype(path, size, index=idx)
        return ImageFont.truetype(self.rng.choice(LATIN_BOLD) if bold else self.latin, size)

    def text(self, x, y, s, size, kind=None, bold=False, cjk=None):
        cjk = any("㐀" <= c <= "鿿" for c in s) if cjk is None else cjk
        f = self.font(size, cjk, bold)
        self.d.text((x, y), s, font=f, fill=self.rng.randint(0, 40))
        box = list(self.d.textbbox((x, y), s, font=f))
        if kind:
            self.targets.append({"kind": kind, "text": s, "box": box})
        return box

    def run(self, x, y, parts, size):
        """Draw a line made of (text, kind) parts; returns the right edge."""
        for s, kind in parts:
            b = self.text(x, y, s, size, kind)
            x = b[2] + int(size * (0.3 if self.lang == "en" else 0.15))
        return x

    def rule(self, x0, y0, x1, y1, w=3):
        self.d.line((x0, y0, x1, y1), fill=self.rng.randint(0, 60), width=w)

    def rect(self, box, w=3):
        self.d.rectangle(box, outline=self.rng.randint(0, 60), width=w)

    def crest(self, x, y, s):
        self.d.ellipse((x, y, x + s, y + s), outline=20, width=6)
        self.d.polygon([(x + s * .3, y + s * .25), (x + s * .7, y + s * .25), (x + s * .7, y + s * .55),
                        (x + s * .5, y + s * .78), (x + s * .3, y + s * .55)], outline=20, fill=self.rng.choice([255, 120]))

def exam(page):
    rng, zh = page.rng, page.lang == "zh"
    org = rng.choice(ZH_ORGS[:2] + ZH_ORGS[3:5] if zh else EN_ORGS[:2] + EN_ORGS[3:5])
    name = rng.choice(ZH_NAMES if zh else EN_NAMES)
    page.crest(160, 140, 230)
    page.text(440, 160, org, rng.randint(64, 84), "org", bold=True)
    page.text(440, 290, "2025-26 年度模擬考試 數學 試卷一" if zh else "2025-26 Mock Examination  Mathematics Paper 1", rng.randint(40, 50))
    # Candidate table with ruled grid: the classic source of thin vertical slivers.
    top = 470
    cols = [160, 560, 1380, 1700, 2320]
    for x in cols:
        page.rule(x, top, x, top + 200, rng.choice([2, 3, 4]))
    for yy in (top, top + 100, top + 200):
        page.rule(cols[0], yy, cols[-1], yy, rng.choice([2, 3]))
    page.text(190, top + 22, "姓名" if zh else "Name", 46)
    page.text(590, top + 18, name, 54, "name")
    page.text(1410, top + 22, "班別" if zh else "Class", 46)
    page.text(1730, top + 18, f"{rng.randint(1, 6)}{rng.choice('ABCDE')}", 50)
    page.text(190, top + 122, "學號" if zh else "Student No.", 40)
    sid = f"S{rng.randint(1000000, 9999999)}"
    page.text(590, top + 118, sid, 50, "name" if False else None)
    # Answer boxes (tiny squares) for candidate number, each a separate box.
    bx = 1410
    for i in range(8):
        page.rect((bx + i * 112, top + 112, bx + i * 112 + 100, top + 190), rng.choice([2, 3]))
    # Margin rule down the left side, as on answer books.
    page.rule(110, 760, 110, H - 260, rng.choice([2, 3]))
    y = 790
    for q in range(rng.randint(3, 5)):
        page.text(160, y, f"{q + 1}.  " + rng.choice(ZH_FILL if zh else EN_FILL), rng.randint(42, 52))
        y += 90
        if rng.random() < .5:
            page.text(220, y, rng.choice(ZH_FILL if zh else EN_FILL), rng.randint(40, 48))
            y += 80
        for _ in range(rng.randint(2, 4)):  # answer lines
            page.rule(220, y + 60, 2300, y + 60, 2)
            y += 85
        if rng.random() < .4:  # boxed answer with a vertical divider
            page.rect((1700, y, 2300, y + 140), 3)
            page.rule(1900, y, 1900, y + 140, 2)
            page.text(1720, y + 40, "答案" if zh else "Ans", 40)
            y += 170
        y += 60
        if y > H - 700:
            break
    if rng.random() < .7:
        tel = phone(rng)
        page.run(160, H - 360, [("查詢電話：" if zh else "Enquiries:", None), (tel, "phone")], 40)
    page.text(160, H - 250, org, rng.randint(28, 34), "org")  # small footer copy
    page.text(2200, H - 250, f"P. {rng.randint(1, 12)}", 32)

def form(page):
    rng, zh = page.rng, page.lang == "zh"
    org = rng.choice([ZH_ORGS[2], ZH_ORGS[5]] if zh else [EN_ORGS[2], EN_ORGS[5]])
    page.text(200, 180, org, rng.randint(66, 86), "org", bold=True)
    tel, mail = phone(rng), f"{rng.choice(['info', 'hr', 'admin', 'office'])}@{rng.choice(['harbourlane', 'silvermine', 'example'])}.com.hk"
    x = page.run(200, 300, [("電話" if zh else "Tel", None), (tel, "phone")], 38)
    page.run(x + 40, 300, [("電郵" if zh else "Email", None), (mail, "email")], 38)
    page.rule(200, 380, 2280, 380, 4)
    page.text(200, 440, "員工資料表" if zh else "Staff Information Form", 60, bold=True)
    rows = [("姓名" if zh else "Name", rng.choice(ZH_NAMES if zh else EN_NAMES), "name"),
            ("身分證號碼" if zh else "HKID No.", hkid(rng), "id"),
            ("聯絡電話" if zh else "Contact phone", phone(rng), "phone"),
            ("信用卡號碼" if zh else "Card number", luhn(rng), "card"),
            ("職位" if zh else "Position", "文員" if zh else "Clerk", None)]
    top, left, mid, right = 600, 200, 900, 2280
    for i, (label, value, kind) in enumerate(rows):
        yy = top + i * 130
        page.rule(left, yy, right, yy, rng.choice([2, 3]))
        page.text(left + 30, yy + 35, label, 44)
        page.text(mid + 30, yy + 30, value, 50, kind)
    page.rule(left, top + len(rows) * 130, right, top + len(rows) * 130, 3)
    for xx in (left, mid, right):
        page.rule(xx, top, xx, top + len(rows) * 130, rng.choice([2, 3]))
    y = top + len(rows) * 130 + 120
    for _ in range(rng.randint(4, 7)):
        page.text(200, y, rng.choice(ZH_FILL if zh else EN_FILL), rng.randint(40, 48))
        y += 85
    who = rng.choice(ZH_NAMES if zh else EN_NAMES)
    page.run(200, y + 60, [("簽署：" if zh else "Signed by", None), (who, "name")], 44)
    page.rule(200, y + 260, 900, y + 260, 3)
    if rng.random() < .5:  # QR-like block of small squares (tiny connected components)
        for i in range(12):
            for j in range(12):
                if rng.random() < .5:
                    page.d.rectangle((1900 + i * 22, y + 100 + j * 22, 1919 + i * 22, y + 119 + j * 22), fill=0)

def degrade(page, rng):
    im = page.im
    angle = rng.choice([0, 0, rng.uniform(-1.6, 1.6)])
    bg = rng.randint(222, 250)
    if angle:
        im = im.rotate(angle, resample=Image.BICUBIC, expand=False, fillcolor=255)
    arr = np.asarray(im, dtype=np.float32)
    arr = bg - (255 - arr) * (bg / 255) * rng.uniform(.75, 1.0)  # grey paper, faded ink
    yy = np.linspace(0, 1, H)[:, None]
    arr += rng.uniform(-10, 10) * yy  # uneven lighting
    arr += np.random.default_rng(rng.randint(0, 1 << 30)).normal(0, rng.uniform(0, 10), arr.shape)
    if rng.random() < .4:  # specks
        n = rng.randint(200, 1500)
        g = np.random.default_rng(rng.randint(0, 1 << 30))
        ys, xs = g.integers(0, H, n), g.integers(0, W, n)
        arr[ys, xs] = rng.randint(20, 90)
    im = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
    blur = rng.choice([0, 0, .6, 1.0, 1.4]) if not STRESS["on"] else rng.choice([1.0, 1.6, 2.2])
    if STRESS["on"]:
        arr2 = np.asarray(im, dtype=np.float32)
        im = Image.fromarray(np.clip(255 - (255 - arr2) * rng.uniform(.35, .6), 0, 255).astype(np.uint8))  # faint ink
    if blur:
        im = im.filter(ImageFilter.GaussianBlur(blur))
    dpi = rng.choice([150, 200, 200, 300])
    im = im.resize((round(W * dpi / 300), round(H * dpi / 300)), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=rng.randint(35, 88) if not STRESS["on"] else rng.randint(18, 40))
    # Ground truth after rotation, normalised to 0..1.
    cx, cy, a = W / 2, H / 2, math.radians(-angle)
    for t in page.targets:
        x0, y0, x1, y1 = t["box"]
        pts = [(x0, y0), (x1, y0), (x0, y1), (x1, y1)]
        rot = [(cx + (x - cx) * math.cos(a) - (y - cy) * math.sin(a), cy + (x - cx) * math.sin(a) + (y - cy) * math.cos(a)) for x, y in pts]
        xs, ys = [p[0] for p in rot], [p[1] for p in rot]
        t["box"] = [min(xs) / W, min(ys) / H, max(xs) / W, max(ys) / H]
    return buf.getvalue(), {"angle": round(angle, 2), "dpi": dpi, "blur": blur, "bg": bg}

def build(outdir, seed, count):
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    manifest = []
    for n in range(count):
        page = Page(rng, rng.choice(["en", "zh"]))
        (exam if rng.random() < .55 else form)(page)
        data, meta = degrade(page, rng)
        path = out / f"doc{n:03d}.pdf"
        turn = rng.choice([0, 90, 180, 270]) if STRESS["on"] else 0
        if turn:  # the scanner fed the sheet sideways: image is rotated, page shows it rotated
            img = Image.open(io.BytesIO(data)).rotate(turn, expand=True)
            b2 = io.BytesIO(); img.save(b2, "JPEG", quality=85); data = b2.getvalue()
            for t in page.targets:
                x0, y0, x1, y1 = t["box"]
                if turn == 90: t["box"] = [y0, 1 - x1, y1, 1 - x0]
                elif turn == 180: t["box"] = [1 - x1, 1 - y1, 1 - x0, 1 - y0]
                else: t["box"] = [1 - y1, x0, 1 - y0, x1]
            meta["turn"] = turn
        with fitz.open() as doc:
            w, h = (842, 595) if turn in (90, 270) else (595, 842)
            p = doc.new_page(width=w, height=h)
            p.insert_image(p.rect, stream=data)
            doc.save(path)
        manifest.append({"file": path.name, "lang": page.lang, **meta, "targets": page.targets})
    (out / "truth.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), "utf-8")
    return manifest

if __name__ == "__main__":
    STRESS["on"] = "--stress" in sys.argv
    m = build(sys.argv[1], int(sys.argv[2]), int(sys.argv[3]))
    print(len(m), "pages,", sum(len(x["targets"]) for x in m), "targets")
