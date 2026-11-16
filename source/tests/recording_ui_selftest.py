"""Real Qt integration of continuous recording, save/recovery, and close handling."""


def run_recording_ui_tests(namespace):
    import csv
    import json
    import threading
    import time
    import traceback
    import tempfile
    from pathlib import Path
    import numpy as np
    import pyqtgraph as pg
    from PyQt5 import QtCore, QtWidgets
    from roi_intensity import RoiIntensityPanel
    import continuous_recording as storage

    base_out = Path.cwd() / '_recording_ui_test_output'
    base_out.mkdir(exist_ok=True)
    out = Path(tempfile.mkdtemp(prefix='run_', dir=str(base_out)))
    result = {'status': 'running', 'hardware_access': False, 'cases': [], 'messages': []}
    result['artifacts_directory'] = str(out)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = panel = None
    original_messages = {key: getattr(QtWidgets.QMessageBox, key) for key in ('information', 'warning', 'question')}
    original_picker = QtWidgets.QFileDialog.getOpenFileName
    original_capture = namespace['capture_metadata'].take_capture
    original_export = storage.export_journal
    answer = [QtWidgets.QMessageBox.No]
    chooser_calls = []
    target = [out / 'continuous.csv']
    clock = [time.perf_counter()]
    frames = [0]

    def handler(kind):
        def call(*args, **kwargs):
            result['messages'].append({'kind': kind, 'title': args[1], 'text': args[2]})
            if kind == 'question':
                assert args[4] == QtWidgets.QMessageBox.No, 'confirmation default must be No'
                return answer[0]
            return QtWidgets.QMessageBox.Ok
        return call

    def choose(name):
        chooser_calls.append(name)
        return str(target[0])

    def pump(predicate, timeout=10):
        deadline = time.perf_counter() + timeout
        while not predicate():
            app.processEvents()
            panel._recording.poll()
            if time.perf_counter() > deadline:
                raise AssertionError('timed out: ' + panel._recording.label.text())
            time.sleep(.005)
        app.processEvents()

    def feed(value=12, delta=1.0):
        clock[0] += delta
        frame = np.full((12, 16), value, dtype=np.uint8)
        view.setImage(frame, autoLevels=False, levels=(0, 255))
        panel.consume_frame(frame, clock[0])
        frames[0] += 1

    def roi(bounds):
        x0, y0, x1, y1 = bounds
        panel.roi.setPos((x0, y0))
        panel.roi.setSize((x1-x0, y1-y0))
        panel.roi.show()
        panel._region_changed()

    def passed(name, **details):
        result['cases'].append({'name': name, 'status': 'passed', **details})

    try:
        for kind in original_messages:
            setattr(QtWidgets.QMessageBox, kind, handler(kind))
        namespace['capture_metadata'].take_capture = lambda frame: None
        window = QtWidgets.QWidget()
        window.setWindowTitle('FLIR continuous recording — hardware-free verification')
        layout = QtWidgets.QVBoxLayout(window)
        view = pg.ImageView()
        view.getImageItem().setOpts(axisOrder='row-major')
        view.ui.roiBtn.hide()
        view.ui.menuBtn.hide()
        layout.addWidget(view, 1)
        panel = RoiIntensityPanel(view.ui.graphicsView, view.getImageItem())
        layout.addWidget(panel)
        panel.peak_timer.stop()
        controller = panel._recording
        controller.root_override = out / 'cache'
        panel._choose_export_path = choose
        window.resize(1500, 960)
        window.show()
        app.processEvents()
        assert controller.start() is False and controller.recorder is None
        passed('start_requires_a_valid_roi')

        feed()
        roi((2, 3, 7, 7))
        panel.record_button.click()
        rec = controller.recorder
        assert rec is not None and rec.accepting and panel.record_button.text() == '停止记录'
        assert controller.start() is False and controller.recorder is rec
        began = time.perf_counter()
        for i in range(180):
            if i == 90:
                roi((0, 0, 4, 4))
            if i == 120:
                panel.reset_button.click()
            feed()
        elapsed = time.perf_counter() - began
        assert rec.submitted == 180 and len(panel.samples) < 180 and rec.accepting, rec.status()
        feed(255, 2.1)
        assert rec.saturation_history['detected']
        feed(12, 2.1)
        panel.reset_button.click()
        assert not panel._saturation_history['detected'] and rec.saturation_history['detected']
        controller.poll()
        app.processEvents()
        assert window.grab().save(str(out / 'recording.png'))
        passed('continuous_clock_and_all_180_points_survive_roi_change_and_curve_reset', producer_loop_seconds=elapsed)

        panel.record_button.click()
        assert not rec.accepting
        before_count = rec.submitted
        feed()
        assert rec.submitted == before_count
        pump(lambda: controller.report is not None and controller.job is None)
        assert rec.done.is_set() and controller.report['sample_count'] == 182
        assert controller.report['saturation_history']['detected']
        assert not chooser_calls and controller.source.exists()
        assert any(item['title'] == '连续记录过曝保存确认' for item in result['messages'])
        journal = rec.path.read_bytes()
        passed('stop_freezes_session_and_declining_overexposure_save_keeps_cache')

        # Keep the export worker deliberately busy and prove Qt/preview stays live.
        entered, release = threading.Event(), threading.Event()
        def slow_export(*args):
            entered.set()
            assert release.wait(5)
            return original_export(*args)
        storage.export_journal = slow_export
        answer[0] = QtWidgets.QMessageBox.Yes
        assert controller.save()
        assert entered.wait(2)
        heartbeat = []
        QtCore.QTimer.singleShot(0, lambda: heartbeat.append(True))
        feed()
        app.processEvents()
        assert heartbeat and controller.job is not None and not controller.job.done.is_set()
        release.set()
        pump(lambda: controller.source is None)
        storage.export_journal = original_export
        assert not rec.path.exists(), 'successful save did not remove its own cache'
        rows = list(csv.reader(target[0].open(encoding='utf-8-sig', newline='')))
        assert len(rows) == 183 and float(rows[-1][0]) > 180
        assert rows[1][2:] == ['2', '3', '5', '4'] and rows[91][2:] == ['0', '0', '4', '4']
        text = target[0].with_suffix('.txt').read_text(encoding='utf-8-sig')
        summary = json.loads(text.split('--- BEGIN JSONL ---\n')[1].splitlines()[0])
        assert summary['saturation_history']['detected'] and summary['sample_count'] == 182
        passed('confirmed_export_runs_off_ui_and_preserves_whole_session_with_parameter_txt')

        torn = out / 'recovery' / 'session.jsonl'
        torn.parent.mkdir(exist_ok=True)
        torn.write_bytes(b''.join(journal.splitlines(keepends=True)[:-1]) + b'{"type":"sample"')
        QtWidgets.QFileDialog.getOpenFileName = lambda *a, **k: (str(torn), '')
        target[0] = out / 'recovered.csv'
        answer[0] = QtWidgets.QMessageBox.No
        controller.recover()
        pump(lambda: controller.report is not None and controller.job is None)
        assert controller.report['sample_count'] == 182 and not controller.report['complete']
        assert torn.exists() and not target[0].exists()
        answer[0] = QtWidgets.QMessageBox.Yes
        assert controller.save()
        pump(lambda: controller.source is None)
        assert torn.exists(), 'recovered source belonging to another session was deleted'
        assert len(list(csv.reader(target[0].open(encoding='utf-8-sig')))) == 183
        passed('recover_button_restores_complete_prefix_and_keeps_original_cache')

        assert controller.start()
        rec = controller.recorder
        feed()
        feed()
        window.close()
        assert not rec.accepting, 'closing window did not stop recording'
        pump(lambda: not window.isVisible())
        assert rec.done.is_set() and rec.path.exists()
        report = storage.inspect_journal(rec.path)
        assert report['complete'] and report['sample_count'] == 2
        passed('window_close_drains_writer_and_preserves_recoverable_session')
        result['status'] = 'passed'
    except Exception:
        result['status'] = 'failed'
        result['traceback'] = traceback.format_exc()
        if window is not None:
            window.grab().save(str(out / 'failure.png'))
        raise
    finally:
        storage.export_journal = original_export
        namespace['capture_metadata'].take_capture = original_capture
        QtWidgets.QFileDialog.getOpenFileName = original_picker
        for kind, function in original_messages.items():
            setattr(QtWidgets.QMessageBox, kind, function)
        if panel is not None:
            panel._recording.timer.stop()
            panel.peak_timer.stop()
            if panel._recording.recorder is not None:
                panel._recording.recorder.stop()
                panel._recording.recorder.done.wait(5)
            if panel._recording.job is not None:
                panel._recording.job.done.wait(5)
        (out / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        (base_out / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        if window is not None:
            for box in list(pg.ViewBox.AllViews):
                scene = box.scene()
                if scene is not None and any(view.window() is window for view in scene.views()):
                    try:
                        box.destroyed.disconnect()
                    except TypeError:
                        pass
                    box.setXLink(None)
                    box.setYLink(None)
                    box.unregister()
            window.close()
            window.deleteLater()
            QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)
        app.processEvents()
    return result
