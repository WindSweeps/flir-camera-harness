"""Actual original window, Qt widgets/signals, synthetic hardware and image replay."""


def run_workflow_tests(namespace):
    import json
    import marshal
    import sys
    import time
    import traceback
    import types
    from pathlib import Path
    from PyQt5 import QtCore, QtWidgets
    from camera_modes_test import MockBackend
    import camera_modes as modes
    import capture_metadata
    output = Path.cwd() / '_workflow_test_output'
    output.mkdir(exist_ok=True)
    result = {'status': 'running', 'hardware_access': False, 'cases': [], 'layouts': []}
    original = types.ModuleType('_reviewed_main')
    original.__file__ = str(Path(sys.executable).parent / 'main.py')
    exec(marshal.loads(namespace['_original_main_code']), original.__dict__)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    errors = []
    old_hook = sys.excepthook
    sys.excepthook = lambda typ, value, tb: errors.append(''.join(traceback.format_exception(typ, value, tb)))
    window = None
    def pump(seconds=.12):
        end = time.perf_counter() + seconds
        while time.perf_counter() < end:
            app.processEvents()
            time.sleep(.005)
        if errors:
            raise AssertionError('\n'.join(errors))
    try:
        window = original.RoiCameraWindow(backend_factory=MockBackend, camera_provider=lambda: [('MOCK', '模拟相机 · 仅测试')])
        window.show()
        pump()
        ui = window._workflow
        ui.photos.set_root(output / 'photos')
        ui.photos.name.setText('模拟测试')
        panel = window.roi_intensity_panel
        ui.mode.setCurrentIndex(1)
        assert window.backend is None, 'mode before start should be staged'
        window.start_button.click()
        pump()
        backend = window.backend
        assert backend is not None, window.state_label.text()
        assert modes.read(backend, 'TriggerMode') == 'On' and backend.streaming
        ui.refresh()
        assert '等待 Line0 上升沿' in ui.mode_note.text()
        assert window.acquisition_timer.isActive(), 'waiting for trigger must keep acquisition alive'
        result['cases'].append('external startup + idle waiting without images')
        ui.mode.setCurrentIndex(0)
        assert modes.read(backend, 'TriggerMode') == 'Off' and backend.streaming
        ui.mode.setCurrentIndex(1)
        ui.delay.setValue(15)
        ui.delay.editingFinished.emit()
        assert modes.read(backend, 'TriggerDelay') == 15 and backend.streaming
        backend.camera.nodes['TriggerActivation'].value = 'FallingEdge'
        backend.camera.nodes['TriggerActivation'].fail = 'RisingEdge'
        ui.delay.setValue(16)
        ui.delay.editingFinished.emit()
        assert ui.delay.value() == 15 and backend.streaming
        assert '切换失败' in window.state_label.text()
        backend.camera.nodes['TriggerActivation'].fail = None
        result['cases'].append('live switching, delay readback, failed change rolls UI back')
        window.auto_exposure_check.setChecked(True)
        assert modes.read(backend, 'GainAuto') == modes.read(backend, 'ExposureAuto') == 'Continuous'
        assert not window.gain_spin.isEnabled() and not window.exposure_spin.isEnabled()
        ui.gain_limit.setValue(14)
        ui.gain_limit.editingFinished.emit()
        assert modes.read(backend, 'AutoExposureGainUpperLimit') == 14
        panel._saturation_backend = backend
        panel._saturation_metadata = {'effective_parameters': {'ExposureTime': {'value': 4567}, 'Gain': {'value': 7.5}}}
        ui.refresh()
        assert window.gain_spin.value() == 7.5 and window.exposure_spin.value() == 4567
        window.auto_exposure_check.setChecked(False)
        assert window.gain_spin.isEnabled() and modes.read(backend, 'GainAuto') == 'Off'
        result['cases'].append('auto exposure + gain, actual display, cap edit, manual restore')
        window.close()
        pump()

        sample = Path(sys._MEIPASS) / 'sample' / 'camera_frame.png'
        second_sample = sample.with_name('camera_frame_2.png')
        window = original.RoiCameraWindow(backend_factory=lambda serial: original.ImageReplayBackend(Path(serial)),
            camera_provider=lambda: [(str(sample), '图片回放 · camera_frame.png'), (str(second_sample), '图片回放 · camera_frame_2.png')])
        window.show()
        pump()
        ui = window._workflow
        ui.photos.set_root(output / 'photos')
        panel = window.roi_intensity_panel
        window.start_button.click()
        pump(.25)
        assert window.current_frame is not None, window.state_label.text()
        assert not ui.mode.model().item(1).isEnabled(), 'do not simulate external trigger on replay'
        window.exposure_spin.setValue(8000)
        window.apply_exposure_button.click()
        window.gain_spin.setValue(3)
        window.apply_gain_button.click()
        assert window.backend.exposure == 8000 and window.backend.gain_db == 3
        window.auto_exposure_check.setChecked(True)
        assert window.backend.automatic and not window.gain_spin.isEnabled()
        window.auto_exposure_check.setChecked(False)
        assert not window.backend.automatic and window.gain_spin.isEnabled()
        first_frame = window.current_frame.copy()
        window.camera_combo.setCurrentIndex(1)
        pump(.25)
        assert window.backend.streaming and window.current_frame is not None
        assert first_frame.shape != window.current_frame.shape or not __import__('numpy').array_equal(first_frame, window.current_frame)
        assert panel.bounds is None, 'source switch must clear selection'
        window.camera_combo.setCurrentIndex(0)
        pump(.25)
        result['cases'].append('replay manual/automatic controls and live camera source switching')
        panel.roi.setPos((100, 100))
        panel.roi.setSize((180, 140))
        panel.roi.show()
        panel._region_changed()
        pump(.3)
        assert panel.bounds is not None and panel.samples
        panel._recording.root_override = output / 'cache'
        panel.record_button.click()
        pump(.3)
        assert panel._recording.recorder.accepting
        panel._recording.stop(auto_save=False)
        pump(.3)
        assert panel._recording.recorder.done.is_set()
        assert panel._recording.recorder.written > 0
        deadline = time.perf_counter() + 5
        while panel._recording.report is None or panel._recording.job is not None:
            assert time.perf_counter() < deadline, 'recording inspection did not finish'
            pump(.05)
        result['cases'].append('real replay, ROI, curve, continuous recording after relayout')
        for name, width, height, zoom in (('wide', 1600, 1000, 0), ('medium', 1280, 960, 0),
                                         ('narrow', 900, 960, 0), ('zoom150', 1400, 1000, 1),
                                         ('zoom200', 1280, 960, 2)):
            ui.zoom_combo.setCurrentIndex(zoom)
            window.resize(width, height)
            pump(.25)
            assert window.grab().save(str(output / (name + '.png')))
            body_font = window.start_button.font().pointSizeF()
            assert body_font > 0 and body_font % 6 == 0, body_font
            assert ui.side.horizontalScrollBar().maximum() == 0, 'sidebar horizontal overflow: ' + name
            assert ui.results_scroll.horizontalScrollBar().maximum() == 0, 'result horizontal overflow: ' + name
            assert ui.image_area.height() >= ui.workspace.height() * .68, 'image lost priority: ' + name
            image_height, image_width = window.current_frame.shape[:2]
            extent = window.image_plot.getViewBox().viewRect().adjusted(-.01, -.01, .01, .01)
            assert extent.contains(QtCore.QRectF(0, 0, image_width, image_height)), 'frame clipped: ' + name
            for button in ui.command_buttons:
                assert button.isVisible() and window.rect().contains(button.mapTo(window, button.rect().center())), name
            result['layouts'].append({'name': name, 'width': window.width(), 'height': window.height(),
                                     'body_font_pt': body_font, 'vertical_controls_scroll': ui.side.verticalScrollBar().maximum(),
                                     'image_area_height': ui.image_area.height(), 'data_area_height': ui.results_scroll.height(),
                                     'full_frame_visible': True})
        ui.zoom_combo.setCurrentIndex(0)
        window.resize(900, 960)
        pump()
        assert not ui.side.isVisible(), 'narrow window should prioritize image over settings'
        ui.settings_button.click()
        pump()
        assert ui.side.isVisible()
        ui.settings_button.click()
        window.image_plot.setRange(xRange=(100, 200), yRange=(100, 200), padding=0)
        ui.fit_button.click()
        pump()
        extent = window.image_plot.getViewBox().viewRect().adjusted(-.01, -.01, .01, .01)
        assert extent.contains(QtCore.QRectF(0, 0, image_width, image_height))
        result['cases'].append('full frame remains visible, compact data pane, settings toggle and fit button')
        result['status'] = 'passed'
    except BaseException:
        result['status'] = 'failed'
        result['error'] = traceback.format_exc()
        if window:
            window.grab().save(str(output / 'failure.png'))
        raise
    finally:
        (output / 'report.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        if window:
            window.close()
        sys.excepthook = old_hook
    print(json.dumps(result, ensure_ascii=False))
