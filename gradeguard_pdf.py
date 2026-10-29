"""PDF observations, never automatic survey controls. PDF coordinates are points.

Text/OCR boxes locate printed evidence, not the physical leader endpoint. A
reviewed curb control uses separately entered CAD E,N and an explicit meaning.
"""
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
import csv
import hashlib
import io
import json
import math
import os
import re
import shutil
import subprocess
import tempfile

import fitz


@dataclass
class PdfGrade:
    id: str
    page: int
    bbox: tuple
    label: str
    elevation: float
    raw: str
    method: str = 'PDF text'
    flags: str = 'Meaning and physical location unconfirmed'


NUMBER = re.compile(r'(?<![\w.])[-+]?\d{1,7}\.\d{1,4}(?![\w.])')
LABEL = (r'(?:HIGH POINT|LOW POINT|TOP/BACK OF CURB|TOP OF CURB|BACK OF CURB|'
         r'BOTTOM OF CURB|TOP OF CONCRETE|HPS|LPS|HP|LP|TBC|TOC|BOC|TC|BC|TB|'
         r'GUTTER|GUT|FL|FG|FS|FFE|FF|ELEVATION|ELEV|EL|RIM|SG|TP|TS|BS|TW|BW|TD|GB|LC)')
LABEL_GROUP = LABEL + r'(?:\s*/\s*' + LABEL + r')*'
PREFIX = re.compile(r'(?<![A-Z])(' + LABEL_GROUP + r')\s*(?:\(E\)\s*)?[=:\s(]*$')
SUFFIX = re.compile(r'^[)\s:=]*(?:\([A-Z][A-Z -]{0,30}\)\s*)?(' + LABEL_GROUP + r')(?![A-Z])')


def normalize(raw):
    text = raw.upper().replace('\\P', '\n').replace('\u2212', '-')
    # Only known dotted abbreviations; decimal points are preserved.
    for abbr in ('TBC', 'BOC', 'HPS', 'LPS', 'HP', 'LP', 'TC', 'BC', 'FL', 'FG', 'FS', 'GB'):
        pattern = r'\b' + r'\s*'.join(letter + r'\.' for letter in abbr)
        text = re.sub(pattern, abbr, text)
    text = re.sub(r'\s+', ' ', text).strip()
    text = re.sub(r'\b(HIGH POINT|LOW POINT|HP|LP)\s+(?=TOP OF CURB|BACK OF CURB|BOTTOM OF CURB|TC\b|BC\b|BOC\b|EL\b)', r'\1/', text)
    return text


def parse_callouts(raw):
    """Read prefix/suffix/stacked labels without assigning engineering meaning.

    Bare decimals are observations only when the whole block is one value.
    Percentages, dimensions, dates and unlabeled numbers in prose are excluded.
    """
    text = normalize(raw)
    matches = list(NUMBER.finditer(text))
    if not matches:
        return []
    first = matches[0]
    orientation = 'prefix' if PREFIX.search(text[:first.start()]) else 'suffix'
    result = []
    for i, match in enumerate(matches):
        left = text[matches[i-1].end() if i else 0:match.start()]
        right = text[match.end():matches[i+1].start() if i+1 < len(matches) else len(text)]
        if re.match(r'\s*(?:%|[\"\u2033]|FT\b|FEET\b|AC\b|SF\b)', right):
            continue
        pre, post = PREFIX.search(left), SUFFIX.search(right)
        chosen = (pre or post) if orientation == 'prefix' else (post or pre)
        if chosen:
            label = re.sub(r'\s*/\s*', '/', chosen.group(1))
        elif len(matches) == 1 and re.fullmatch(r'\(?\s*[-+]?\d{1,7}\.\d{1,4}\s*\)?', text):
            label = 'SPOT'
        else:
            continue
        flags = ['Meaning and physical location unconfirmed']
        if re.search(r'\(E\)|\bEX(?:IST(?:ING)?)?\b|\bMATCH\b|\bHOLD\b', text):
            flags.append('Existing/match evidence; verify intended design')
        if any(k in text for k in ('LEGEND', 'DENOTES', 'PROPOSED FINISH', 'ELEVATION OF')):
            flags.append('Possible legend/example')
        if any(k in label for k in ('BC', 'BOC', 'TC', 'TB')):
            flags.append('Curb abbreviation requires plan legend')
        if 'HP' in label or 'LP' in label or 'POINT' in label:
            flags.append('High/low may describe ground, swale or pavement')
        result.append((label, float(match.group()), '; '.join(flags)))
    return result


def fingerprint(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def candidates_from_blocks(blocks, page, method='PDF text'):
    rows = []
    for bbox, raw in blocks:
        for index, (label, value, flags) in enumerate(parse_callouts(raw)):
            key = json.dumps([page, [round(x, 2) for x in bbox], raw, index, method])
            identity = hashlib.sha256(key.encode()).hexdigest()[:20]
            if method == 'OCR':
                flags += '; OCR: verify every digit against the image'
            rows.append(PdfGrade(identity, page, tuple(bbox), label, value, raw, method, flags))
    return rows


def scan_pdf(path, progress=None):
    rows, pages = [], []
    with fitz.open(path) as doc:
        if doc.needs_pass:
            raise ValueError('This PDF is password protected. Use an accessible local copy.')
        for i, page in enumerate(doc):
            blocks = []
            for block in page.get_text('dict')['blocks']:
                if block.get('type') == 0:
                    raw = '\n'.join(' '.join(span['text'] for span in line['spans']) for line in block['lines'])
                    blocks.append((block['bbox'], raw))
            found = candidates_from_blocks(blocks, i + 1)
            rows.extend(found)
            pages.append(dict(page=i+1, text_blocks=len(blocks), candidates=len(found),
                              note='Text scan only; drawn/scanned labels may need OCR or manual review.'))
            if progress:
                progress(i+1, len(doc))
    return rows, dict(pdf_sha256=fingerprint(path), pages=pages, candidate_count=len(rows),
                      warning='Observations only. Every sheet must be visually checked for missed and misread callouts.')


def tesseract_path():
    return shutil.which('tesseract') or next((p for p in (
        r'C:\Program Files\Tesseract-OCR\tesseract.exe',
        r'C:\Program Files (x86)\Tesseract-OCR\tesseract.exe') if os.path.isfile(p)), None)


def ocr_page(path, page_number):
    """OCR is optional, local, and review-only. No document leaves the computer."""
    exe = tesseract_path()
    if not exe:
        raise ValueError('Local Tesseract OCR is not installed. Text reading and manual callouts remain available.')
    with fitz.open(path) as doc, tempfile.TemporaryDirectory(prefix='gradeguard-ocr-') as folder:
        page = doc[page_number - 1]
        scale = min(300 / 72, math.sqrt(60_000_000 / (page.rect.width * page.rect.height)))
        pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        src = Path(folder) / 'page.png'
        pix.save(src)
        args = [exe, str(src), 'stdout', '-l', 'eng', '--psm', '11', 'tsv']
        completed = subprocess.run(args, capture_output=True, encoding='utf-8', errors='replace', timeout=120,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if completed.returncode:
            raise ValueError('OCR failed: ' + completed.stderr[-800:])
        lines = {}
        for word in csv.DictReader(io.StringIO(completed.stdout), delimiter='\t'):
            if not word.get('text', '').strip() or word.get('level') != '5':
                continue
            key = tuple(word[k] for k in ('block_num', 'par_num', 'line_num'))
            x, y, w, h = (int(word[k]) / scale for k in ('left', 'top', 'width', 'height'))
            box = fitz.Rect(x, y, x+w, y+h) * page.derotation_matrix
            if key not in lines:
                lines[key] = [box, []]
            lines[key][0] |= box
            lines[key][1].append(word['text'])
        blocks = [(tuple(box), ' '.join(words)) for box, words in lines.values()]
        return candidates_from_blocks(blocks, page_number, 'OCR')


def render_evidence(path, page_number, bbox=None, width=1100):
    with fitz.open(path) as doc:
        page = doc[page_number-1]
        clip = page.rect
        highlight = None
        if bbox:
            highlight = fitz.Rect(bbox) * page.rotation_matrix
            clip = (highlight + (-65, -65, 65, 65)) & page.rect
        matrix = fitz.Matrix(min(width / max(clip.width, 1), 6), min(width / max(clip.width, 1), 6))
        pix = page.get_pixmap(matrix=matrix, clip=clip, alpha=False)
        rect = None
        if highlight:
            r = highlight * matrix
            rect = (r.x0-pix.x, r.y0-pix.y, r.x1-pix.x, r.y1-pix.y)
        return pix.tobytes('png'), rect


def reviewed_control(candidate, elevation, easting, northing, meaning, role, height, reference, confirmed):
    if not confirmed:
        raise ValueError('Confirm the printed value, legend/detail, CAD tick, units and vertical reference first.')
    if meaning not in ('Top/back of curb', 'Flowline/gutter/bottom of curb'):
        raise ValueError('Choose the meaning verified in this plan legend/detail.')
    if role not in ('Spot', 'High point', 'Low point'):
        raise ValueError('Choose spot, high point or low point for this curb control.')
    values = [float(v) for v in (elevation, easting, northing)]
    if not all(math.isfinite(v) for v in values):
        raise ValueError('Elevation, Easting and Northing must be finite numbers.')
    if not str(reference).strip():
        raise ValueError('Record the legend/detail sheet and benchmark/vertical reference checked.')
    adjustment = 0.0
    if meaning == 'Flowline/gutter/bottom of curb':
        adjustment = float(height)
        if not math.isfinite(adjustment) or adjustment < 0:
            raise ValueError('Enter the local curb rise in feet (zero for a confirmed flush curb).')
    top = values[0] + adjustment
    if not math.isfinite(top) or abs(top) <= 1e-9:
        raise ValueError('The resulting grade is nonfinite or zero; the engine keeps it in review.')
    return dict(id=candidate.id, page=candidate.page, kind={'Spot':'TC', 'High point':'HP_TC', 'Low point':'LP_TC'}[role],
                elev=top, cad=values[1:], raw=candidate.raw, pdf_bbox=list(candidate.bbox),
                detected_label=candidate.label, detected_elevation=candidate.elevation,
                raw_elevation=values[0], meaning=meaning, role=role, curb_rise=adjustment,
                reference=reference.strip(), confirmed=True, units='US survey feet',
                method=candidate.method, reviewed_utc=datetime.now(timezone.utc).isoformat())


def validate_reviewed_data(data, pdf_path, cad_path):
    if data.get('schema') != 'gradeguard.pdf-controls.v1':
        raise ValueError('Unsupported PDF control format.')
    if not pdf_path or data.get('pdf_sha256') != fingerprint(pdf_path):
        raise ValueError('Reviewed PDF controls belong to a different or changed PDF. Review them again.')
    if not cad_path or data.get('dxf_sha256') != fingerprint(cad_path):
        raise ValueError('Reviewed PDF controls belong to a different or changed DXF. Review them again.')
    with fitz.open(pdf_path) as doc:
        page_count = len(doc)
    ids = set()
    for spot in data.get('manual_spots', []):
        if spot.get('units') != 'US survey feet' or spot.get('id') in ids:
            raise ValueError('Invalid units or duplicate PDF control.')
        if not isinstance(spot.get('page'), int) or not 1 <= spot['page'] <= page_count:
            raise ValueError('Invalid PDF control page.')
        ids.add(spot.get('id'))
        candidate = PdfGrade(spot['id'], spot['page'], tuple(spot['pdf_bbox']), spot['detected_label'],
                             spot['detected_elevation'], spot['raw'], spot['method'])
        expected = reviewed_control(candidate, spot['raw_elevation'], *spot['cad'], spot['meaning'],
                                    spot['role'], spot['curb_rise'], spot['reference'], spot.get('confirmed'))
        if spot['kind'] != expected['kind'] or not math.isfinite(float(spot['elev'])) or abs(float(spot['elev'])-expected['elev']) > 1e-9:
            raise ValueError('PDF control elevation/conversion disagrees with its reviewed source.')


def save_reviewed_data(path, spots, pdf_path, cad_path):
    if Path(path).resolve() in (Path(pdf_path).resolve(), Path(cad_path).resolve()):
        raise ValueError('A control file cannot overwrite an original PDF or DXF.')
    data = dict(schema='gradeguard.pdf-controls.v1', pdf_path=os.path.abspath(pdf_path),
                cad_path=os.path.abspath(cad_path), pdf_sha256=fingerprint(pdf_path),
                dxf_sha256=fingerprint(cad_path), manual_spots=spots, alignment_points=[])
    validate_reviewed_data(data, pdf_path, cad_path)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Atomic replacement; keep the previous saved review recoverable.
    if target.exists():
        shutil.copy2(target, str(target) + '.bak')
    fd, temporary = tempfile.mkstemp(prefix=target.name, suffix='.tmp', dir=target.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return data


def export_review(path, rows):
    with open(path, 'w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(asdict(rows[0])) if rows else list(PdfGrade.__dataclass_fields__))
        writer.writeheader()
        for row in rows:
            data = asdict(row)
            data['raw'] = data['raw'].replace('\n', ' | ')
            writer.writerow(data)
