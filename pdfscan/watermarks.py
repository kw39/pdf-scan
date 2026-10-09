"""Local watermark detection. Hide only independently verified text-paint operations.
Flattened scans and uncertain artwork stay intact with review warnings.
"""
import re
import unicodedata
from collections import Counter, defaultdict
import pymupdf as fitz


def compact(text):
    return re.sub(r"[^\w\u3400-\u9fff]", "", unicodedata.normalize("NFKC", text).casefold())


SCANNER = re.compile(r"\b(?:camscanner|adobe\s+scan|(?:microsoft|office)\s+lens|genius\s+scan|tiny\s+scanner|scanbot|scanner\s+pro|vflat|iscanner|turboscan)\b", re.I)
DOMAIN = re.compile(r"(?:https?://|www\.)\S+|\b[a-z0-9][a-z0-9.-]+\.(?:com|org|net|edu|hk|io|app)(?:/\S*)?\b", re.I)


def groups(page):
    result = defaultdict(list)
    for span in page.get_texttrace():
        if span['type'] == 0 and span['opacity'] > 0:
            result[span['seqno']].append(span)
    return list(result.values())


def group_text(spans):
    return ' '.join(''.join(chr(c[0]) for c in s['chars']) for s in spans).strip()


def group_box(page, spans):
    box = fitz.Rect(spans[0]['bbox'])
    for span in spans[1:]:
        box |= fitz.Rect(span['bbox'])
    box = (box * page.rotation_matrix) & page.rect
    return [box.x0 / page.rect.width, box.y0 / page.rect.height, box.x1 / page.rect.width, box.y1 / page.rect.height]


def repeat_key(page, spans):
    return compact(group_text(spans)), tuple(round(v, 2) for v in group_box(page, spans))


def repeated_labels(doc):
    counts = Counter()
    for page in doc:
        counts.update(set(repeat_key(page, s) for s in groups(page)))
    return counts


def classify(page, spans, aliases, repeats):
    text = group_text(spans)
    if not 3 <= len(text) <= 160 or not compact(text):
        return None
    box = group_box(page, spans)
    if box[0] >= box[2] or box[1] >= box[3]:
        return None
    edge = box[1] >= .88 or box[3] <= .12
    pale = all(s['opacity'] <= .55 or min(s['color']) >= .55 for s in spans)
    tilted = any(abs(s['dir'][1]) > .15 for s in spans)
    large = max(s['size'] for s in spans) >= 24 / min(1, 1800 / max(page.rect.width, page.rect.height))
    school = next((a for a in aliases if len(compact(a)) >= 5 and compact(text) == compact(a)), None)
    if not school and pale and (tilted or large):
        school = next((a for a in aliases if len(compact(a)) >= 2 and compact(text) == compact(a)), None)
    if school and (pale or tilted or large):
        return 'Text watermark', school
    caption = re.findall(r'[\w\u3400-\u9fff]+', DOMAIN.sub('', SCANNER.sub('', text)).casefold())
    scanner_caption = all(t in {'scanned','with','by','created','using','powered','download','from','pdf'} for t in caption)
    if SCANNER.search(text) and scanner_caption and (edge or (pale and (large or tilted))):
        return 'Scanner-app watermark', text
    if DOMAIN.search(text) and (edge or (pale and (large or tilted))):
        if scanner_caption:
            return 'Website watermark', text
    if repeats[repeat_key(page, spans)] >= 2 and pale and (tilted or large):
        return 'Possible publisher watermark', text
    return None


def tokens(data):
    """Lex PDF tokens without confusing strings, names or comments with operators.
    Inline-image streams are refused, because their binary data are not PDF tokens.
    """
    pos, size = 0, len(data)
    whitespace, delimiters = b'\x00\t\n\x0c\r ', b'()<>[]{}/%'
    while pos < size:
        if data[pos] in whitespace:
            pos += 1
            continue
        if data[pos] == 37:
            while pos < size and data[pos] not in b'\r\n':
                pos += 1
            continue
        start = pos
        if data[pos] == 40:
            depth = 1
            pos += 1
            while pos < size and depth:
                if data[pos] == 92:
                    pos += 2
                    continue
                depth += (data[pos] == 40) - (data[pos] == 41)
                pos += 1
            if depth:
                raise ValueError('Unclosed PDF string')
        elif data[pos:pos+2] in (b'<<', b'>>'):
            pos += 2
        elif data[pos] == 60:
            pos = data.find(b'>', pos + 1)
            if pos < 0:
                raise ValueError('Unclosed PDF hex string')
            pos += 1
        elif data[pos] == 47:
            pos += 1
            while pos < size and data[pos] not in whitespace + delimiters:
                pos += 1
        elif data[pos] in delimiters:
            pos += 1
        else:
            while pos < size and data[pos] not in whitespace + delimiters:
                pos += 1
        yield start, pos, data[start:pos]


def text_operations(data, image=False):
    start, depth, inside = None, 0, False
    operations = []
    for lo, hi, token in tokens(data):
        if token == b'BI':
            raise ValueError('Inline image content requires manual review')
        if start is None:
            start = lo
        if token in (b'[', b'<<'):
            depth += 1
        elif token in (b']', b'>>'):
            depth -= 1
        elif depth == 0 and re.fullmatch(rb"[A-Za-z*'\"]+", token) and token not in (b'true', b'false', b'null'):
            if token == b'BT':
                inside = True
            elif token == b'ET':
                inside = False
            elif token == b'Tr':
                # Hiding a text paint also removes its glyph clipping path. The
                # bbox/text signatures cannot verify graphics inside that mask.
                # Retain the page's objects instead of changing unrelated pixels.
                if float(data[start:lo].strip()) in (4, 5, 6, 7):
                    raise ValueError('Text clipping requires manual review')
            elif (inside and token in (b'Tj', b'TJ') and not image) or (image and token == b'Do'):
                operations.append((start, hi))
            start = None
        if depth < 0:
            raise ValueError('Invalid PDF container')
    if depth or inside:
        raise ValueError('Unbalanced PDF content')
    return operations


def signature(span):
    return (span['type'], span['font'], span['size'], span['dir'], span['color'], span['opacity'],
            tuple((c[0], c[1], tuple(c[2]), tuple(c[3])) for c in span['chars']))


def visible(page):
    return Counter(signature(s) for s in page.get_texttrace() if s['type'] in (0,1) and s['opacity'] > 0)


def graphics(page):
    return [(kind, box) for kind, box in page.get_bboxlog() if kind not in ('fill-text', 'stroke-text', 'ignore-text')]


def suppress(data, operations):
    for lo, hi in sorted(set(operations), reverse=True):
        op = data[lo:hi]
        data = data[:lo] + (b' q Q ' if op.rstrip().endswith(b'Do') else b' q 3 Tr '+op+b' Q ') + data[hi:]
    return data


def plan(page, aliases, repeats):
    candidates, warnings = [], []
    for spans in groups(page):
        kind = classify(page, spans, aliases, repeats)
        if kind:
            candidates.append((spans, kind))
    if not candidates:
        return [], [], warnings
    regions = [{'rect': group_box(page, spans), 'reason': kind[0], 'method': 'watermark-text', 'match': kind[1]}
               for spans, kind in candidates]
    probe = fitz.open()
    try:
        probe.insert_pdf(page.parent, from_page=page.number, to_page=page.number)
        target = probe[0]
        data = b'\n'.join(probe.xref_stream(x) for x in target.get_contents())
        if len(data) > 2_000_000 or len(candidates) > 12:
            raise ValueError('Watermark inspection budget exceeded')
        text_ops = text_operations(data)
        object_ops = text_operations(data, image=True)
        if len(text_ops) > 250 or len(object_ops) > 80:
            raise ValueError('Watermark inspection budget exceeded')
        # Form XObjects can contain an entire watermark split across several
        # text operators. Probe their page-level Do before individual text paints.
        # Accept only if exactly the candidate text disappears and all other
        # visible text and graphics remain identical; mixed-content forms fail.
        ops = object_ops + text_ops
        baseline, shapes = visible(target), graphics(target)
        xref = probe.get_new_xref()
        probe.update_object(xref, '<<>>')
        probe.update_stream(xref, data)
        target.set_contents(xref)
        accepted = []
        for index, (spans, kind) in enumerate(candidates):
            if kind[0].startswith('Possible'):
                continue
            wanted = Counter(signature(s) for s in spans)
            for op in ops:
                if op in accepted:
                    continue
                probe.update_stream(xref, suppress(data, [op]))
                after = visible(target)
                if baseline - after == wanted and not after - baseline and graphics(target) == shapes:
                    regions[index]['_operation'] = op
                    accepted.append(op)
                    break
        probe.update_stream(xref, suppress(data, accepted))
        expected = baseline.copy()
        for index, (spans, _) in enumerate(candidates):
            if '_operation' in regions[index]:
                expected.subtract(Counter(signature(s) for s in spans))
        if visible(target) != +expected or graphics(target) != shapes:
            raise ValueError('Combined watermark verification failed')
    except Exception as error:
        for region in regions:
            region.pop('_operation', None)
        warnings.append('Watermark objects retained: ' + type(error).__name__ + '; inspect before approval.')
    finally:
        probe.close()
    for region in regions:
        if '_operation' in region:
            warnings.append(region['reason'] + ' can be removed as a separate PDF object; inspect the preview.')
        else:
            warnings.append(region['reason'] + ' retained for manual review: ' + region['match'])
    return [r for r in regions if '_operation' in r], regions, warnings


def render(page, regions, dpi):
    ops = [r['_operation'] for r in regions if '_operation' in r]
    if not ops:
        return page.get_pixmap(dpi=dpi, alpha=False)
    with fitz.open() as clone:
        clone.insert_pdf(page.parent, from_page=page.number, to_page=page.number)
        target = clone[0]
        data = b'\n'.join(clone.xref_stream(x) for x in target.get_contents())
        xref = clone.get_new_xref()
        clone.update_object(xref, '<<>>')
        clone.update_stream(xref, suppress(data, ops))
        target.set_contents(xref)
        return target.get_pixmap(dpi=dpi, alpha=False)

def ocr_footer_regions(page, words, existing):
    """Recognise flattened scanner/domain footers; erase only isolated white-margin labels.
    A white moat around the complete OCR line prevents cutting adjacent ink. Central
    and overlapping scan watermarks are retained for review.
    """
    from PIL import Image
    import io
    lines = defaultdict(list)
    for word in words:
        lines[tuple(word[5:7])].append(word)
    regions, warnings, gray = [], [], None
    for line in lines.values():
        text = ' '.join(w[4] for w in line)
        if not SCANNER.search(text) and not DOMAIN.search(text):
            continue
        box = fitz.Rect(line[0][:4])
        for word in line[1:]:
            box |= fitz.Rect(word[:4])
        box = (box * page.rotation_matrix) & page.rect
        rect = [max(0, box.x0/page.rect.width-.002), max(0, box.y0/page.rect.height-.002),
                min(1, box.x1/page.rect.width+.002), min(1, box.y1/page.rect.height+.002)]
        if any(rect[0] <= r['rect'][2] and rect[2] >= r['rect'][0] and
               rect[1] <= r['rect'][3] and rect[3] >= r['rect'][1] for r in existing):
            continue
        residue = DOMAIN.sub('', SCANNER.sub('', text)).casefold()
        allowed = {'scanned', 'with', 'by', 'created', 'using', 'powered', 'download', 'from', 'pdf'}
        safe_words = all(t in allowed for t in re.findall(r'[\w\u3400-\u9fff]+', residue))
        isolated = False
        if safe_words and (rect[1] >= .90 or rect[3] <= .06) and rect[3]-rect[1] < .04:
            if gray is None:
                dpi = max(1, round(200 * min(1, 1800/max(page.rect.width,page.rect.height))))
                gray = Image.open(io.BytesIO(page.get_pixmap(dpi=dpi, alpha=False).tobytes('png'))).convert('L')
            W, H = gray.size
            x0, y0, x1, y1 = int(rect[0]*W), int(rect[1]*H), min(W,int(rect[2]*W)+1), min(H,int(rect[3]*H)+1)
            # Require whitespace on all four sides, including the page edge. A
            # clipped or touching mark is uncertain and is never auto-whitened.
            if x0 >= 4 and y0 >= 4 and x1+4 <= W and y1+4 <= H:
                strips = [(x0-4,y0-4,x1+4,y0), (x0-4,y1,x1+4,y1+4),
                          (x0-4,y0,x0,y1), (x1,y0,x1+4,y1)]
                isolated = all(gray.crop(b).getextrema()[0] >= 245 for b in strips)
        reason = 'Scanner-app watermark' if SCANNER.search(text) else 'Website watermark'
        if isolated:
            regions.append({'rect':rect, 'reason':reason+' in isolated scan margin', 'method':'watermark-ocr', 'match':text})
            warnings.append(reason+' can be removed from an isolated white scan margin; verify the footer.')
        else:
            warnings.append('Possible '+reason.lower()+' retained in scan/content; inspect manually: '+text[:160])
    return regions, warnings


def image_plan(page, info, excluded=()):
    """A crest already identified by the existing detector, removed as an image
    object when possible. A failed verification preserves the existing treatment.
    """
    probe = fitz.open()
    try:
        probe.insert_pdf(page.parent, from_page=page.number, to_page=page.number)
        target = probe[0]
        data = b'\n'.join(probe.xref_stream(x) for x in target.get_contents())
        if len(data) > 2_000_000:
            return None
        ops = text_operations(data, image=True)
        if len(ops) > 80:
            return None
        baseline, shapes = visible(target), Counter(graphics(target))
        wanted = Counter([('fill-image', tuple(info['bbox']))])
        xref = probe.get_new_xref(); probe.update_object(xref, '<<>>')
        probe.update_stream(xref,data);target.set_contents(xref)
        for op in ops:
            if op in excluded:
                continue
            probe.update_stream(xref,suppress(data,[op]))
            after=Counter(graphics(target))
            if shapes-after == wanted and not after-shapes and visible(target) == baseline:
                box=(fitz.Rect(info['bbox'])*page.rotation_matrix)&page.rect
                return {'rect':[box.x0/page.rect.width,box.y0/page.rect.height,box.x1/page.rect.width,box.y1/page.rect.height],
                        'method':'watermark-image', 'reason':'Identified school logo/watermark image', '_operation':op}
    except Exception:
        return None
    finally:
        probe.close()
    return None


def scan_text_candidates(page, words, aliases):
    """Large school wording in a scan body is a possible blended watermark.
    Never replace its broad bounding rectangle with white through questions.
    """
    lines=defaultdict(list)
    for word in words:
        lines[tuple(word[5:7])].append(word)
    candidates=[]
    for line in lines.values():
        text=' '.join(w[4] for w in line)
        alias=next((a for a in aliases if len(compact(a))>=5 and compact(text)==compact(a)),None)
        if not alias:
            continue
        box=fitz.Rect(line[0][:4])
        for word in line[1:]:box |= fitz.Rect(word[:4])
        box=(box*page.rotation_matrix)&page.rect
        rect=[box.x0/page.rect.width,box.y0/page.rect.height,box.x1/page.rect.width,box.y1/page.rect.height]
        if rect[1]>.15 and rect[3]<.88 and (rect[3]-rect[1]>.035 or
                (rect[2]-rect[0]>.45 and rect[3]-rect[1]>.025)):
            candidates.append({'rect':rect,'reason':'Text watermark','match':alias})
    return candidates
