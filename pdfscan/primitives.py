import re, unicodedata, io, os
import pymupdf as fitz
from PIL import Image, ImageDraw, ImageFilter, ImageOps

RENDER_SIDE = 1800
MAX_SIDE = 14400

def page_scale(page):
    return min(1.0, RENDER_SIDE / max(page.rect.width, page.rect.height))

def key(s):
    return re.sub('[^\\w\\u3400-\\u9fff]', '', unicodedata.normalize('NFKC', s).casefold())
EXAM_LABEL = re.compile('(?:19|20)\\d\\d|exam|mock|test|paper|math|maths|term|section|\\bform\\b|\\bF\\.?\\s?[1-6]\\b|\\bS[1-6]\\b|\\bM[12]\\b|\\bEP\\b|考試|測驗|試卷|數學|中文|英文|上學期|下學期|模擬', re.I)
FORM_FIELD = re.compile('barcode|candidatenumber|candidateno|cent(?:re|er)number|seatnumber|stickyour|labelhere|考生編號|試場編號|座位編號|條碼')
FORM_WORDS = {'please', 'stick', 'affix', 'attach', 'place', 'put', 'your', 'the', 'barcode', 'bar', 'code', 'label', 'labels', 'sticker', 'here', 'this', 'box', 'space', 'candidate', 'candidates', 'number', 'centre', 'center', 'seat', 'class', 'name'}
FORM_CJK = set('請在此處貼上於電腦條碼標籤考生試場座位編號姓名班別')

def image_text(bbox, words):
    return ' '.join((w[4] for w in words if bbox.contains(fitz.Point((w[0] + w[2]) / 2, (w[1] + w[3]) / 2))))

def image_note(text):
    shown = re.sub('\\s+', ' ', text).strip()
    return f" (text read inside image: '{shown[:60]}{('…' if len(shown) > 60 else '')}')" if shown else ' (no readable text inside image)'

def names_school(readings, aliases):
    compact = key(' '.join(readings))
    return any((len(key(a)) >= 3 and key(a) in compact for a in aliases))

def exam_label_image(bbox, words, aliases, extra=()):
    readings = [image_text(bbox, words), *extra]
    if names_school(readings, aliases):
        return False
    return any((len(key(t)) >= 6 and len(EXAM_LABEL.findall(t)) >= 2 for t in readings))

def form_residue(text):
    left = []
    for token in re.findall('[A-Za-z]+|[\\u3400-\\u9fff]+', unicodedata.normalize('NFKC', text)):
        if token.isascii():
            t = token.casefold()
            if t not in FORM_WORDS and len(t) >= 4 and re.search('[aeiouy]', t) and (not re.fullmatch('[iltj]+', t)):
                left.append(token)
        elif len([c for c in token if c not in FORM_CJK]) >= 2:
            left.append(token)
    return left

def form_field_image(box, readings, aliases, colour=0.0):
    if box.height <= 0 or box.width / box.height < 1.8 or colour > 0.03:
        return ''
    if names_school(readings, aliases):
        return ''
    return next((t for t in readings if FORM_FIELD.search(key(t)) and (not form_residue(t))), '')

def strip_rules(gray, run):
    bw = gray.point(lambda v: 0 if v < 150 else 255)
    src = bw.load()
    out = bw.copy()
    dst = out.load()
    w, h = bw.size
    for outer, inner, at in ((h, w, lambda a, b: (b, a)), (w, h, lambda a, b: (a, b))):
        for a in range(outer):
            b = 0
            while b < inner:
                if src[at(a, b)]:
                    b += 1
                    continue
                start = b
                while b < inner and (not src[at(a, b)]):
                    b += 1
                if b - start >= run:
                    for k in range(start, b):
                        dst[at(a, k)] = 255
    return out

def close_read(page, bbox):
    try:
        clip = fitz.Rect(bbox) * page.rotation_matrix & page.rect
        if clip.is_empty:
            return ([], 1.0)
        readings, colour = ([], 1.0)
        for dpi, lang, strip in ((300, 'eng+chi_tra', False), (300, 'eng+chi_tra', True), (400, 'eng+chi_tra', True), (400, 'chi_tra', True)):
            dpi = min(dpi, int(2000 * 72 / max(clip.width, clip.height)))
            pix = page.get_pixmap(dpi=dpi, clip=clip, alpha=False)
            rgb = Image.open(io.BytesIO(pix.tobytes('png'))).convert('RGB')
            if not strip:
                spread = rgb.convert('HSV').getchannel('S').point(lambda v: 255 if v > 90 else 0)
                colour = spread.histogram()[255] / (rgb.width * rgb.height)
            im = strip_rules(rgb.convert('L'), max(20, dpi // 4)) if strip else rgb.convert('L')
            stream = io.BytesIO()
            ImageOps.expand(im, 30, 255).convert('RGB').save(stream, format='PNG')
            ocr = fitz.open('pdf', fitz.Pixmap(stream.getvalue()).pdfocr_tobytes(language=lang, tessdata=os.environ.get('TESSDATA_PREFIX')))
            readings.append(' '.join(ocr[0].get_text().split()))
        return (list(dict.fromkeys((t for t in readings if t))), colour)
    except Exception:
        return ([], 1.0)

def snap_to_ink(gray, rect, parts, pad=3):
    W, H = gray.size
    x0, x1 = (max(0, int(rect[0] * W)), min(W, int(rect[2] * W + 0.999)))
    y0, y1 = (max(0, int(rect[1] * H)), min(H, int(rect[3] * H + 0.999)))
    line = max((int((q[3] - q[1]) * H) for q in parts)) if parts else 0
    if x1 - x0 < 4 or y1 - y0 < 2 or line < 2:
        return rect
    top, bottom = (max(0, y0 - line), min(H, y1 + line))
    ink = gray.crop((x0, top, x1, bottom)).point(lambda v: 255 if v < 200 else 0)
    band = ink.resize((1, bottom - top), Image.BOX)
    rows = list(getattr(band, "get_flattened_data", band.getdata)())
    has_ink = [v * (x1 - x0) / 255 >= 2 for v in rows]
    cores = [(int((q[1] + (q[3] - q[1]) * 0.25) * H) - top, int((q[3] - (q[3] - q[1]) * 0.25) * H) - top) for q in parts]
    kept, row = ([], 0)
    while row < len(has_ink):
        if not has_ink[row]:
            row += 1
            continue
        start = row
        while row < len(has_ink) and has_ink[row]:
            row += 1
        if any((start <= c1 and row > c0 for c0, c1 in cores)):
            kept.append((start + top, row + top))
    if not kept:
        return rect
    reach = 0.35 * line
    ink_top, ink_bottom = (min((k[0] for k in kept)), max((k[1] for k in kept)))
    new_top = ink_top - pad if ink_top >= y0 - reach else y0
    new_bottom = ink_bottom + pad if ink_bottom <= y1 + reach else y1
    return [rect[0], max(0, new_top / H), rect[2], min(1, new_bottom / H)]

def rule_segments(gray, box, reach=20, thick=6, dark=200):
    W, H = gray.size
    x0, y0, x1, y1 = box
    px = gray.load()
    out = []

    def extent(x, y, dx, dy):
        n = 0
        while 0 <= x < W and 0 <= y < H and (px[x, y] < dark) and (n < reach):
            n += 1
            x += dx
            y += dy
        return n

    def thin(x, y, dx, dy):
        return extent(x, y, dx, dy) + extent(x - dx, y - dy, -dx, -dy) - 1 <= thick
    for y in range(y0, y1):
        if x0 > 0 and extent(x0 - 1, y, -1, 0) >= reach and thin(x0 - reach // 2, y, 0, 1):
            x = x0
            while x < x1 and px[x, y] < dark:
                x += 1
            if x > x0:
                out.append((x0, y, x, y + 1))
        if x1 < W and extent(x1, y, 1, 0) >= reach and thin(x1 + reach // 2, y, 0, 1):
            x = x1 - 1
            while x >= x0 and px[x, y] < dark:
                x -= 1
            if x < x1 - 1:
                out.append((x + 1, y, x1, y + 1))
    for x in range(x0, x1):
        if y0 > 0 and extent(x, y0 - 1, 0, -1) >= reach and thin(x, y0 - reach // 2, 1, 0):
            y = y0
            while y < y1 and px[x, y] < dark:
                y += 1
            if y > y0:
                out.append((x, y0, x + 1, y))
        if y1 < H and extent(x, y1, 0, 1) >= reach and thin(x, y1 + reach // 2, 1, 0):
            y = y1 - 1
            while y >= y0 and px[x, y] < dark:
                y -= 1
            if y < y1 - 1:
                out.append((x, y + 1, x + 1, y1))
    return out

def readable_boxes(words, page):
    heights = sorted((w[3] - w[1] for w in words if w[3] > w[1]))
    if not heights:
        return []
    limit = 1.5 * heights[len(heights) // 2]
    out = []
    for w in words:
        if w[3] - w[1] > limit or not re.search('[A-Za-z]{2}|\\d|[\\u3400-\\u9fff]', w[4]):
            continue
        d = fitz.Rect(w[:4]) * page.rotation_matrix
        out.append((d.x0 / page.rect.width, d.y0 / page.rect.height, d.x1 / page.rect.width, d.y1 / page.rect.height, w[4]))
    return out

def text_share(points, width, height, boxes, slack=3):
    px = [(b[0] * width - slack, b[1] * height - slack, b[2] * width + slack, b[3] * height + slack, b[4]) for b in boxes]
    hit, used = (0, {})
    for a, b in points:
        for q in px:
            if q[0] <= a <= q[2] and q[1] <= b <= q[3]:
                hit += 1
                used[q[:4]] = q[4]
                break
    return (hit / len(points) if points else 0, ' '.join(used.values()))
INSTRUCTION_CUES = {'instructions': '\\binstructions?\\b|考生須知|注意事項', 'attempt all': '\\battempt all\\b|\\banswer all\\b|全部試題|回答所有|作答所有|均須作答|全部作答', 'write your answers': '\\bwrite (?:all )?your answers?\\b|答案寫在|寫在.{0,6}(?:答題|空位)', 'answer book': 'question\\s*[-–—]*\\s*answer book|\\banswer book(?:let)?\\b|答題簿|題目簿', 'time allowed': '\\btime allowed\\b|\\b\\d+\\s*hours?(?: and \\d+\\s*minutes)?\\b|考試時間|時限|小時.{0,6}分鐘|分鐘完卷|小時完卷', 'supplementary sheets': 'supplementary answer sheets?|補充答題紙', 'not drawn to scale': 'not necessarily drawn to scale|不一定依照比例|並非按比例', 'numerical answers': 'numerical answers? (?:should|must)|數值答案', 'working shown': 'working must be (?:clearly )?shown|須顯示.{0,4}算式|列出.{0,4}步驟|算式須.{0,4}列出', 'paper consists of': 'this paper consists of|this paper must be answered|本試卷(?:共|包括|分)', 'margins': 'do not write in the margins|answers written in the margins|邊界以外', "marker's use": "marker'?s'? use only|for markers?'? use|閱卷員專用|只供閱卷", 'candidate details': 'candidate number|class number|考生編號|姓名.{0,6}班[別號]', 'do not turn over': 'do not turn (?:over|this page)|未得指示|不可翻'}
INSTRUCTION_WORDS = re.compile("\\b(?:answers?|answered|questions?|papers?|write|written|writing|marks?|marked|marker'?s?|candidates?|sections?|time|sheets?|book(?:let)?|pencil|exam(?:ination)?|mock|test|page|name|class|number|no|allowed|hours?|minutes?|instructions?|attempt|supplementary|diagrams?|scale|working|shown|specified|numerical|significant|figures|margins?|provided|spaces?|calculators?|approved|told|open|check|end|total|deduction|read|carefully|insert|information|required|advised|clearly|erased|receive|deducted|wrong|date|am|pm|school|college|form|mathematics|maths|compulsory|part|module|english|chinese|physics|chemistry|biology|economics|history|geography|subject|pages|printed|blank|request|students?|rubber|graph|ruler|pen|ink|correct|must|should|will|may)\\b|考生|試卷|作答|答案|時間|題目|須知|姓名|班別|分數|考試|模擬|試題|答題|算式|日期|總分|頁數|小時|分鐘|班號|評閱|邊界|補充|開考|須|請", re.I)
FORMULA_REFERENCE = re.compile('formula(?:e|s)?\\s+for\\s+reference|list of formulae|formula sheet|參考公式|公式表', re.I)
MATH_FUNCTIONS = re.compile('(?i)sin|cos|tan|cot|sec|csc|log|ln')
QUESTION_SIGNAL = re.compile("[(\\[]\\s*\\d+\\s*marks?\\s*[)\\]]|\\d+\\s*分\\s*[)）]|\\b(?:[A-Za-z]{2,}[,']?\\s+){3,}[A-Za-z]{2,}\\s*\\?|[\\u3400-\\u9fff]{2,}\\s*[？?]|\\bfind\\b(?!\\s+(?:it\\s+)?useful)|\\bwrite down\\b(?!\\s+(?:the subject|your|name|class))|\\bwhich (?:of|one|is|are|was|were|statement|option|graph|diagram|pair|set)\\b|\\b(?:prove|show that|solve|simplify|factori[sz]e|calculate|evaluate|expand|explain|describe|determine|sketch|express|estimate|compare|suggest|discuss|identify|what|why|how many|how much|answer:)\\b|(?:^|\\s)A\\.\\s.+?\\sB\\.\\s.+?\\sC\\.\\s.+?\\sD\\.\\s|(?<![要需請追尋訴徵祈懇])求|計算|證明|化簡|解釋|因式分解|下列|哪|答案[:：]", re.I | re.S)
MATH_SIGNAL = re.compile('[×÷∫√∑π≤≥±∓]|[A-Za-z0-9)]\\s*=\\s*[-+−(√A-Za-z0-9]')
MAC_GLYPHS = {177: '–', 178: '—', 179: '“', 180: '”', 181: '‘', 182: '’'}

def unshift(word):
    if len(word) < 4 or not re.search("[$&'*+;<=%]", word) or re.fullmatch('[\\d.,:;/%()\\-+±$]+', word):
        return None
    out = []
    for ch in word:
        code = ord(ch)
        if 3 <= code - 0 <= 93 and code >= 32:
            out.append(chr(code + 29))
        elif code in MAC_GLYPHS:
            out.append(MAC_GLYPHS[code])
        else:
            return None
    decoded = ''.join(out)
    letters = sum((c.isalpha() for c in decoded))
    before = sum((c.isalpha() for c in word))
    if letters >= 3 and letters >= 0.7 * len(decoded) and (before <= 0.3 * len(word)):
        return decoded
    return None

def unshift_words(words):
    out = [(*w[:4], unshift(w[4]), *w[5:]) if unshift(w[4]) else tuple(w) for w in words]
    done = [unshift(w[4]) is not None for w in words]
    changed = True
    while changed:
        changed = False
        for i, w in enumerate(words):
            if done[i]:
                continue
            near = [j for j in (i - 1, i + 1) if 0 <= j < len(words) and done[j] and (tuple(words[j][5:7]) == tuple(w[5:7]))]
            codes = [ord(c) for c in w[4]]
            if near and codes and all((32 <= c <= 93 or c in MAC_GLYPHS for c in codes)):
                decoded = ''.join((MAC_GLYPHS.get(c) or chr(c + 29) for c in codes))
                if all((ch.isalpha() or ch in "’'" for ch in decoded)):
                    out[i] = (*w[:4], decoded, *w[5:])
                    done[i] = changed = True
    return out

def decode_page_text(text):
    garbled = False

    def fix(match):
        nonlocal garbled
        decoded = unshift(match.group(0))
        if decoded:
            garbled = True
        return decoded or match.group(0)
    text = re.sub('\\S+', fix, text)
    return re.sub('(?<=\\s)±(?=\\s)', '–', text) if garbled else text

def question_content(flat, formulas=False):
    maths = MATH_SIGNAL.findall(flat)
    return bool(QUESTION_SIGNAL.search(flat) or (not formulas and (len(maths) >= 2 or any(('=' in m for m in maths)))) or (re.search('\\(a\\)', flat) and re.search('\\(b\\)', flat)))

def flatten(text):
    joined = re.sub('(?<=[\\u3400-\\u9fff])[ \\t]+(?=[\\u3400-\\u9fff])', '', unicodedata.normalize('NFKC', text))
    return (joined, re.sub('\\s+', ' ', joined))

def instruction_page(text, diagram=False):
    joined, flat = flatten(text)
    cues = [name for name, pattern in INSTRUCTION_CUES.items() if re.search(pattern, flat, re.I)]
    formulas = bool(FORMULA_REFERENCE.search(flat))
    if len(cues) < 3 or (diagram and (not formulas)):
        return (False, cues)
    if question_content(flat, formulas):
        return (False, cues)
    count = lambda t: len(re.findall('[A-Za-z]+|\\d+|[\\u3400-\\u9fff]', t))
    other = 0.0
    for part in re.split('[.!?。！？;；\\n]+', joined):
        if INSTRUCTION_WORDS.search(part) or any((re.search(pattern, part, re.I) for pattern in INSTRUCTION_CUES.values())):
            continue
        if formulas:
            part = MATH_FUNCTIONS.sub(' ', part)
        other += len(re.findall('[A-Za-z]{2,}', part)) + len(re.findall('[\\u3400-\\u9fff]', part)) / 2
    return (count(flat) <= 700 and other <= 80, cues)

def blank_image(page, xref, cache=None):
    if cache is None:
        cache = {}
    doc = page.parent
    key_ = (id(doc), xref)
    if not xref:
        return False
    if key_ not in cache:
        try:
            base = fitz.Pixmap(doc, xref)
            if min(base.width, base.height) <= 8:
                cache[key_] = True
                return True
            ref = doc.xref_get_key(xref, 'SMask')
            mask = fitz.Pixmap(doc, int(ref[1].split()[0])) if ref[0] == 'xref' else None
            invisible = mask is not None and mask.color_count() == 1 and (mask.samples[:1] == b'\x00')
            flat = base.color_count() == 1 and (mask is None or mask.color_count() == 1)
            cache[key_] = invisible or flat
        except Exception:
            cache[key_] = False
    return cache[key_]

def crest_sized(box, page):
    return 0.6 <= box.width / max(box.height, 1e-06) <= 1.7 and box.get_area() <= 0.06 * page.rect.width * page.rect.height

def has_diagram(page):
    top = page.rect.height * 0.22
    for d in page.get_drawings():
        if (fitz.Rect(d['rect']) * page.rotation_matrix).y0 <= top:
            continue
        for item in d['items']:
            if item[0] == 'c':
                return True
            if item[0] == 'l' and abs(item[1].x - item[2].x) > 1 and (abs(item[1].y - item[2].y) > 1):
                return True
    area = page.rect.width * page.rect.height
    for info in page.get_image_info(xrefs=True):
        box = fitz.Rect(info['bbox']) * page.rotation_matrix
        barcode = box.width >= 3 * box.height and box.height < page.rect.height * 0.08
        if box.y0 > top and (not barcode) and (box.get_area() < 0.8 * area) and (box.width > page.rect.width * 0.1) and (box.height > page.rect.height * 0.05) and (not crest_sized(box, page)) and (not blank_image(page, info['xref'])):
            return True
    return False
