"""Qt controls for continuous disk-backed measurement recording."""
import time
from datetime import datetime
from pathlib import Path

from PyQt5 import QtCore, QtWidgets
import capture_metadata
import continuous_recording as storage


class RecordingController(QtCore.QObject):
    def __init__(self, panel):
        super().__init__(panel)
        self.panel = panel
        self.recorder = None
        self.source = None
        self.report = None
        self.job = None
        self.job_kind = None
        self.handled_stop = False
        self.auto_save = False
        self.in_dialog = False
        self.close_requested = False
        self.event_cursor = 0
        self.last_event_backend = None
        self.error_shown = False
        self.window = None
        self.root_override = None  # Tests may redirect cache files to their workspace.

        self.button = QtWidgets.QPushButton('开始记录', panel)
        self.button.setObjectName('startStopRecordingButton')
        self.button.setToolTip('从点击时起持续记录选区光强和参数，直到停止；不限60秒。')
        self.button.clicked.connect(self.toggle)
        self.save_button = QtWidgets.QPushButton('保存记录…', panel)
        self.save_button.setObjectName('saveRecordingButton')
        self.save_button.setEnabled(False)
        self.save_button.clicked.connect(self.save)
        self.recover_button = QtWidgets.QPushButton('恢复记录缓存…', panel)
        self.recover_button.setObjectName('recoverRecordingButton')
        self.recover_button.clicked.connect(self.recover)
        self.label = QtWidgets.QLabel('连续记录：未开始（界面曲线仍显示最近60秒）', panel)
        self.label.setObjectName('continuousRecordingStatus')
        self.label.setWordWrap(True)
        self.label.setStyleSheet('QLabel { padding: 4px; color: #374151; }')
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self.button)
        row.addWidget(self.save_button)
        row.addWidget(self.recover_button)
        row.addWidget(self.label, 1)
        panel.layout().insertLayout(3, row)
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self.poll)
        self.timer.start()
        QtCore.QTimer.singleShot(0, self.install_window_filter)

    def install_window_filter(self):
        if self.window is not None:
            self.window.removeEventFilter(self)
        self.window = self.panel.window()
        self.window.installEventFilter(self)

    def eventFilter(self, obj, event):
        if obj is self.window and event.type() == QtCore.QEvent.Close:
            busy = self.job is not None or (self.recorder is not None and not self.recorder.done.is_set())
            if busy:
                self.close_requested = True
                self.stop(auto_save=False)
                self.label.setText('正在结束记录并写入缓存，完成后关闭；未导出缓存可在下次恢复。')
                event.ignore()
                return True
        return super().eventFilter(obj, event)

    def toggle(self):
        if self.recorder is not None and self.recorder.accepting:
            self.stop(auto_save=True)
        else:
            self.start()

    def start(self):
        if self.job is not None or (self.recorder is not None and not self.recorder.done.is_set()):
            return False
        if self.panel.frame is None or self.panel.bounds is None:
            QtWidgets.QMessageBox.information(self.panel, '请先框选', '请先取得图像并框选统计区域，再开始连续记录。')
            return False
        try:
            recorder = storage.Recorder(root=self.root_override)
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self.panel, '无法开始记录', '无法创建记录缓存：\n' + str(exc))
            return False
        # Earlier unsaved sessions stay on disk and can be recovered separately.
        self.recorder = recorder
        self.source = recorder.path
        self.report = None
        self.event_cursor = 0
        self.last_event_backend = getattr(self.panel.window(), 'backend', None)
        self.handled_stop = self.error_shown = self.auto_save = False
        self.button.setText('停止记录')
        self.button.setEnabled(True)
        self.save_button.setEnabled(False)
        self.recover_button.setEnabled(False)
        self.label.setText('正在连续记录 · 0 点 · 后台缓存写入中')
        self.label.setToolTip(str(self.source))
        # Recheck current selection without resetting the display curve/history.
        import saturation_ui
        self.panel._saturation_manual_key = None
        saturation_ui.manual_check(self.panel)
        self.poll_events()
        return True

    def append(self, record):
        if self.recorder is not None:
            self.recorder.append(record)

    def saturation(self, state):
        if self.recorder is not None:
            self.recorder.note_saturation(state)

    def marker(self, action):
        if self.recorder is not None:
            self.poll_events()
            self.recorder.marker(action, self.panel.bounds)

    def poll_events(self):
        if self.recorder is None or not self.recorder.accepting:
            return
        backend = getattr(self.panel.window(), 'backend', None)
        try:
            backends = [self.last_event_backend] if self.last_event_backend is not None and self.last_event_backend is not backend else []
            if backend is not None:
                backends.append(backend)
            for current in backends:
                events = capture_metadata.get_parameter_events(self.event_cursor, backend=current)
                for event in events:
                    self.event_cursor = max(self.event_cursor, event.get('id', 0))
                    if event.get('host_monotonic_s', 0) >= self.recorder.started:
                        self.recorder.event(event)
            self.last_event_backend = backend
        except Exception as exc:
            # Report a gap rather than silently inventing parameter history.
            self.recorder._offer('event_read_error', {'utc': storage.utc_now(), 'error': str(exc)})

    def stop(self, auto_save=True):
        if self.recorder is None or self.recorder.done.is_set():
            return
        if self.recorder.accepting:
            self.poll_events()
            self.recorder.stop(context=getattr(self.panel, '_experiment_context', {}))
        self.auto_save = auto_save
        self.button.setEnabled(False)
        self.button.setText('正在停止…')
        self.label.setText('停止接收新数据，正在写完缓存…')

    def poll(self):
        if self.in_dialog:
            return
        if self.job is not None:
            if not self.job.done.is_set():
                return
            job, kind = self.job, self.job_kind
            self.job = self.job_kind = None
            if job.error:
                self.report = None
                self.label.setText('操作失败，缓存保留，可重试：' + job.error)
                self.save_button.setEnabled(self.source is not None)
                self.recover_button.setEnabled(True)
                self.button.setEnabled(True)
                if not self.close_requested:
                    QtWidgets.QMessageBox.warning(self.panel, '记录缓存已保留', job.error + '\n\n' + str(self.source))
            elif kind == 'inspect':
                self.report = job.result
                self.label.setText('记录已停止 · {} 点 · 缓存已保留'.format(self.report['sample_count']))
                self.save_button.setEnabled(self.report['sample_count'] > 0)
                self.recover_button.setEnabled(True)
                self.button.setEnabled(True)
                if self.auto_save and not self.close_requested:
                    self.auto_save = False
                    self.save()
            elif kind == 'export':
                self.label.setText('已保存 {} 点：{}'.format(job.result['sample_count'], job.result['csv']))
                self.save_button.setEnabled(False)
                self.recover_button.setEnabled(True)
                self.button.setEnabled(True)
                try:
                    if self.recorder is not None and self.recorder.done.is_set() and self.source == self.recorder.path:
                        storage.remove_saved_cache(self.source)
                except OSError:
                    pass  # Export succeeded; a harmless duplicate cache may remain.
                if not self.close_requested:
                    QtWidgets.QMessageBox.information(self.panel, '连续记录已保存',
                        '已保存 {} 个数据点。\n\n{}\n{}'.format(job.result['sample_count'], job.result['csv'], job.result['txt']))
                self.source = None
                self.report = None
            if self.close_requested and self.job is None:
                self.window.close()
            return
        if self.recorder is None:
            return
        state = self.recorder.status()
        if self.recorder.accepting:
            self.poll_events()
            duration = max(0, time.perf_counter() - self.recorder.started)
            self.label.setText('记录中 {:02d}:{:02d}:{:02d} · {} 点 · 已落盘 {} 点'.format(
                int(duration // 3600), int(duration % 3600 // 60), int(duration % 60),
                state['submitted'], state['flushed']))
            return
        if not state['done']:
            self.button.setEnabled(False)
            self.label.setText('正在写完缓存…' if not state['error'] else state['error'] + ' 正在保留已有数据…')
            return
        if not self.handled_stop:
            self.handled_stop = True
            self.button.setText('开始记录')
            if state['error'] and not self.error_shown and not self.close_requested:
                self.error_shown = True
                self.auto_save = False
                QtWidgets.QMessageBox.warning(self.panel, '连续记录已提前停止',
                    state['error'] + '\n已提交 {} 点，写入 {} 点。缓存保留于：\n{}'.format(
                        state['submitted'], state['written'], self.source))
            self._inspect(auto_save=self.auto_save)

    def _inspect(self, auto_save=False):
        self.auto_save = auto_save
        self.job_kind = 'inspect'
        self.job = storage.BackgroundJob(storage.inspect_journal, self.source)
        self.button.setEnabled(False)
        self.save_button.setEnabled(False)
        self.recover_button.setEnabled(False)
        self.label.setText('正在核对记录缓存…')

    def save(self):
        if self.source is None or self.job is not None or self.in_dialog:
            return False
        if self.recorder is not None and not self.recorder.done.is_set():
            return False
        if self.report is None:
            self._inspect(auto_save=True)
            return False
        if not self.report['sample_count']:
            return False
        self.in_dialog = True
        try:
            if self.report['saturation_history'].get('detected'):
                answer = QtWidgets.QMessageBox.question(self.panel, '连续记录过曝保存确认',
                    '这次连续记录中检测到过曝，相关光强可能失真。\n是否仍然保存全部记录？\n选择“否”会保留缓存。',
                    QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No, QtWidgets.QMessageBox.No)
                if answer != QtWidgets.QMessageBox.Yes:
                    return False
            if not self.report['complete'] or (self.report.get('end') or {}).get('error'):
                answer = QtWidgets.QMessageBox.question(self.panel, '恢复未完整结束的记录',
                    '此记录曾中断或提前停止，将保存可恢复的 {} 个完整数据点。\n是否继续？'.format(self.report['sample_count']),
                    QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No, QtWidgets.QMessageBox.No)
                if answer != QtWidgets.QMessageBox.Yes:
                    return False
            name = datetime.now().strftime('FLIR_ROI_record_%Y%m%d_%H%M%S.csv')
            path = self.panel._choose_export_path(name)
            if not path:
                return False
            path = Path(path).with_suffix('.csv')
            if path.exists() or path.with_suffix('.txt').exists():
                answer = QtWidgets.QMessageBox.question(self.panel, '覆盖连续记录文件',
                    '同名CSV或TXT已存在，是否同时覆盖？', QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                    QtWidgets.QMessageBox.No)
                if answer != QtWidgets.QMessageBox.Yes:
                    return False
            self.job_kind = 'export'
            self.job = storage.BackgroundJob(storage.export_journal, self.source, path, self.report['source_signature'])
            self.button.setEnabled(False)
            self.save_button.setEnabled(False)
            self.recover_button.setEnabled(False)
            self.panel._curve_export_directory = str(path.parent)
            self.label.setText('正在后台保存CSV和参数TXT，预览可继续…')
            return True
        finally:
            self.in_dialog = False

    def recover(self):
        if self.job is not None or (self.recorder is not None and not self.recorder.done.is_set()):
            return
        path, unused = QtWidgets.QFileDialog.getOpenFileName(self.panel, '恢复连续记录缓存',
                    str(self.root_override or storage.cache_root()), '记录缓存 (session.jsonl)')
        if not path:
            return
        self.recorder = None
        self.source = Path(path)
        self.report = None
        self._inspect(auto_save=True)


def install(panel):
    panel._recording = RecordingController(panel)
    panel.record_button = panel._recording.button
    panel._record_saturation = panel._recording.saturation


def append(panel, record):
    controller = getattr(panel, '_recording', None)
    if controller is not None:
        controller.append(record)


def marker(panel, action):
    controller = getattr(panel, '_recording', None)
    if controller is not None:
        controller.marker(action)
