"""External-trigger photo saving controls, backend lifecycle and async drain."""
from pathlib import Path
import weakref
from PyQt5 import QtCore, QtGui, QtWidgets
import photo_storage


class PhotoController(QtCore.QObject):
    def __init__(self, window, panel):
        super().__init__(window)
        self.w, self.p = window, panel
        self.sessions, self.last = [], None
        self.closing = False
        self.reported = weakref.WeakSet()
        self.root = Path(QtCore.QStandardPaths.writableLocation(QtCore.QStandardPaths.PicturesLocation)) / 'FLIR_ROI_Camera' / 'Photos'
        self.enabled = QtWidgets.QCheckBox('外部触发自动保存照片')
        self.enabled.setChecked(True)
        self.enabled.setToolTip('收到外部触发完整图像即保存TIFF和同名参数TXT；与曲线记录按钮独立。')
        self.name = QtWidgets.QLineEdit('拍摄')
        self.name.setMaxLength(60)
        self.name.setObjectName('photoShootName')
        self.name.setToolTip('文件名：系统时间_此处名称_递增序号；每个新批次从000001开始。')
        self.directory = QtWidgets.QLineEdit(str(self.root))
        self.directory.setReadOnly(True)
        self.directory.setToolTip(str(self.root))
        self.choose_button = QtWidgets.QPushButton('选择目录…')
        self.choose_button.clicked.connect(self.choose)
        self.open_button = QtWidgets.QPushButton('打开照片目录')
        self.open_button.setStyleSheet('QPushButton { padding: 2px 4px; min-height: 20px; }')
        self.open_button.clicked.connect(self.open_folder)
        self.label = QtWidgets.QLabel()
        self.label.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
        self.label.setObjectName('photoSaveStatus')
        bar = QtWidgets.QStatusBar(window)
        bar.setSizeGripEnabled(False)
        bar.addWidget(self.label, 1)
        bar.addPermanentWidget(self.open_button)
        window.setStatusBar(bar)
        self.enabled.toggled.connect(self.settings_changed)
        window.installEventFilter(self)
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self.poll)
        self.timer.start()
        self.poll()

    def set_root(self, root):
        self.root = Path(root)
        self.directory.setText(str(self.root))
        self.directory.setToolTip(str(self.root))

    def choose(self):
        chosen = QtWidgets.QFileDialog.getExistingDirectory(self.w, '选择照片保存目录', str(self.root),
            QtWidgets.QFileDialog.ShowDirsOnly | QtWidgets.QFileDialog.DontUseNativeDialog)
        if chosen:
            self.set_root(chosen)
            self.poll()

    def open_folder(self):
        path = self.last.path if self.last is not None else self.root
        if path.exists():
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(path)))
        else:
            QtWidgets.QMessageBox.information(self.w, '照片保存目录', '开始外部触发采集后创建此目录：\n' + str(path))

    def settings_changed(self):
        backend = self.w.backend
        if backend is not None and hasattr(backend, 'set_trigger_mode'):
            previous = getattr(backend, '_photo_autosave_enabled', False)
            backend._photo_autosave_enabled = self.enabled.isChecked()
            config = getattr(backend, '_trigger_configuration', {})
            if config.get('mode') and not backend.streaming:
                try:
                    backend.set_trigger_mode(config['mode'], config.get('readback', {}).get('TriggerDelay', 9))
                except Exception as exc:
                    backend._photo_autosave_enabled = previous
                    with QtCore.QSignalBlocker(self.enabled):
                        self.enabled.setChecked(previous)
                    QtWidgets.QMessageBox.warning(self.w, '无法更改照片保存设置', str(exc))
        self.poll()

    def attach(self, backend):
        if not hasattr(backend, 'set_trigger_mode') or getattr(backend, '_photo_lifecycle_installed', False):
            return
        backend._photo_lifecycle_installed = True
        backend._photo_autosave_enabled = self.enabled.isChecked()
        backend._photo_context = lambda: {'roi_bounds':tuple(self.p.bounds) if self.p.bounds else None,
            'experiment':dict(getattr(self.p, '_experiment_context', {}))}
        original_start, original_stop, original_close = backend.start, backend.stop, backend.close
        def start(*args, **kwargs):
            mode = getattr(backend, '_photo_pending_mode', None) or getattr(backend, '_trigger_configuration', {}).get('mode')
            self.ensure_session(backend, mode)
            try:
                return original_start(*args, **kwargs)
            except Exception:
                self.end_session(backend)
                raise
        def stop(*args, **kwargs):
            try:
                return original_stop(*args, **kwargs)
            finally:
                self.end_session(backend)
        def close(*args, **kwargs):
            try:
                return original_close(*args, **kwargs)
            finally:
                self.end_session(backend)
        backend.start, backend.stop, backend.close = start, stop, close

    def ensure_session(self, backend, mode):
        backend._photo_autosave_enabled = self.enabled.isChecked()
        if mode != 'external' or not self.enabled.isChecked():
            return
        current = getattr(backend, '_photo_session', None)
        if current is not None and current.accepting:
            return
        if any(not s.done.is_set() and s.status()['pending'] for s in self.sessions):
            raise photo_storage.PhotoError('上一批照片仍在写入，请等待写入完成后再开始。')
        session = photo_storage.Session(self.root, self.name.text())
        self.sessions.append(session)
        self.last = backend._photo_session = session
        self.poll()

    def prepare_mode(self, backend, mode):
        backend._photo_pending_mode = mode
        # A stopped camera only updates configuration. The batch/name is frozen
        # when acquisition actually starts, leaving setup fields editable.
        if backend.streaming:
            self.ensure_session(backend, mode)

    def finish_mode(self, backend, success):
        backend._photo_pending_mode = None
        actual = getattr(backend, '_trigger_configuration', {}).get('mode')
        if actual != 'external':
            self.end_session(backend)
        self.poll()

    def end_session(self, backend):
        session = getattr(backend, '_photo_session', None)
        if session is not None:
            session.stop()
            backend._photo_session = None

    def poll(self):
        backend = self.w.backend
        replay = backend is not None and not hasattr(backend, 'set_trigger_mode')
        external_running = (backend is not None and backend.streaming and
                            getattr(backend, '_trigger_configuration', {}).get('mode') == 'external')
        busy = any(not s.done.is_set() for s in self.sessions)
        for widget in (self.enabled, self.name, self.choose_button):
            widget.setEnabled(not replay and not external_running and not busy)
        for session in list(self.sessions):
            state = session.status()
            if state['error'] and session not in self.reported:
                self.reported.add(session)
                if backend is not None and getattr(backend, '_photo_session', None) is session:
                    self.w._stop_acquisition()
                message = '已保存{}张，待写{}张，未落盘{}张（含待写）。\n{}\n\n{}'.format(state['written'], state['pending'], state['unsaved'], state['error'], state['path'])
                self.w.state_label.setText('照片保存失败，采集已停止：' + state['error'])
                QtWidgets.QMessageBox.warning(self.w, '照片保存失败', message)
        self.sessions = [s for s in self.sessions if not s.done.is_set() or s is self.last]
        if self.last is None:
            text = '照片：仅外部触发自动保存 · ' + str(self.root) if self.enabled.isChecked() else '照片自动保存已关闭'
            if replay:
                text = '图片回放：不生成触发照片'
            self.label.setStyleSheet('')
        else:
            state = self.last.status()
            if state['error']:
                text = '照片保存失败 · 已保存{}张 · 未保存{}张 · {}'.format(state['written'], state['unsaved'], state['path'])
                self.label.setStyleSheet('color: #b42318; font-weight: 600;')
            else:
                suffix = ' · 帧号不连续，详见参数TXT' if state['frame_id_gaps'] else ''
                text = '照片已保存{}张 · 待写{}张{} · {}'.format(state['written'], state['pending'], suffix, state['path'])
                self.label.setStyleSheet('color: #975300;' if suffix else '')
        self.label.setText(text)
        self.label.setToolTip(text)
        if self.closing and not any(not s.done.is_set() for s in self.sessions):
            self.closing = False
            self.w.close()

    def eventFilter(self, obj, event):
        if obj is self.w and event.type() == QtCore.QEvent.Close:
            backend = self.w.backend
            if backend is not None:
                self.w._stop_acquisition()
            if any(not s.done.is_set() for s in self.sessions):
                self.closing = True
                self.p._recording.stop(auto_save=False)
                self.label.setText('正在写完照片后关闭，请稍候…')
                event.ignore()
                return True
        return super().eventFilter(obj, event)
