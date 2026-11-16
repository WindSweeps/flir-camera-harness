"""Native shipped runtime + original replay/ROI + real loopback HTTP tests."""


def run_monitor_tests(namespace, demo=False):
    import base64
    import http.client
    import json
    import marshal
    import sys
    import time
    import traceback
    import types
    from pathlib import Path
    from PyQt5 import QtCore, QtGui, QtWidgets
    import monitor_ui
    output = Path.cwd() / '_monitor_test_output'
    output.mkdir(exist_ok=True)
    original = types.ModuleType('_monitor_original_main')
    original.__file__ = str(Path(sys.executable).parent / 'main.py')
    exec(marshal.loads(namespace['_original_main_code']), original.__dict__)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    errors = []
    old_hook = sys.excepthook
    sys.excepthook = lambda typ, value, tb: errors.append(''.join(traceback.format_exception(typ, value, tb)))
    window = None
    results = {'status': 'running', 'hardware_access': False, 'cases': []}

    def pump(seconds=.1):
        end = time.perf_counter() + seconds
        while time.perf_counter() < end:
            app.processEvents()
            time.sleep(.005)
        assert not errors, '\n'.join(errors)

    def request(service, path='/api/state?client=testphone123', token=None, method='GET', headers=None):
        conn = http.client.HTTPConnection(*service.http.server_address[:2], timeout=3)
        try:
            options = dict(headers or {})
            if token is not None:
                options['Authorization'] = 'Bearer ' + token
            conn.request(method, path, headers=options)
            response = conn.getresponse()
            return response.status, response.read(), dict(response.getheaders())
        finally:
            conn.close()
    try:
        results['lan_addresses'] = monitor_ui.lan_addresses()
        sample = Path(sys._MEIPASS) / 'sample' / 'camera_frame.png'
        window = original.RoiCameraWindow(backend_factory=lambda serial: original.ImageReplayBackend(Path(serial)),
                                         camera_provider=lambda: [(str(sample), '图片回放 · 手机监控验证')])
        window.show()
        pump()
        panel, control = window.roi_intensity_panel, window._workflow.monitor
        assert control.service is None and not control.timer.isActive()
        window._workflow.photos.set_root(output / 'photos')
        panel._recording.root_override = output / 'record_cache'
        window.start_button.click()
        pump(.3)
        assert panel.frame is not None
        panel.roi.setPos((100, 100))
        panel.roi.setSize((180, 140))
        panel.roi.show()
        panel._region_changed()
        pump(.3)
        control.start('127.0.0.1', 0)
        service, worker = control.service, control.worker
        pump(.7)
        assert worker.encoded == 0, 'No viewer must cause no encoding'
        results['cases'].append('monitor defaults off; zero JPEG work without authenticated viewers')
        assert request(service)[0] == 401
        assert request(service, token='wrong')[0] == 401
        assert request(service, '/../../Windows/win.ini', token=service.token)[0] == 404
        assert request(service, token=service.token, method='POST')[0] == 405
        assert request(service, token=service.token, headers={'Origin': 'http://evil.invalid'})[0] == 403
        assert request(service, token=service.token, headers={'Host': 'evil.invalid'})[0] == 403
        for path in ('/', '/app.js', '/app.css'):
            status, body, headers = request(service, path)
            assert status == 200 and body and 'Content-Security-Policy' in headers
        assert worker.encoded == 0
        results['cases'].append('authentication, origin/host checks, fixed assets, no files or remote writes')
        code, body, headers = request(service, token=service.token)
        payload = json.loads(body)
        assert code == 200 and payload['roi'] == list(panel.bounds)
        assert payload['intensity'] == panel.samples[-1][1] and payload['frame'] is None
        pump(.8)
        data = json.loads(request(service, token=service.token)[1])
        assert data['frame'] and data['frame']['original_width'] == panel.frame.shape[1]
        encoded = data['frame']['data_url'].split(',', 1)[1]
        image = QtGui.QImage.fromData(base64.b64decode(encoded), 'JPEG')
        assert not image.isNull() and image.width() <= 960 and image.height() <= 720
        image.save(str(output / 'preview.png'))
        assert len(data['curve']) <= 181
        assert not any(s in body.decode() for s in ('session.jsonl', 'image_data', 'nodes', 'C:\\'))
        results['cases'].append('actual replay ROI and parameters served; bounded whole-image JPEG and curve')
        start_count = worker.encoded
        pump(1.2)
        assert 1 <= worker.encoded - start_count <= 3
        assert worker.queue.maxsize == 1
        assert control.last_tick_ms < 100
        results['tick_ms'] = control.last_tick_ms
        results['cases'].append('2fps timer, one pending thumbnail, responsive UI, no extra camera queries')
        panel._saturation_history['detected'] = True
        control.tick()
        assert json.loads(request(service, token=service.token)[1])['saturation']['ever']
        control.show_dialog()
        assert control.dialog.grab().save(str(output / 'connection_dialog.png'))
        qr = monitor_ui.qr_image(service.link)
        qr.save(str(output / 'qr.png'))
        assert window.grab().save(str(output / 'native_desktop.png'))
        control.dialog.hide()
        if demo:
            (output / 'connection.json').write_text(json.dumps({'url': service.link, 'pid': __import__('os').getpid()}), encoding='utf-8')
            stop_file = output / 'stop_demo'
            stop_file.unlink(missing_ok=True)
            deadline = time.monotonic() + 240
            while time.monotonic() < deadline and not stop_file.exists():
                pump(.1)
        window.start_button.click()
        pump(.6)
        data = json.loads(request(service, token=service.token)[1])
        assert not data['acquiring'] and data['frame']
        version = data['frame']['version']
        unchanged = json.loads(request(service, '/api/state?client=testphone123&since=' + str(version), service.token)[1])
        assert 'data_url' not in unchanged['frame'] and unchanged['frame']['age_s'] > 0
        results['cases'].append('stopped/trigger-wait image keeps its age; unchanged JPEG is not retransmitted')
        first_token = service.token
        control.stop()
        assert service.stopped.wait(2) and worker.done.wait(2)
        assert service.preview is None and worker.queue.empty()
        control.start('127.0.0.1', 0)
        second = control.service
        assert second.token != first_token and request(second, token=first_token)[0] == 401
        window.close()
        pump(.4)
        assert second.stopped.is_set() and control.service is None
        results['cases'].append('stop clears buffers, close stops listener and re-enable rotates access link')
        results['status'] = 'passed'
    except BaseException:
        results['status'] = 'failed'
        results['error'] = traceback.format_exc()
        raise
    finally:
        if window:
            window._workflow.monitor.stop()
            window.close()
            pump(.3)
        sys.excepthook = old_hook
        (output / 'report.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(results))
