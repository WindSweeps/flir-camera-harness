"""Typed MCP operations over the existing Qt application, not UI automation.

All device/widgets access stays on the Qt thread. Disk exports/hash work use at
most two workers, and retain only bounded job metadata. No frames in journals.
"""
import base64
import copy
import hashlib
import json
import os
import re
import tempfile
import threading
import time
import uuid
from collections import OrderedDict, deque
from pathlib import Path

import numpy as np
from PyQt5 import QtCore, QtGui, QtWidgets
import capture_metadata
import camera_modes
import continuous_recording as storage
import experiment_context
import monitor_ui
import parameter_log
import saturation_ui
from harness_core import ToolError, VERSION


def set_widget(widget, setter, value):
    with QtCore.QSignalBlocker(widget):
        getattr(widget, setter)(value)


def digest_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def write_pair_safely(path, overwrite, writer):
    """Windows rename fails if target exists, including a concurrent creator.

Stage in the destination filesystem, then commit both files. Existing-pair
replacement uses the already reviewed rollback implementation in the writer.
"""
    CameraHarness.check_destination(path, overwrite)
    if overwrite:
        return writer(path)
    folder = Path(tempfile.mkdtemp(prefix='.flir_mcp_', dir=str(path.parent))).resolve()
    assert folder.parent == path.parent.resolve()
    staged = folder / path.name
    committed = []
    try:
        result = writer(staged)
        for source, target in ((staged, path), (staged.with_suffix('.txt'), path.with_suffix('.txt'))):
            # This application is Windows-only: os.rename never replaces a target.
            os.rename(str(source), str(target))
            committed.append((target, target.stat().st_ino))
        return result
    except BaseException:
        for target, inode in reversed(committed):
            try:
                if target.stat().st_ino == inode:
                    target.unlink()
            except OSError:
                pass
        raise
    finally:
        for source in (staged, staged.with_suffix('.txt')):
            source.unlink(missing_ok=True)
        folder.rmdir()  # No recursive deletion or arbitrary directory cleanup.


class Job:
    def __init__(self, kind, function, finalize=None):
        self.kind, self.finalize = kind, finalize
        self.id = uuid.uuid4().hex
        self.started_utc = parameter_log.utc_now()
        self.done = threading.Event()
        self.result = self.error = None
        self.finalized = False
        def run():
            try:
                self.result = function()
            except ToolError as exc:
                self.error = exc.value()
            except Exception as exc:
                self.error = {'ok': False, 'code': 'OPERATION_FAILED', 'message': str(exc)}
            finally:
                self.done.set()
        self.thread = threading.Thread(target=run, name='FLIR-harness-' + kind, daemon=True)
        self.thread.start()


class CameraHarness(QtCore.QObject):
    def __init__(self, window, provider, output_root, replay=False):
        super().__init__(window)
        self.w, self.p, self.provider = window, window.roi_intensity_panel, provider
        self.ui = window._workflow
        self.output_root = Path(output_root).expanduser().resolve()
        self.replay, self.run_id = bool(replay), uuid.uuid4().hex
        self.jobs, self.records = OrderedDict(), OrderedDict()
        self.current_record = None
        self.events = deque(maxlen=32)
        self.cameras = []
        self.parameters_cache, self.parameters_at = None, float('-inf')
        self.ranges_cache = None
        self.closing = False
        self.path_jobs = set()
        self.ui.photos.set_root(self.output_root / 'photos')
        self.p._recording.root_override = self.output_root / 'cache'
        self.dialogs = {}
        # MCP has no user at a modal dialog. Information/errors enter status;
        # questions decline by default. Export approvals use explicit tool flags.
        for name in ('information', 'warning', 'critical', 'question'):
            self.dialogs[name] = getattr(QtWidgets.QMessageBox, name)
            def notice(parent, title, message, *args, _kind=name, **kwargs):
                self.events.append({'utc': parameter_log.utc_now(), 'kind': _kind,
                                    'title': str(title), 'message': str(message)[:4096]})
                return QtWidgets.QMessageBox.No if _kind == 'question' else QtWidgets.QMessageBox.Ok
            setattr(QtWidgets.QMessageBox, name, staticmethod(notice))
        window.installEventFilter(self)

    def eventFilter(self, watched, event):
        if watched is self.w and event.type() == QtCore.QEvent.Close and not self.closing:
            # Closing the shown window must not leave a deinitialized backend
            # behind a still-live MCP session. Stop/drain the whole owned session.
            self.begin_shutdown()
            self.w.hide()
            event.ignore()
            return True
        return super().eventFilter(watched, event)

    def call(self, name, args):
        self.poll_jobs()
        self.sync_gui_recording()
        if self.closing:
            raise ToolError('SHUTTING_DOWN', 'The client disconnected; pending writes are draining.')
        return getattr(self, 'do_' + name)(**args)

    def sync_gui_recording(self):
        rec = self.p._recording.recorder
        if rec is None:
            return
        found = next((k for k,v in self.records.items() if v.get('recorder') is rec), None)
        if found is None:
            found = self.add_record({'source': rec.path, 'recorder': rec, 'owned': True, 'exported': None})
        self.current_record = found

    def backend(self):
        if self.w.backend is None:
            raise ToolError('NO_CAMERA', 'Call flir_list_cameras and flir_open_camera first.')
        return self.w.backend

    def new_job(self, kind, fn, finalize=None):
        self.poll_jobs()
        if sum(not j.finalized for j in self.jobs.values()) >= 2:
            raise ToolError('BUSY', 'Two background operations are already running; poll their jobs first.')
        while len(self.jobs) >= 64:
            key = next((k for k,j in self.jobs.items() if j.finalized), None)
            if key is None:
                raise ToolError('BUSY', 'Job registry full')
            self.jobs.pop(key)
        job = Job(kind, fn, finalize)
        self.jobs[job.id] = job
        return {'job_id': job.id, 'status': 'running', 'done': False,
                'next': 'Poll flir_job_status until done=true; do not assume saving has completed.'}

    def poll_jobs(self):
        for job in list(self.jobs.values()):
            if job.done.is_set() and not job.finalized:
                try:
                    if job.finalize:
                        job.finalize(job)
                except Exception as exc:
                    job.error = {'ok': False, 'code': 'FINALIZE_FAILED', 'message': str(exc), 'result': job.result}
                job.finalized = True

    def do_job_status(self, job_id):
        job = self.jobs.get(job_id)
        if job is None:
            raise ToolError('UNKNOWN_JOB', 'Job not found in this process (last 64 jobs retained).')
        return {'job_id': job.id, 'kind': job.kind, 'started_utc': job.started_utc,
                'done': job.finalized, 'status': ('failed' if job.error else 'succeeded') if job.finalized else 'running',
                'result': job.result if job.finalized else None, 'error': job.error}

    def do_status(self):
        b = self.w.backend
        frame = self.p.frame
        stamp = getattr(self.p, '_saturation_frame_time', None)
        return {'harness_version': VERSION, 'run_id': self.run_id, 'source': 'replay_simulation' if self.replay else 'flir_hardware',
                'camera_id': self.w.camera_combo.currentData() if b else None,
                'opened': b is not None, 'streaming': bool(b and b.streaming),
                'ui_status': self.w.state_label.text(), 'window_visible': self.w.isVisible(),
                'frame_shape': list(frame.shape) if frame is not None else None,
                'frame_age_s': max(0, time.perf_counter() - stamp) if stamp is not None else None,
                'roi_bounds_xyxy': self.p.bounds, 'curve_points': len(self.p.samples),
                'peak_display': self.p.peak_label.text(),
                'trigger': copy.deepcopy(getattr(b, '_trigger_configuration', {'mode': 'continuous' if self.replay and b else None})),
                'saturation': copy.deepcopy(self.p._saturation_state),
                'saturation_history': copy.deepcopy(self.p._saturation_history),
                'recording': self.do_record_status(self.current_record) if self.current_record else None,
                'photos': self.do_photo_status(), 'monitor': self.monitor_status(),
                'jobs_running': sum(not j.finalized for j in self.jobs.values()),
                'recent_events': list(self.events), 'output_directory': str(self.output_root),
                'calibration': {'status': 'uncalibrated', 'physical_value': None}}

    def do_list_cameras(self):
        self.cameras = list(self.provider())
        if not self.w.backend:
            with QtCore.QSignalBlocker(self.w.camera_combo):
                self.w.camera_combo.clear()
                for serial, label in self.cameras:
                    self.w.camera_combo.addItem(str(label), str(serial))
        return {'source': 'replay_simulation' if self.replay else 'flir_hardware',
                'cameras': [{'camera_id': str(s), 'label': str(l)} for s,l in self.cameras]}

    def do_open_camera(self, camera_id):
        if self.w.backend:
            if self.w.camera_combo.currentData() == camera_id:
                return {'already_open': True, 'state': self.do_status()}
            raise ToolError('CAMERA_BUSY', 'Stop recording and close the current camera before selecting another.')
        self.do_list_cameras()
        index = self.w.camera_combo.findData(camera_id)
        if index < 0:
            raise ToolError('NOT_FOUND', 'Camera is not in the current enumeration.', {'camera_id': camera_id})
        self.w.camera_combo.setCurrentIndex(index)
        self.w._camera_changed()
        backend = None
        try:
            backend = self.w.backend_factory(camera_id)
            self.w.backend = backend
            low, high, step = backend.exposure_range()
            self.w.exposure_spin.setRange(low, high)
            self.w.exposure_spin.setSingleStep(step)
            low, high, step = backend.gain_range()
            self.w.gain_spin.setRange(low, high)
            self.w.gain_spin.setSingleStep(step)
            self.parameters_cache = None
            self.ranges_cache = None
            params = self.do_parameters(refresh=True)
            if self.replay:
                exposure, gain, automatic = backend.exposure, backend.gain_db, backend.automatic
            else:
                nodes = params['snapshot']['nodes']
                exposure = nodes.get('ExposureTime', {}).get('value')
                gain = nodes.get('Gain', {}).get('value')
                automatic = nodes.get('ExposureAuto', {}).get('value') == 'Continuous'
            if exposure is not None:
                set_widget(self.w.exposure_spin, 'setValue', float(exposure))
            if gain is not None:
                set_widget(self.w.gain_spin, 'setValue', float(gain))
            set_widget(self.w.auto_exposure_check, 'setChecked', bool(automatic))
            self.w.state_label.setText('MCP：设备已打开，等待开始采集')
            self.ui.auto_changed()
        except Exception:
            if backend:
                backend.close()
            self.w.backend = None
            raise
        return {'state': self.do_status(), 'parameters': params}

    def do_close_camera(self):
        c = self.p._recording
        if c.recorder and not c.recorder.done.is_set():
            raise ToolError('RECORDING_ACTIVE', 'Stop recording and wait for its cache to drain first.')
        if self.w.backend:
            self.w._stop_acquisition()
            self.w._camera_changed()
        self.parameters_cache = None
        return {'state': self.do_status()}

    def do_start_capture(self):
        b = self.backend()
        if not b.streaming:
            self.w._toggle_acquisition()
        if not self.w.backend or not self.w.backend.streaming:
            raise ToolError('START_FAILED', self.w.state_label.text())
        return {'state': self.do_status()}

    def do_stop_capture(self):
        if self.w.backend:
            self.w._stop_acquisition()
        return {'state': self.do_status()}

    def do_parameters(self, refresh=False):
        b = self.backend()
        now = time.monotonic()
        if self.replay:
            snapshot = {'source': 'replay_simulation', 'ExposureTime_us': b.exposure,
                        'Gain_db': b.gain_db, 'automatic': b.automatic, 'frame_associated': False}
            ranges = {'exposure_us': list(b.exposure_range()), 'gain_db': list(b.gain_range())}
        else:
            if self.parameters_cache is None or (refresh and now - self.parameters_at >= 2):
                self.parameters_cache = capture_metadata.read_parameter_snapshot(b)
                self.parameters_at = now
                self.ranges_cache = {'exposure_us': list(b.exposure_range()), 'gain_db': list(b.gain_range())}
            snapshot = copy.deepcopy(self.parameters_cache)
            ranges = self.ranges_cache
        return {'snapshot': snapshot, 'ranges_min_max_step': ranges,
                'snapshot_age_s': 0 if self.replay else max(0, now - self.parameters_at),
                'features': {'external_trigger': not self.replay, 'auto_exposure_and_gain': True,
                             'hardware_binning_setter': False, 'hardware_roi_setter': False,
                             'raw_photo_autosave': not self.replay, 'physical_conversion': False},
                'note': 'Unsupported/read-only nodes are reported by the device. Binning/hardware ROI are recorded, not writable through this harness.'}

    def changed(self):
        self.parameters_cache = None
        self.p._saturation_manual_key = None
        saturation_ui.manual_check(self.p)
        b = self.w.backend
        if b is not None:
            try:
                automatic = b.automatic if self.replay else (
                    camera_modes.read(b, 'ExposureAuto') == 'Continuous' and camera_modes.read(b, 'GainAuto') == 'Continuous')
                set_widget(self.w.auto_exposure_check, 'setChecked', automatic)
            except Exception:
                pass  # Parameter tool exposes unavailable/unreadable nodes.
        self.ui.auto_changed()

    def manual(self, key, value, range_name, setter, widget):
        b = self.backend()
        low, high, step = getattr(b, range_name)()
        if not low <= value <= high:
            raise ToolError('OUT_OF_RANGE', 'Value is outside the device range.', {'minimum': low, 'maximum': high, 'step': step})
        try:
            if self.replay:
                b.set_exposure_auto(False)
            actual = getattr(b, setter)(value)
            set_widget(self.w.auto_exposure_check, 'setChecked', False)
            set_widget(widget, 'setValue', float(actual))
            if key == 'gain_db':
                self.w._gain_requested = actual
            return {'requested': value, key: actual, 'source': 'replay_simulation' if self.replay else 'device_readback'}
        finally:
            self.changed()

    def do_set_exposure(self, exposure_us):
        return self.manual('exposure_us', exposure_us, 'exposure_range', 'set_exposure_us', self.w.exposure_spin)

    def do_set_gain(self, gain_db):
        return self.manual('gain_db', gain_db, 'gain_range', 'set_gain_db', self.w.gain_spin)

    def do_set_auto_brightness(self, enabled, gain_upper_db=None):
        b = self.backend()
        previous = getattr(b, '_auto_gain_upper_requested', None)
        if gain_upper_db is not None:
            if self.replay:
                raise ToolError('UNSUPPORTED', 'Gain upper limit is a hardware feature; replay cannot validate it.')
            low, high, actual = camera_modes.gain_limit_range(b)
            if not low <= gain_upper_db <= high:
                raise ToolError('OUT_OF_RANGE', 'Auto gain limit outside device range.', {'minimum': low, 'maximum': high})
            b._auto_gain_upper_requested = gain_upper_db
        try:
            actual = b.set_exposure_auto(enabled)
        except Exception:
            b._auto_gain_upper_requested = previous
            raise
        finally:
            self.changed()
        set_widget(self.w.auto_exposure_check, 'setChecked', enabled)
        if gain_upper_db is not None:
            set_widget(self.ui.gain_limit, 'setValue', gain_upper_db)
        self.ui.auto_changed()
        return {'enabled': enabled, 'readback': actual,
                'source': 'replay_simulation' if self.replay else 'device_readback'}

    def do_set_trigger(self, mode, delay_us=9):
        b = self.backend()
        if self.replay:
            if mode == 'external':
                raise ToolError('UNSUPPORTED', 'Replay has no external trigger. Use hardware for Line0.')
            return {'mode': 'continuous', 'source': 'replay_simulation'}
        success = False
        try:
            self.ui.photos.prepare_mode(b, mode)
            actual = b.set_trigger_mode(mode, delay_us)
            success = True
            actual_delay = actual['readback'].get('TriggerDelay', delay_us)
            set_widget(self.ui.mode, 'setCurrentIndex', 1 if mode == 'external' else 0)
            set_widget(self.ui.delay, 'setValue', actual_delay)
            self.ui.applied = (mode, actual_delay)
            return actual
        finally:
            self.ui.photos.finish_mode(b, success)
            self.changed()

    def do_set_roi(self, x, y, width, height):
        if self.p.frame is None:
            raise ToolError('NO_FRAME', 'Start capture and wait for a frame before selecting ROI.')
        h, w = self.p.frame.shape[:2]
        if x + width > w or y + height > h:
            raise ToolError('OUT_OF_RANGE', 'ROI extends beyond the full image.', {'image_width': w, 'image_height': h})
        with QtCore.QSignalBlocker(self.p.roi):
            self.p.roi.setPos((x, y))
            self.p.roi.setSize((width, height))
            self.p.roi.show()
        self.p._region_changed()
        return {'roi_bounds_xyxy': self.p.bounds, 'curve_reset': True,
                'saturation': copy.deepcopy(self.p._saturation_state)}

    def do_clear_curve(self):
        self.p.reset_trace()
        return {'cleared': True, 'continuous_recording_preserved': True}

    def do_clear_peak(self):
        self.p.reset_peak()
        return {'cleared': True}

    def do_get_curve(self, max_points=300):
        # Original rolling deque is already bounded to the display window.
        points = list(self.p.samples)
        total = len(points)
        if total > max_points:
            indices = np.linspace(0, total-1, max_points, dtype=int)
            points = [points[int(i)] for i in indices]
        return {'columns': ['elapsed_s', 'sum_display_dn'], 'points': points,
                'available_points': total, 'returned_points': len(points), 'downsampled': total > len(points),
                'roi_bounds_xyxy': self.p.bounds, 'physical_value': None, 'calibration_status': 'uncalibrated'}

    def do_preview(self, max_width=960):
        frame = self.p.frame
        if frame is None:
            raise ToolError('NO_FRAME', 'No frame has been received.')
        # Bound work before encoding, preserve complete field of view, no raw cache.
        h, w = frame.shape[:2]
        stride = max(1, int(np.ceil(max(w / max_width, h / 960))))
        pixels = np.ascontiguousarray(frame[::stride, ::stride], dtype=np.uint8)
        ph, pw = pixels.shape[:2]
        img = QtGui.QImage(pixels.data, pw, ph, pixels.strides[0], QtGui.QImage.Format_Grayscale8)
        data = QtCore.QByteArray()
        buffer = QtCore.QBuffer(data)
        buffer.open(QtCore.QIODevice.WriteOnly)
        if not img.save(buffer, 'JPEG', 80):
            raise ToolError('ENCODE_FAILED', 'Could not encode preview')
        stamp = getattr(self.p, '_saturation_frame_time', None)
        return {'image_width': w, 'image_height': h, 'preview_width': pw, 'preview_height': ph,
                'roi_bounds_xyxy': self.p.bounds, 'age_s': max(0, time.perf_counter()-stamp) if stamp is not None else None,
                'source': 'replay_simulation' if self.replay else 'display_preview_not_raw',
                '_content': [{'type': 'image', 'mimeType': 'image/jpeg', 'data': base64.b64encode(bytes(data)).decode('ascii')}]}

    def path(self, value):
        p = Path(value).expanduser()
        if not p.is_absolute():
            p = self.output_root / p
        return p.resolve()

    def export_path(self, value):
        p = self.path(value)
        if p.suffix.lower() != '.csv':
            raise ToolError('INVALID_ARGUMENT', 'Export path must end in .csv')
        if str(p).casefold() in self.path_jobs:
            raise ToolError('BUSY', 'An export to this destination is already running.')
        return p

    @staticmethod
    def check_destination(p, overwrite):
        if not overwrite and (p.exists() or p.with_suffix('.txt').exists()):
            raise ToolError('FILE_EXISTS', 'CSV or parameter TXT exists; choose a new name or explicitly set overwrite=true.', {'csv': str(p)})
        p.parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def check_saturation(history, allowed):
        if history.get('detected') and not allowed:
            raise ToolError('SATURATION_WARNING', 'Overexposure was detected in this workspace/recording. Ask the user whether to save before retrying allow_saturated=true.', {'saturation_history': history})

    def export_job(self, kind, path, fn, finalize=None):
        key = str(path).casefold()
        def finished(job):
            self.path_jobs.discard(key)
            if finalize:
                finalize(job)
        result = self.new_job(kind, fn, finished)
        self.path_jobs.add(key)
        return result

    def do_export_curve(self, path, overwrite=False, allow_saturated=False):
        snapshot = parameter_log.freeze_export(self.p, capture_metadata)
        if not snapshot['samples'] or not snapshot['bounds']:
            raise ToolError('NO_DATA', 'No selected-ROI curve points to export.')
        self.check_saturation(snapshot['saturation_history'], allow_saturated)
        p = self.export_path(path)
        def export():
            count = write_pair_safely(p, overwrite, lambda target: parameter_log.write_export_bundle(target, **snapshot))
            return {'csv': str(p), 'txt': str(p.with_suffix('.txt')), 'sample_count': count,
                    'csv_sha256': digest_file(p), 'calibration_status': 'uncalibrated'}
        return self.export_job('curve_export', p, export)

    def record(self, record_id):
        if record_id not in self.records:
            raise ToolError('UNKNOWN_RECORD', 'Unknown record_id. Use status or recover a saved cache path.')
        return self.records[record_id]

    def add_record(self, item):
        while len(self.records) >= 32:
            removable = next((k for k,v in self.records.items() if k != self.current_record and not v.get('exporting') and
                              (v.get('recorder') is None or v['recorder'].done.is_set())), None)
            if removable is None:
                raise ToolError('BUSY', 'Record registry is full')
            self.records.pop(removable)
        rid = uuid.uuid4().hex
        self.records[rid] = item
        return rid

    def do_record_start(self):
        if not self.backend().streaming:
            raise ToolError('NOT_STREAMING', 'Start acquisition before starting recording.')
        if self.p.frame is None or self.p.bounds is None:
            raise ToolError('NO_ROI', 'Get a frame and select a valid ROI first.')
        c = self.p._recording
        if c.recorder and c.recorder.accepting:
            raise ToolError('ALREADY_RECORDING', 'Recording already running.', {'record_id': self.current_record})
        if not c.start():
            raise ToolError('START_FAILED', 'Recording could not start. Wait for prior work to drain.', {'events': list(self.events)})
        rid = self.add_record({'source': c.recorder.path, 'recorder': c.recorder, 'owned': True, 'exported': None})
        self.current_record = rid
        return self.do_record_status(rid)

    def do_record_stop(self, record_id):
        item = self.record(record_id)
        rec = item.get('recorder')
        if rec and rec.accepting:
            if rec is self.p._recording.recorder:
                self.p._recording.stop(auto_save=False)
            else:
                rec.stop(context=self.p._experiment_context)
        return self.do_record_status(record_id)

    def do_record_status(self, record_id):
        item = self.record(record_id)
        rec = item.get('recorder')
        c = self.p._recording
        if c.source == item['source'] and c.job is not None and c.job_kind == 'inspect' and c.job.done.is_set():
            c.poll()
        state = rec.status() if rec else {'accepting': False, 'done': True, **item.get('report', {})}
        history = rec.saturation_history if rec else item.get('report', {}).get('saturation_history', {})
        inspecting = c.source == item['source'] and c.job is not None
        return {'record_id': record_id, **state, 'cache_path': str(item['source']),
                'saturation_history': copy.deepcopy(history), 'exporting': bool(item.get('exporting')),
                'inspection_pending': inspecting,
                'export_ready': bool(state.get('done') and not inspecting and not item.get('exporting') and not item.get('cache_removed')),
                'exported': item.get('exported'), 'cache_removed_after_export': item.get('cache_removed', False)}

    def do_record_export(self, record_id, path, overwrite=False, allow_saturated=False, allow_incomplete=False):
        item = self.record(record_id)
        rec = item.get('recorder')
        if rec and not rec.done.is_set():
            raise ToolError('RECORDING_ACTIVE', 'Stop the record and wait until done=true before export.')
        if item.get('exporting'):
            raise ToolError('BUSY', 'This record is already exporting.')
        if item.get('cache_removed'):
            raise ToolError('ALREADY_EXPORTED', 'Cache was removed after successful export.', item.get('exported'))
        c = self.p._recording
        if c.source == item['source'] and c.job is not None and c.job_kind == 'inspect' and c.job.done.is_set():
            c.poll()
        if c.source == item['source'] and c.job is not None:
            raise ToolError('BUSY', 'Record inspection is still running; retry after polling status.')
        if rec:
            self.check_saturation(rec.saturation_history, allow_saturated)
        p = self.export_path(path)
        source = item['source']
        def export():
            report = storage.inspect_journal(source)
            self.check_saturation(report['saturation_history'], allow_saturated)
            if (not report['complete'] or (report.get('end') or {}).get('error')) and not allow_incomplete:
                raise ToolError('INCOMPLETE_RECORDING', 'This cache ended abnormally or is incomplete. Explain missing-data risk before retrying allow_incomplete=true.', report)
            result = write_pair_safely(p, overwrite, lambda target: storage.export_journal(source, target, report['source_signature']))
            result.update(csv=str(p), txt=str(p.with_suffix('.txt')))
            if item.get('owned'):
                try:
                    storage.remove_saved_cache(source)
                    result['cache_removed'] = True
                except OSError as exc:
                    result['cache_removed'] = False
                    result['cache_cleanup_error'] = str(exc)
            else:
                result['cache_removed'] = False
            return result
        def finished(job):
            item['exporting'] = False
            if not job.error:
                item['exported'] = job.result
                item['cache_removed'] = job.result['cache_removed']
                if c.source == source:
                    c.source, c.report = None, None
                    c.handled_stop = True
                    c.save_button.setEnabled(False)
                    c.label.setText('MCP 已保存记录：' + str(p))
        result = self.export_job('record_export', p, export, finished)
        item['exporting'] = True
        if c.source == source:
            # A record can drain before the UI's 500 ms inspection timer fires.
            # Prevent that timer from re-inspecting a cache we are about to remove.
            c.handled_stop = True
            c.auto_save = False
            c.save_button.setEnabled(False)
        return result

    def do_record_recover(self, cache_path):
        path = self.path(cache_path)
        if path.name != 'session.jsonl' or not path.is_file():
            raise ToolError('INVALID_CACHE', 'Select an existing session.jsonl record cache.')
        if any(v['source'] == path and v.get('recorder') and not v['recorder'].done.is_set() for v in self.records.values()):
            raise ToolError('RECORDING_ACTIVE', 'Cannot recover a cache while it is still writing.')
        def finished(job):
            if not job.error:
                rid = self.add_record({'source': path, 'owned': False, 'report': job.result})
                job.result = {'record_id': rid, 'cache_path': str(path), **job.result}
        return self.new_job('record_recover', lambda: storage.inspect_journal(path), finished)

    def do_photo_configure(self, enabled, name, directory):
        photos = self.ui.photos
        b = self.w.backend
        if (b and b.streaming) or any(not s.done.is_set() for s in photos.sessions):
            raise ToolError('BUSY', 'Stop acquisition and wait for pending photo writes before configuring the next batch.')
        if not name.strip() or name != name.strip() or re.search(r'[<>:"/\\|?*\x00-\x1f]', name) or name.endswith('.'):
            raise ToolError('INVALID_ARGUMENT', 'Photo name contains invalid Windows filename characters or trailing spaces/dot.')
        target = self.path(directory)
        if b and not self.replay:
            previous = getattr(b, '_photo_autosave_enabled', False)
            b._photo_autosave_enabled = enabled
            try:
                config = getattr(b, '_trigger_configuration', {})
                if config.get('mode'):
                    b.set_trigger_mode(config['mode'], config.get('readback', {}).get('TriggerDelay', 9))
            except Exception:
                b._photo_autosave_enabled = previous
                raise
        set_widget(photos.enabled, 'setChecked', enabled)
        photos.name.setText(name)
        photos.set_root(target)
        photos.poll()
        return self.do_photo_status()

    def do_photo_status(self):
        photos = self.ui.photos
        return {'enabled': photos.enabled.isChecked(), 'name': photos.name.text(), 'directory': str(photos.root),
                'filename_pattern': 'YYYYMMDD-HHMMSS-mmm_name_000001.tif',
                'timestamp_meaning': 'host image receive time, UTC+08:00; not sensor exposure start',
                'last_batch': photos.last.status() if photos.last else None,
                'pending_batches': sum(not s.done.is_set() for s in photos.sessions),
                'frame_queue_limit': 8, 'byte_queue_limit': 64*1024*1024}

    def do_context_get(self):
        return {'context': copy.deepcopy(self.p._experiment_context)}

    def do_context_set(self, values):
        if any(j.kind == 'context_update' and not j.finalized for j in self.jobs.values()):
            raise ToolError('BUSY', 'A context update is already running.')
        draft = copy.deepcopy(self.p._experiment_context)
        draft.update(values)
        def prepare():
            for prefix in ('dark_reference', 'flat_reference'):
                identity = experiment_context._reference_hash(draft.get(prefix + '_path'))
                draft.update({prefix + '_' + k: v for k,v in identity.items()})
            draft.update(source='user_entered_not_verified', calibration_status='uncalibrated',
                         dark_correction_applied=False, flat_correction_applied=False)
            return draft
        def finished(job):
            if not job.error:
                job.result['updated_utc'] = parameter_log.utc_now()
                self.p._experiment_context = job.result
        return self.new_job('context_update', prepare, finished)

    def do_monitor_addresses(self):
        return {'addresses': monitor_ui.lan_addresses()}

    def monitor_status(self):
        service = self.ui.monitor.service
        address = service.http.server_address if service else (None, None)
        return {'enabled': service is not None,
                'host': address[0], 'port': address[1],
                'read_only': True}

    def do_monitor_start(self, host, port=8765):
        service = self.ui.monitor.service
        if service and service.http.server_address[:2] != (host, port):
            raise ToolError('BUSY', 'Monitor already uses another address; stop it before changing address.')
        self.ui.monitor.start(host, port)
        service = self.ui.monitor.service
        return {**self.monitor_status(), 'access_link': service.link,
                'note': 'Private read-only link; client phone must reach this LAN address. Treat the fragment token as a secret.'}

    def do_monitor_stop(self):
        self.ui.monitor.stop()
        return self.monitor_status()

    def do_show_window(self, visible):
        if visible:
            self.w.show()
            self.w.raise_()
        else:
            self.w.hide()
        return {'visible': self.w.isVisible()}

    def begin_shutdown(self):
        if self.closing:
            return
        self.closing = True
        self.ui.monitor.stop()
        self.p._recording.stop(auto_save=False)
        self.p._recording.auto_save = False
        try:
            self.w._stop_acquisition()
        except Exception as exc:
            self.events.append({'kind': 'shutdown_error', 'message': str(exc)})
        for session in self.ui.photos.sessions:
            session.stop()

    def shutdown_ready(self):
        self.poll_jobs()
        c = self.p._recording
        return (all(j.finalized for j in self.jobs.values()) and
                (not c.recorder or c.recorder.done.is_set()) and
                (not c.job or c.job.done.is_set()) and
                all(s.done.is_set() for s in self.ui.photos.sessions))

    def finish_shutdown(self):
        if self.w.backend:
            self.w.backend.close()
            self.w.backend = None
        for name, original in self.dialogs.items():
            setattr(QtWidgets.QMessageBox, name, original)
