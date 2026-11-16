"""Task-oriented native Qt workspace. Typography tokens are multiples of 6 pt."""
import os
import time
os.environ['PYQTGRAPH_QT_LIB'] = 'PyQt5'
from PyQt5 import QtCore, QtGui, QtWidgets
import camera_modes
import photo_ui
import monitor_ui


def detach_layout(layout):
    """Remove layout items, retaining widgets and their signal connections."""
    while layout.count():
        item = layout.takeAt(0)
        if item.layout():
            detach_layout(item.layout())
            item.layout().deleteLater()


def text_label(text, role=None):
    label = QtWidgets.QLabel(text)
    label.setWordWrap(True)
    label.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
    if role:
        label.setProperty('role', role)
    return label


class Workflow(QtCore.QObject):
    def __init__(self, window, panel):
        super().__init__(window)
        self.w, self.p = window, panel
        self.zoom = 1.0
        self.applied = ('continuous', 9.0)
        self.replay = False
        self.layout_mode = None
        self.metric_mode = None
        self.resize_pending = False
        self.factory = window.backend_factory
        self.photos = photo_ui.PhotoController(window, panel)
        self.monitor = monitor_ui.MonitorController(window, panel)
        self.build()
        window.backend_factory = self.create_backend
        window.auto_exposure_check.toggled.connect(self.auto_changed)
        window.exposure_timer.timeout.disconnect()
        window.exposure_timer.timeout.connect(self.refresh)
        window.start_button.clicked.connect(self.refresh)
        window.camera_combo.currentIndexChanged.connect(self.refresh)
        window.installEventFilter(self)
        self.set_zoom()
        self.auto_changed()
        self.refresh()

    def section(self, title):
        group = QtWidgets.QFrame()
        group.setObjectName('controlSection')
        layout = QtWidgets.QVBoxLayout(group)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        layout.addWidget(text_label(title, 'section'))
        self.side_layout.addWidget(group)
        return layout

    def row(self, layout, label, widget, button=None):
        layout.addWidget(text_label(label))
        line = QtWidgets.QHBoxLayout()
        line.addWidget(widget, 1)
        if button is not None:
            line.addWidget(button)
        layout.addLayout(line)

    def build(self):
        w, p = self.w, self.p
        w.setWindowTitle('FLIR ROI Camera · 1.9')
        root = w.centralWidget().layout()
        # Drop only the obsolete anonymous toolbar labels; named widgets are reused.
        old_toolbar = root.itemAt(0).layout()
        for i in range(old_toolbar.count()):
            widget = old_toolbar.itemAt(i).widget()
            if isinstance(widget, QtWidgets.QLabel) and widget is not w.state_label:
                widget.hide()
                widget.deleteLater()
        detach_layout(root)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)
        detach_layout(p.layout())
        p.setMinimumSize(0, 0)
        p.setMaximumSize(16777215, 16777215)

        self.side = QtWidgets.QScrollArea()
        self.side.setWidgetResizable(True)
        self.side.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.side.setMinimumWidth(0)
        body = QtWidgets.QWidget()
        self.side_layout = QtWidgets.QVBoxLayout(body)
        self.side_layout.setContentsMargins(0, 0, 6, 0)
        self.side_layout.setSpacing(12)
        self.side.setWidget(body)

        camera = self.section('相机与采集')
        w.camera_combo.setMinimumWidth(0)
        w.camera_combo.setMaximumWidth(16777215)
        w.camera_combo.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
        w.camera_combo.setMinimumContentsLength(10)
        w.refresh_cameras_button.setText('刷新')
        self.row(camera, '相机', w.camera_combo, w.refresh_cameras_button)
        self.mode = QtWidgets.QComboBox()
        self.mode.setObjectName('acquisitionModeCombo')
        self.mode.addItem('连续采集', 'continuous')
        self.mode.addItem('外部触发 · Line0 上升沿', 'external')
        self.row(camera, '采集模式', self.mode)
        self.delay = QtWidgets.QDoubleSpinBox()
        self.delay.setObjectName('triggerDelaySpin')
        self.delay.setRange(0, 100000000)
        self.delay.setDecimals(2)
        self.delay.setSuffix(' μs')
        self.delay.setValue(9)
        self.delay.setKeyboardTracking(False)
        self.delay_label = text_label('触发延迟')
        camera.addWidget(self.delay_label)
        camera.addWidget(self.delay)
        self.mode_note = text_label('连续采集 · 无需外部信号', 'muted')
        self.mode_note.setObjectName('triggerModeStatus')
        camera.addWidget(self.mode_note)
        w.start_button.setProperty('role', 'primary')
        w.state_label.setWordWrap(True)
        w.state_label.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
        camera.addWidget(w.state_label)
        self.mode.currentIndexChanged.connect(self.apply_mode)
        self.delay.editingFinished.connect(self.apply_mode)

        photos = self.section('触发照片保存')
        photos.addWidget(self.photos.enabled)
        self.row(photos, '拍摄名称（文件名）', self.photos.name)
        self.row(photos, '照片保存目录', self.photos.directory)
        photos.addWidget(self.photos.choose_button)
        photos.addWidget(text_label('系统时间_拍摄名称_序号.tif\n每张照片配同名参数 TXT。', 'muted'))

        brightness = self.section('曝光与增益')
        w.auto_exposure_check.setText('自动曝光 + 自动增益')
        w.auto_exposure_check.setToolTip('由相机自动调节曝光和增益；实际值随图像更新。')
        brightness.addWidget(w.auto_exposure_check)
        w.apply_exposure_button.setText('应用')
        w.apply_gain_button.setText('应用')
        self.row(brightness, '曝光时间', w.exposure_spin, w.apply_exposure_button)
        self.row(brightness, '增益', w.gain_spin, w.apply_gain_button)
        self.gain_limit = QtWidgets.QDoubleSpinBox()
        self.gain_limit.setObjectName('autoGainUpperLimitSpin')
        self.gain_limit.setRange(0, 48)
        self.gain_limit.setDecimals(2)
        self.gain_limit.setSuffix(' dB')
        self.gain_limit.setValue(12)
        self.gain_limit.setKeyboardTracking(False)
        self.row(brightness, '自动增益上限', self.gain_limit)
        self.gain_limit.editingFinished.connect(self.apply_gain_limit)
        self.brightness_note = text_label('连接后按相机范围限制上限。自动模式下显示实际曝光与增益。', 'muted')
        brightness.addWidget(self.brightness_note)

        measure = self.section('选区与记录')
        p.select_button.setText('框选统计区域')
        p.reset_button.setText('清空曲线 / 重新计时')
        p.export_button.setText('导出 60 s 曲线…')
        p.record_button.setProperty('role', 'record')
        for button in (p.reset_button, p._recording.save_button):
            measure.addWidget(button)
        measure.addWidget(text_label('记录可持续至手动停止；曲线仅显示最近 60 s。CSV 与参数 TXT 同目录保存。', 'muted'))
        details = self.section('实验与显示')
        details.addWidget(p.context_button)
        details.addWidget(p._recording.recover_button)
        self.zoom_combo = QtWidgets.QComboBox()
        for label, factor in (('100%', 1.0), ('150%', 1.5), ('200%', 2.0)):
            self.zoom_combo.addItem(label, factor)
        self.row(details, '界面字号', self.zoom_combo)
        self.zoom_combo.currentIndexChanged.connect(self.set_zoom)
        self.side_layout.addStretch(1)

        # Primary actions stay visible while settings scroll, including at 200%.
        self.commands = QtWidgets.QGridLayout()
        self.commands.setSpacing(8)
        self.settings_button = QtWidgets.QPushButton('相机参数')
        self.settings_button.setCheckable(True)
        self.settings_button.setChecked(True)
        self.settings_button.setToolTip('显示或收起左侧参数区，为图像留出更多空间。')
        self.settings_button.toggled.connect(self.side.setVisible)
        self.command_buttons = (w.start_button, p.select_button, p.record_button, p.export_button, self.settings_button)
        for button, shortcut in zip(self.command_buttons, ('F5', 'Ctrl+L', 'Ctrl+R', 'Ctrl+E', 'Ctrl+P')):
            button.setShortcut(shortcut)
            button.setToolTip(button.toolTip() + '\n快捷键：' + shortcut)
        root.addLayout(self.commands)

        # Image stays in the original GraphicsView, preserving ROI interaction.
        self.image_area = QtWidgets.QWidget()
        image_layout = QtWidgets.QVBoxLayout(self.image_area)
        image_layout.setContentsMargins(0, 0, 0, 0)
        image_layout.setSpacing(4)
        image_header = QtWidgets.QHBoxLayout()
        image_header.addWidget(text_label('相机图像'), 1)
        image_header.addWidget(self.monitor.button)
        self.fit_button = QtWidgets.QPushButton('完整画面')
        self.fit_button.setToolTip('按原比例显示整幅图像；双击图像也可恢复完整画面。')
        self.fit_button.clicked.connect(w._fit_image_to_view)
        image_header.addWidget(self.fit_button)
        image_layout.addLayout(image_header)
        w.graphics.setMinimumSize(180, 240)
        image_layout.addWidget(w.graphics, 1)
        p.region_label.setWordWrap(True)
        p.region_label.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
        image_layout.addWidget(p.region_label)

        results = p.layout()
        results.setSpacing(4)
        results.setContentsMargins(0, 2, 4, 2)
        results.addWidget(p.saturation_label)
        self.data_row = QtWidgets.QHBoxLayout()
        self.data_row.setSpacing(8)
        self.metric_panel = QtWidgets.QWidget()
        self.metrics = QtWidgets.QGridLayout()
        self.metrics.setContentsMargins(0, 0, 0, 0)
        self.metrics.setSpacing(4)
        self.metric_panel.setLayout(self.metrics)
        self.data_row.addWidget(self.metric_panel)
        p.plot.setMinimumHeight(130)
        p.plot.setMaximumHeight(16777215)
        p.plot.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
        self.data_row.addWidget(p.plot, 1)
        results.addLayout(self.data_row, 1)
        p._recording.label.setStyleSheet('QLabel { color: #526879; padding: 0px; }')
        results.addWidget(p._recording.label)
        for card in (p.value_card, p.peak_card):
            card.setMinimumSize(0, 0)
            card.setMaximumSize(16777215, 16777215)
            card.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
            card.setStyleSheet('')
            card.layout().setContentsMargins(8, 4, 8, 4)
            card.layout().setSpacing(2)
            for label in card.findChildren(QtWidgets.QLabel):
                label.setStyleSheet('')
                label.setWordWrap(True)
                label.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
                label.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
        for value in (p.value_label, p.peak_label):
            value.setProperty('role', 'value')
        value_title = [x for x in p.value_card.findChildren(QtWidgets.QLabel) if x is not p.value_label][0]
        value_title.setText('当前区域光强')
        peak_title = [x for x in p.peak_card.findChildren(QtWidgets.QLabel) if x is not p.peak_label][0]
        peak_title.setText('240 s 最高')
        p.clear_peak_button.setStyleSheet('QPushButton { padding: 2px 4px; min-height: 20px; }')
        detach_layout(p.peak_card.layout())
        peak_header = QtWidgets.QHBoxLayout()
        peak_header.addWidget(peak_title, 1)
        peak_header.addWidget(p.clear_peak_button)
        p.peak_card.layout().addLayout(peak_header)
        p.peak_card.layout().addWidget(p.peak_label)
        self.metrics.addWidget(p.value_card, 0, 0)
        self.metrics.addWidget(p.peak_card, 1, 0)
        self.results_scroll = QtWidgets.QScrollArea()
        self.results_scroll.setWidgetResizable(True)
        self.results_scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.results_scroll.setWidget(p)
        self.results_scroll.setMinimumHeight(120)
        self.workspace = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        self.workspace.setChildrenCollapsible(False)
        self.workspace.addWidget(self.image_area)
        self.workspace.addWidget(self.results_scroll)
        self.workspace.setStretchFactor(0, 1)
        self.workspace.setStretchFactor(1, 0)
        self.workspace.setSizes([760, 220])
        self.outer = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self.outer.setChildrenCollapsible(False)
        self.outer.addWidget(self.side)
        self.outer.addWidget(self.workspace)
        self.outer.setStretchFactor(0, 0)
        self.outer.setStretchFactor(1, 1)
        root.addWidget(self.outer, 1)
        tab_order = self.command_buttons + (w.camera_combo, w.refresh_cameras_button, self.mode, self.delay,
            w.auto_exposure_check, w.exposure_spin, w.apply_exposure_button, w.gain_spin, w.apply_gain_button,
            self.gain_limit, p.reset_button, p._recording.save_button, p.context_button,
            p._recording.recover_button, self.zoom_combo, p.clear_peak_button)
        for first, second in zip(tab_order, tab_order[1:]):
            QtWidgets.QWidget.setTabOrder(first, second)
        w.setMinimumSize(640, 640)
        w.resize(1440, 1000)

    def create_backend(self, *args, **kwargs):
        backend = self.factory(*args, **kwargs)
        self.replay = not hasattr(backend, 'set_trigger_mode')
        self.photos.attach(backend)
        if self.replay:
            self.mode.model().item(1).setEnabled(False)
            with QtCore.QSignalBlocker(self.mode):
                self.mode.setCurrentIndex(0)
            self.gain_limit.setEnabled(False)
            self.brightness_note.setText('图片回放：曝光和增益为模拟参数，不改变回放图像。')
            self.applied = ('continuous', self.delay.value())
            return backend
        self.mode.model().item(1).setEnabled(True)
        try:
            try:
                low, high, actual = camera_modes.gain_limit_range(backend)
                with QtCore.QSignalBlocker(self.gain_limit):
                    self.gain_limit.setRange(low, high)
                self.gain_limit.setEnabled(True)
                backend._auto_gain_upper_requested = self.gain_limit.value()
                self.brightness_note.setText('自动调节曝光与增益。提高增益也会放大噪声；读数尚未标定物理单位。')
            except Exception:
                backend._auto_gain_upper_requested = None
                self.gain_limit.setEnabled(False)
                self.brightness_note.setText('无法调整此相机的自动增益上限，使用设备已有上限。')
            desired = (self.mode.currentData(), self.delay.value())
            self.photos.prepare_mode(backend, desired[0])
            backend.set_trigger_mode(*desired)
            self.photos.finish_mode(backend, True)
            self.applied = desired
            return backend
        except Exception:
            backend.close()
            raise

    def apply_mode(self, *unused):
        desired = (self.mode.currentData(), self.delay.value())
        self.delay.setEnabled(desired[0] == 'external' and not self.replay)
        backend = self.w.backend
        if backend is None:
            self.refresh()
            return
        if self.replay or desired == self.applied:
            self.refresh()
            return
        running = self.w.acquisition_timer.isActive()
        self.w.acquisition_timer.stop()
        try:
            self.photos.prepare_mode(backend, desired[0])
            backend.set_trigger_mode(*desired)
            self.applied = desired
            self.w.state_label.setText('采集设置已读回确认')
        except Exception as exc:
            with QtCore.QSignalBlocker(self.mode), QtCore.QSignalBlocker(self.delay):
                self.mode.setCurrentIndex(self.mode.findData(self.applied[0]))
                self.delay.setValue(self.applied[1])
            self.w.state_label.setText('切换失败：' + str(exc))
        finally:
            self.photos.finish_mode(backend, desired == self.applied)
            if running and backend.streaming:
                self.w.acquisition_timer.start()
            if not backend.streaming:
                self.w.start_button.setText('开始采集')
            self.refresh()

    def apply_gain_limit(self):
        backend = self.w.backend
        if backend is None or self.replay:
            return
        old = getattr(backend, '_auto_gain_upper_requested', None)
        new = self.gain_limit.value()
        if old == new:
            return
        backend._auto_gain_upper_requested = new
        if not self.w.auto_exposure_check.isChecked():
            self.brightness_note.setText('新上限将在启用自动曝光 + 自动增益时生效。')
            return
        try:
            backend.set_exposure_auto(True)
            self.w.state_label.setText('自动增益上限已读回确认')
        except Exception as exc:
            backend._auto_gain_upper_requested = old
            if old is not None:
                with QtCore.QSignalBlocker(self.gain_limit):
                    self.gain_limit.setValue(old)
            self.w.state_label.setText('设置上限失败：' + str(exc))
            if not backend.streaming:
                self.w.acquisition_timer.stop()
                self.w.start_button.setText('开始采集')

    def auto_changed(self, *unused):
        enabled = self.w.auto_exposure_check.isChecked()
        for widget in (self.w.exposure_spin, self.w.apply_exposure_button, self.w.gain_spin, self.w.apply_gain_button):
            widget.setEnabled(not enabled)
        backend = self.w.backend
        if backend is not None and not backend.streaming and self.w.acquisition_timer.isActive():
            self.w.acquisition_timer.stop()
            self.w.start_button.setText('开始采集')
        self.refresh()

    def refresh(self, *unused):
        backend = self.w.backend
        if backend is not None and not backend.streaming and self.w.acquisition_timer.isActive():
            self.w.acquisition_timer.stop()
            self.w.start_button.setText('开始采集')
        external = self.mode.currentData() == 'external'
        self.delay.setEnabled(external and not self.replay)
        if backend is None:
            state = '外部触发 · Line0 / 上升沿 / FrameStart' if external else '连续采集 · 无需外部信号'
            self.mode_note.setText(state + '\n设置将在开始采集时应用。')
        elif self.replay:
            self.mode_note.setText('图片回放 · 外部触发不可用')
        elif external and backend.streaming:
            last = getattr(backend, '_last_frame_received_monotonic_s', None)
            receiving = last is not None and time.perf_counter() - last < 2
            self.mode_note.setText(('已收到图像' if receiving else '等待 Line0 上升沿') + '\nFrameStart · 延迟 {:g} μs · Overlap Off'.format(self.applied[1]))
        else:
            self.mode_note.setText(('外部触发已配置' if external else '连续采集') + (' · 采集中' if backend.streaming else ' · 已停止'))
        if backend is not None and self.w.auto_exposure_check.isChecked():
            meta = getattr(self.p, '_saturation_metadata', {})
            if getattr(self.p, '_saturation_backend', None) is not backend:
                return
            effective = meta.get('effective_parameters', {})
            nodes = meta.get('nodes', {})
            for name, spin in (('ExposureTime', self.w.exposure_spin), ('Gain', self.w.gain_spin)):
                record = effective.get(name) or nodes.get(name) or {}
                value = record.get('value')
                if isinstance(value, (float, int)):
                    with QtCore.QSignalBlocker(spin):
                        spin.setValue(value)
                    spin.setToolTip('最近收到帧的读数；无新帧时保留最近值。')

    def set_zoom(self, *unused):
        self.zoom = self.zoom_combo.currentData()
        body, section, value = {1.0: (12, 18, 18), 1.5: (18, 24, 24), 2.0: (24, 36, 36)}[self.zoom]
        style = '''
            QWidget { font-family: "Microsoft YaHei UI"; font-size: BODYpt; color: #183047; }
            QMainWindow, QScrollArea { background: #f3f5f8; }
            QFrame#controlSection { background: white; border: 1px solid #d5dde6; border-radius: 8px; }
            QLabel[role="section"] { font-size: SECTIONpt; font-weight: 600; color: #17364f; }
            QLabel[role="muted"] { color: #526879; }
            QLabel[role="value"] { font-size: VALUEpt; font-weight: 600; color: #174f7d; }
            QFrame#intensityCard, QFrame#peakCard { background: #edf4fa; border: 1px solid #b8cddd; border-radius: 8px; }
            QPushButton, QComboBox, QDoubleSpinBox { padding: 6px; min-height: 26px; border: 1px solid #aabac8; border-radius: 4px; background: white; }
            QPushButton:hover { background: #edf4fa; border-color: #356b98; }
            QPushButton:focus, QComboBox:focus, QDoubleSpinBox:focus, QCheckBox:focus { border: 2px solid #1768a6; }
            QPushButton:checked { background: #d8eafb; border-color: #1768a6; }
            QPushButton[role="primary"] { background: #1768a6; border-color: #1768a6; color: white; font-weight: 600; }
            QPushButton[role="primary"]:hover { background: #105687; }
            QPushButton[role="record"] { color: #9a2738; border-color: #ae5663; font-weight: 600; }
            QPushButton:disabled, QComboBox:disabled, QDoubleSpinBox:disabled { color: #74818c; background: #edf0f3; border-color: #d4dce3; }
            QSplitter::handle { background: #d5dde6; }
            QToolTip { font-size: BODYpt; padding: 6px; }
        '''.replace('BODY', str(body)).replace('SECTION', str(section)).replace('VALUE', str(value))
        self.w.setStyleSheet(style)
        font = QtGui.QFont('Microsoft YaHei UI', body)
        for plot in (self.p.plot, self.w.image_plot):
            for side in ('left', 'bottom'):
                axis = plot.getAxis(side)
                axis.setTickFont(font)
                axis.label.setFont(font)
                axis.setLabel(text=axis.labelText, units=axis.labelUnits,
                              **{'font-size': '{}pt'.format(body), 'color': '#526879'})
        self.layout_mode = self.metric_mode = None
        self.reflow()
        QtCore.QTimer.singleShot(0, self.reflow)

    def eventFilter(self, obj, event):
        if obj is self.w and event.type() == QtCore.QEvent.Resize and not self.resize_pending:
            self.resize_pending = True
            QtCore.QTimer.singleShot(0, self.reflow)
        return super().eventFilter(obj, event)

    def reflow(self):
        self.resize_pending = False
        narrow = self.w.width() < 1100
        if narrow != self.layout_mode:
            self.layout_mode = narrow
            self.side.setMinimumWidth(int(280 * min(self.zoom, 1.5)))
            self.outer.setSizes([int(310 * min(self.zoom, 1.5)), 1000])
            self.settings_button.setChecked(not narrow)
        command_columns = 3 if self.w.width() < 800 or self.zoom > 1 else 5
        for index, button in enumerate(self.command_buttons):
            self.commands.removeWidget(button)
            self.commands.addWidget(button, index // command_columns, index % command_columns)
        for column in range(5):
            self.commands.setColumnStretch(column, 1 if column < command_columns else 0)
        if self.zoom > 1:
            self.data_row.setDirection(QtWidgets.QBoxLayout.TopToBottom)
            self.metric_panel.setMinimumWidth(0)
            self.metric_panel.setMaximumWidth(16777215)
        else:
            self.data_row.setDirection(QtWidgets.QBoxLayout.LeftToRight)
            self.metric_panel.setFixedWidth(250)
        self.p.plot.setMinimumHeight(int(130 * self.zoom))
        # Reserve most height for the whole frame. The small data pane scrolls
        # at large text scales rather than growing at the image's expense.
        available = max(1, self.workspace.height() - self.workspace.handleWidth())
        data_height = max(120, min(int(220 * min(self.zoom, 1.3)), int(available * .28)))
        self.results_scroll.setMaximumHeight(max(data_height, int(available * .35)))
        self.workspace.setSizes([max(240, available - data_height), data_height])


def install_for_panel(panel):
    def attach():
        window = panel.window()
        if not all(hasattr(window, name) for name in ('backend_factory', 'graphics', 'camera_combo', 'exposure_timer')):
            return
        if not hasattr(window, '_workflow'):
            window._workflow = Workflow(window, panel)
    QtCore.QTimer.singleShot(0, attach)
