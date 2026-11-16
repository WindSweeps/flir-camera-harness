"""Independent Pillow round trips and bounded queue / disk-fault checks."""
import json
import re
import sys
import tempfile
import threading
from pathlib import Path
import numpy as np
from PIL import Image
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import photo_storage as storage


def run():
    root = Path(tempfile.mkdtemp(prefix='photos_', dir=str(Path(__file__).resolve().parent)))
    results = []
    meta = {'image_retrieved_at':{'utc':'2026-11-13T07:30:12.084567+00:00'},
            'image_info':{'FrameID':{'value':1}}, 'nodes':{'ExposureTime':{'value':1234.5}}}
    for dtype in ('u1', '<u2', '>u2'):
        raw = np.array([[0, 1, 42], [254, 255, 65520 if '2' in dtype else 128]], dtype=dtype)
        expected = raw.copy()
        session = storage.Session(root, '实验甲')
        session.offer(raw, meta, {'roi_bounds':[0, 0, 1, 1]})
        raw.fill(0)
        session.stop()
        assert session.done.wait(5), 'writer did not finish'
        assert session.status()['written'] == 1 and not session.error, session.status()
        image = session.path / '20261113-153012-084_实验甲_000001.tif'
        with Image.open(image) as reader:
            assert np.array_equal(np.array(reader), expected)
            assert reader.tag_v2[258][0] == expected.dtype.itemsize * 8
            embedded = json.loads(reader.tag_v2[270])
        companion = json.loads(image.with_suffix('.txt').read_text(encoding='utf-8'))
        assert embedded == companion
        assert companion['camera']['nodes']['ExposureTime']['value'] == 1234.5
        assert companion['stored_shape'] == [2, 3], 'ROI must not crop photo'
        assert session.status()['pending_bytes'] == 0
        assert not list(session.path.glob('*.part'))
        results.append('independent raw pixel/TIFF/TXT/name roundtrip: ' + dtype)
    session = storage.Session(root, '含_下划线')
    for ident in (10, 11, 13):
        session.offer(np.ones((2, 2), 'u1'), dict(meta, image_info={'FrameID':{'value':ident}}))
    session.stop()
    assert session.done.wait(5) and session.written == 3
    assert len(session.id_gaps) == 1
    assert [p.stem.rsplit('_', 1)[1] for p in sorted(session.path.glob('*.tif'))] == ['000001', '000002', '000003']
    results.append('same-millisecond filenames unique, sequence increments, frame gaps retained')
    entered, release = threading.Event(), threading.Event()
    def blocked_writer(*args):
        entered.set()
        assert release.wait(5)
        return storage.write_pair(*args)
    session = storage.Session(root, '队列', max_frames=1, writer=blocked_writer)
    session.offer(np.ones((2, 2), 'u1'), meta)
    assert entered.wait(2)
    try:
        session.offer(np.ones((2, 2), 'u1'), meta)
    except storage.PhotoError:
        pass
    else:
        raise AssertionError('full queue must stop explicitly')
    assert session.pending == 1 and session.pending_bytes == 4
    release.set()
    assert session.done.wait(5) and session.written == 1 and session.rejected == 1
    results.append('queue counts include in-flight frame; overload drains accepted frame and reports rejected frame')
    session = storage.Session(root, '字节上限', max_bytes=3)
    try:
        session.offer(np.ones((2, 2), 'u1'), meta)
    except storage.PhotoError:
        pass
    assert session.done.wait(5) and session.submitted == 0 and session.rejected == 1
    results.append('byte cap enforced before copying')
    def broken_writer(*args):
        raise OSError('simulated disk full')
    session = storage.Session(root, '磁盘故障', writer=broken_writer)
    session.offer(np.ones((2, 2), 'u1'), meta)
    assert session.done.wait(5) and session.error and session.written == 0
    assert session.status()['unsaved'] == 1 and session.pending_bytes == 0
    results.append('disk fault surfaced and unsaved count exact')
    for invalid in ('', 'a/b', 'a:b', 'bad\x00name'):
        try:
            storage.Session(root, invalid)
        except storage.PhotoError:
            pass
        else:
            raise AssertionError('unsafe name accepted')
    results.append('invalid names rejected without path traversal')
    report = {'status':'passed', 'cases':results, 'path':str(root)}
    (root / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    run()
