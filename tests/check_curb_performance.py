"""Compare an unmodified baseline with the current curb engine and outputs.

Usage: python tests/check_curb_performance.py path/to/baseline.py
"""
import importlib.util
import random
import sys
import tempfile
import time
from pathlib import Path


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


root = Path(__file__).resolve().parents[1]
old = load('gradeguard_baseline', sys.argv[1])
new = load('gradeguard_current', root / 'schoel_gradeguard.py')


def values(segments):
    return [(s.a, s.b, s.layer) for s in segments]


rng = random.Random(20260916)
for case in range(60):
    # Shuffled/reversed chains, branches, duplicates, disconnected pieces,
    # negative coordinates, and gaps at the existing 1.50-foot boundary.
    data = []
    for i in range(35):
        a = (i * 3.0 - 50, float(case % 3))
        b = (a[0] + rng.choice([1.5, 3.0, 4.5]), a[1])
        if rng.random() < .5:
            a, b = b, a
        data.append((a, b, 'BC'))
    data += [((0, 0), (0, 20), 'branch'), ((200, 200), (220, 200), 'separate')]
    data += data[:2]
    rng.shuffle(data)
    for module in (old, new):
        module.CURB_SEGMENTS = [module.CurbSegment(*s) for s in data]
    for p in [(-49, 0), (0, 0), (210, 201)]:
        assert values(old._local_curb_segments_for_point(p)) == values(new._local_curb_segments_for_point(p))
    for start in [0, 10, len(data) - 1]:
        assert values(old._order_connected_curb_run(old.CURB_SEGMENTS, start)) == values(new._order_connected_curb_run(new.CURB_SEGMENTS, start))
print('PASS: 360 branch/gap/tie/connectivity comparisons')

for module in (old, new):
    module.CURB_SEGMENTS = [module.CurbSegment((i * 2., 0.), (i * 2. + 2., 0.), 'BC') for i in range(1500)]
    started = time.perf_counter()
    result = module._local_curb_segments_for_point((1500., 0.))
    print(module.__name__, '1500-segment curb:', round(time.perf_counter() - started, 3), 'seconds')
    assert len(result) == 1500

with tempfile.TemporaryDirectory(prefix='gradeguard-perf-') as tmp:
    pdf = Path(tmp) / 'labels.pdf'
    with new.fitz.open() as doc:
        page = doc.new_page(width=1000, height=1000)
        for i in range(100):
            x, y = 25 + i % 10 * 90, 25 + i // 10 * 90
            page.insert_text((x, y), 'TC 615.25')
            page.draw_line((x, y + 5), (x + 35, y + 35))
        doc.save(pdf)
    labels = []
    for module in (old, new):
        calls = [0]
        original = module._pdf_page_line_segments
        def counted(page):
            calls[0] += 1
            return original(page)
        module._pdf_page_line_segments = counted
        started = time.perf_counter()
        module.affine_from_layout_coordinates = lambda path: (([1., 0., 0.], [0., 1., 0.]), {})
        result, _ = module.extract_pdf_spot_labels(str(pdf), .5)
        labels.append([vars(label) for label in result])
        print(module.__name__, 'PDF vector reads:', calls[0], 'seconds:', round(time.perf_counter() - started, 3))
        if module is new:
            assert calls[0] == 1
        module._pdf_page_line_segments = original
    assert labels[0] == labels[1] and len(labels[1]) == 100
    print('PASS: all 100 PDF label elevations and leader endpoints identical')
