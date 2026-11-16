"""Actual acquisition method + native Qt + simulated SDK image ownership."""


def run_photo_ui_tests(namespace):
    import json
    import marshal
    import re
    import sys
    import threading
    import time
    import traceback
    import types
    from pathlib import Path
    import numpy as np
    from PyQt5 import QtCore, QtGui, QtWidgets
    from camera_backend import FlirCameraBackend
    from camera_modes_test import MockBackend, Spin
    import photo_storage as storage
    import camera_modes
    output = Path.cwd() / '_photo_ui_test_output'
    output.mkdir(exist_ok=True)
    original = types.ModuleType('_photo_original_main')
    original.__file__ = str(Path(sys.executable).parent / 'main.py')
    exec(marshal.loads(namespace['_original_main_code']), original.__dict__)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    result = {'status':'running', 'hardware_access':False, 'cases':[], 'messages':[]}
    errors, releases, raw_images = [], [], []
    old_hook, old_warning, old_session = sys.excepthook, QtWidgets.QMessageBox.warning, storage.Session
    sys.excepthook = lambda typ, value, tb: errors.append(''.join(traceback.format_exception(typ, value, tb)))
    QtWidgets.QMessageBox.warning = lambda parent, title, text, *args: result['messages'].append({'title':title, 'text':text}) or QtWidgets.QMessageBox.Ok
    class SpinError(Exception):
        def __init__(self, code):
            self.errorcode = code
            super().__init__('mock SDK error ' + str(code))
    class PhotoSpin(Spin):
        SPINNAKER_ERR_TIMEOUT = -1011
        SpinnakerException = SpinError
    class Image:
        def __init__(self, ident, incomplete=False):
            self.ident, self.incomplete = ident, incomplete
            self.array = np.arange(64*96, dtype=np.uint16).reshape(64, 96)
            self.array[0, 0], self.array[-1, -1] = 65520, 65535
            raw_images.append(self)
        def IsIncomplete(self): return self.incomplete
        def GetNDArray(self): return self.array
        def HasChunkData(self): return False
        def GetFrameID(self): return self.ident
        def GetTimeStamp(self): return self.ident * 100000000
        def GetPixelFormatName(self): return 'Mono16'
        def GetPixelFormat(self): return 17825799
        def GetWidth(self): return 96
        def GetHeight(self): return 64
        def GetBitsPerPixel(self): return 16
        def Release(self):
            self.array.fill(0)  # Raw saved copy must survive SDK buffer reuse.
            releases.append(self.ident)
    class Backend(MockBackend):
        grab_frame = FlirCameraBackend.grab_frame
        def __init__(self, serial='PHOTOTEST'):
            super().__init__(serial)
            self._spin = PhotoSpin
            self.frames = []
            def next_image(timeout):
                if not self.frames:
                    raise SpinError(-1011)
                value = self.frames.pop(0)
                if isinstance(value, Exception): raise value
                return value
            self.camera.GetNextImage = next_image
        def _log(self, message): pass
    window = None
    release = threading.Event()
    def pump(seconds=.08):
        end = time.perf_counter() + seconds
        while time.perf_counter() < end:
            app.processEvents()
            time.sleep(.005)
        if errors: raise AssertionError('\n'.join(errors))
    def until(predicate, timeout=5):
        end = time.perf_counter() + timeout
        while not predicate():
            assert time.perf_counter() < end, 'photo UI timed out'
            pump(.03)
        pump()
    try:
        window = original.RoiCameraWindow(backend_factory=Backend, camera_provider=lambda:[('PHOTOTEST', '模拟触发相机')])
        window.show()
        pump()
        ui, panel = window._workflow, window.roi_intensity_panel
        ui.photos.set_root(output / 'photos')
        ui.photos.name.setText('实验_甲')
        ui.mode.setCurrentIndex(1)
        window.start_button.click()
        pump()
        backend = window.backend
        assert backend and backend.streaming, window.state_label.text()
        assert camera_modes.read(backend, 'StreamBufferHandlingMode') == 'OldestFirst'
        first = backend._photo_session
        assert first is not None and first.written == 0
        assert not ui.photos.name.isEnabled(), 'name must remain stable while collecting this batch'
        assert panel.bounds is None, 'test photos must be saved without ROI'
        backend.frames.extend([Image(1), Image(2), Image(4)])
        until(lambda:first.written == 3)
        assert releases == [1, 2, 4] and backend.streaming
        files = sorted(first.path.glob('*.tif'))
        for index, path in enumerate(files, 1):
            assert re.fullmatch(r'\d{8}-\d{6}-\d{3}_实验_甲_\d{6}\.tif', path.name), path.name
            assert path.stem.endswith('_{:06d}'.format(index))
            info = json.loads(path.with_suffix('.txt').read_text(encoding='utf-8'))
            assert info['stored_shape'] == [64, 96] and info['stored_dtype'] == 'uint16'
            assert info['camera']['nodes']['StreamBufferHandlingMode']['value'] == 'OldestFirst'
            qimage = QtGui.QImage(str(path))
            assert not qimage.isNull() and qimage.width() == 96 and qimage.height() == 64
        assert first.id_gaps and first.id_gaps[0]['received'] == 4
        result['cases'].append('actual grab/release path saves raw full frame without ROI, FIFO, naming, TXT, native TIFF decoding')
        assert window.grab().save(str(output / 'photos_saved.png'))
        ui.mode.setCurrentIndex(0)
        until(lambda:first.done.is_set())
        assert camera_modes.read(backend, 'StreamBufferHandlingMode') == 'NewestOnly'
        backend.frames.append(Image(5))
        pump(.15)
        assert first.written == 3 and backend._photo_session is None
        result['cases'].append('continuous preview stops photo saving and restores preview buffer mode')
        window.start_button.click()
        pump()
        ui.photos.name.setText('实验乙')
        ui.mode.setCurrentIndex(1)
        pump()
        assert backend._photo_session is None and ui.photos.name.isEnabled()
        result['cases'].append('stopped external configuration keeps shoot name editable until start')
        window.start_button.click()
        second = backend._photo_session
        backend.frames.append(Image(6, incomplete=True))
        until(lambda:second.done.is_set() and bool(result['messages']))
        assert not backend.streaming and second.rejected == 1 and second.written == 0
        assert 6 in releases, 'incomplete frame must release SDK buffer'
        result['cases'].append('incomplete image stops acquisition, reports unsaved frame and releases SDK buffer')
        ui.photos.enabled.setChecked(False)
        window.start_button.click()
        pump()
        assert backend.streaming and backend._photo_session is None
        backend.frames.append(Image(7))
        pump(.15)
        window.start_button.click()
        pump()
        ui.photos.enabled.setChecked(True)
        result['cases'].append('explicitly disabled photo saving does not create a session')
        previous_messages = len(result['messages'])
        window.start_button.click()
        error_session = backend._photo_session
        backend.frames.append(SpinError(-1001))
        until(lambda:error_session.done.is_set() and len(result['messages']) > previous_messages)
        assert not backend.streaming and error_session.error and error_session.written == 0
        result['cases'].append('non-timeout SDK retrieval errors stop acquisition and report failure')
        def broken_writer(*args):
            raise OSError('simulated disk full')
        storage.Session = lambda *args, **kw: old_session(*args, writer=broken_writer, **kw)
        previous_messages = len(result['messages'])
        window.start_button.click()
        disk_session = backend._photo_session
        backend.frames.append(Image(9))
        until(lambda:disk_session.done.is_set() and len(result['messages']) > previous_messages)
        assert not backend.streaming and disk_session.error and disk_session.status()['unsaved'] == 1
        assert 9 in releases and disk_session.pending_bytes == 0
        result['cases'].append('asynchronous disk failure stops camera and releases pending raw storage')
        entered = threading.Event()
        def slow_writer(*args):
            entered.set()
            assert release.wait(5)
            return storage.write_pair(*args)
        storage.Session = lambda *args, **kw: old_session(*args, writer=slow_writer, **kw)
        window.start_button.click()
        final_session = backend._photo_session
        backend.frames.append(Image(8))
        until(entered.is_set)
        heartbeat = []
        window.close()
        assert window.isVisible(), 'close should wait for pending photo'
        QtCore.QTimer.singleShot(0, lambda:heartbeat.append(True))
        pump()
        assert heartbeat and not backend.streaming
        release.set()
        until(lambda:not window.isVisible())
        assert final_session.done.is_set() and final_session.written == 1
        result['cases'].append('closing drains photo writer without blocking Qt')
        result['status'] = 'passed'
    except BaseException:
        result['status'] = 'failed'
        result['error'] = traceback.format_exc()
        if window:
            window.grab().save(str(output / 'failure.png'))
        raise
    finally:
        release.set()
        if window:
            window.close()
            for session in window._workflow.photos.sessions:
                session.stop()
                session.done.wait(5)
        storage.Session = old_session
        QtWidgets.QMessageBox.warning = old_warning
        sys.excepthook = old_hook
        (output / 'report.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=True))
