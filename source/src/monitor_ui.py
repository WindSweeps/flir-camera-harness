"""Qt LAN monitor controls and bounded, demand-driven preview production."""
import base64
import ctypes
import itertools
import math
import queue
import socket
import struct
import threading
import time
from datetime import datetime
import numpy as np
from PyQt5 import QtCore, QtGui, QtWidgets
import monitor_server
import qrcodegen


def lan_addresses():
    """Windows IP Helper IPv4 table: no shell, DNS, external probes or changes."""
    class Row(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint32) for n in ('address', 'index', 'mask', 'broadcast', 'reassembly')]
        _fields_ += [('unused', ctypes.c_uint16), ('kind', ctypes.c_uint16)]
    class Table(ctypes.Structure):
        _fields_ = [('count', ctypes.c_uint32), ('rows', Row * 1)]
    size = ctypes.c_uint32()
    get_table = ctypes.WinDLL('iphlpapi').GetIpAddrTable
    get_table.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32), ctypes.c_int]
    get_table.restype = ctypes.c_uint32
    get_table(None, ctypes.byref(size), 0)
    for unused in range(3):
        if not 4 <= size.value <= 1024 * 1024:
            return []
        buffer = ctypes.create_string_buffer(size.value)
        code = get_table(buffer, ctypes.byref(size), 0)
        if code == 122:
            continue
        if code:
            return []
        count = ctypes.c_uint32.from_buffer(buffer).value
        addresses = set()
        for i in range(count):
            offset = Table.rows.offset + i * ctypes.sizeof(Row)
            if offset + ctypes.sizeof(Row) > len(buffer):
                break
            row = Row.from_buffer(buffer, offset)
            address = socket.inet_ntoa(struct.pack('=I', row.address))
            if monitor_server.local_address(address) and not address.startswith('127.') and not row.kind & (8 | 64):
                addresses.add(address)
        return sorted(addresses, key=lambda s: (s.startswith('169.254.'), s))
    return []


def qr_image(link):
    qr = qrcodegen.QrCode.encode_text(link, qrcodegen.QrCode.Ecc.MEDIUM)
    size, border, scale = qr.get_size(), 4, 5
    image = QtGui.QImage((size + border*2)*scale, (size + border*2)*scale, QtGui.QImage.Format_RGB32)
    image.fill(QtCore.Qt.white)
    painter = QtGui.QPainter(image)
    painter.setPen(QtCore.Qt.NoPen)
    painter.setBrush(QtCore.Qt.black)
    for y in range(size):
        for x in range(size):
            if qr.get_module(x, y):
                painter.drawRect((x+border)*scale, (y+border)*scale, scale, scale)
    painter.end()
    return image


class PreviewWorker:
    def __init__(self, service):
        self.service = service
        self.queue = queue.Queue(1)
        self.stop_requested, self.done = threading.Event(), threading.Event()
        self.generation = 0
        self.generation_lock = threading.Lock()
        self.error = None
        self.encoded = 0
        self.thread = threading.Thread(target=self.run, name='FLIR-monitor-JPEG', daemon=True)
        self.thread.start()

    def submit(self, pixels, info):
        try:
            self.queue.get_nowait()
        except queue.Empty:
            pass
        self.queue.put_nowait((self.generation, pixels, info))

    def clear(self):
        with self.generation_lock:
            self.generation += 1
            try:
                self.queue.get_nowait()
            except queue.Empty:
                pass
            self.service.set_preview(None)

    def stop(self):
        self.stop_requested.set()
        self.clear()

    def run(self):
        try:
            while not self.stop_requested.is_set():
                try:
                    generation, pixels, info = self.queue.get(timeout=.2)
                except queue.Empty:
                    continue
                try:
                    height, width = pixels.shape
                    image = QtGui.QImage(pixels.data, width, height, pixels.strides[0], QtGui.QImage.Format_Grayscale8)
                    buffer = QtCore.QBuffer()
                    buffer.open(QtCore.QIODevice.WriteOnly)
                    if not image.save(buffer, 'JPEG', 72):
                        raise RuntimeError('JPEG编码不可用')
                    payload = base64.b64encode(bytes(buffer.data())).decode('ascii')
                    buffer.close()
                    self.encoded += 1
                    with self.generation_lock:
                        if generation == self.generation and not self.stop_requested.is_set():
                            self.service.set_preview({**info, 'data_url': 'data:image/jpeg;base64,' + payload})
                            self.error = None
                except Exception as exc:
                    self.error = str(exc)
                finally:
                    del pixels
        finally:
            self.done.set()


class MonitorController(QtCore.QObject):
    def __init__(self, window, panel):
        super().__init__(window)
        self.w, self.p = window, panel
        self.service = self.worker = self.dialog = None
        self.last_frame = self.source = None
        self.retiring = []
        self.address_provider = lan_addresses
        self.next_network_check = 0
        self.last_tick_ms = 0
        self.button = QtWidgets.QPushButton('手机监控…')
        self.button.setObjectName('phoneMonitorButton')
        self.button.setToolTip('在同一局域网中用手机扫码查看画面、光强及记录状态。')
        self.button.clicked.connect(self.show_dialog)
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self.tick)
        window.installEventFilter(self)
        QtWidgets.QApplication.instance().aboutToQuit.connect(self.stop)

    def show_dialog(self):
        if self.dialog is None:
            dialog = self.dialog = QtWidgets.QDialog(self.w)
            dialog.setWindowTitle('局域网手机监控')
            dialog.resize(520, 720)
            root = QtWidgets.QVBoxLayout(dialog)
            scroll = QtWidgets.QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
            content = QtWidgets.QWidget()
            layout = QtWidgets.QVBoxLayout(content)
            scroll.setWidget(content)
            root.addWidget(scroll)
            help_text = QtWidgets.QLabel('手机与电脑连接同一局域网；开启后扫码即可查看。手机只读，不修改相机参数。')
            help_text.setWordWrap(True)
            layout.addWidget(help_text)
            layout.addWidget(QtWidgets.QLabel('本机局域网地址'))
            self.address = QtWidgets.QComboBox()
            layout.addWidget(self.address)
            self.refresh_button = QtWidgets.QPushButton('刷新网络地址')
            self.refresh_button.clicked.connect(self.refresh_addresses)
            layout.addWidget(self.refresh_button)
            self.port = QtWidgets.QSpinBox()
            self.port.setRange(1024, 65535)
            self.port.setValue(8765)
            port_row = QtWidgets.QFormLayout()
            port_row.addRow('端口', self.port)
            layout.addLayout(port_row)
            self.toggle_button = QtWidgets.QPushButton('开启手机监控')
            self.toggle_button.clicked.connect(self.toggle)
            layout.addWidget(self.toggle_button)
            self.status = QtWidgets.QLabel('监控未开启')
            self.status.setWordWrap(True)
            layout.addWidget(self.status)
            self.qr = QtWidgets.QLabel()
            self.qr.setAlignment(QtCore.Qt.AlignCenter)
            layout.addWidget(self.qr)
            self.link = QtWidgets.QLineEdit()
            self.link.setReadOnly(True)
            layout.addWidget(self.link)
            self.copy_button = QtWidgets.QPushButton('复制访问链接')
            self.copy_button.clicked.connect(self.copy_link)
            layout.addWidget(self.copy_button)
            self.key_label = QtWidgets.QLabel()
            self.key_label.setWordWrap(True)
            self.key_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
            layout.addWidget(self.key_label)
            hint = QtWidgets.QLabel('每次开启生成新链接。关闭监控或退出软件后链接失效。\n\n若Windows询问网络访问，请允许“专用网络”；手机需与所选地址互通，访客Wi-Fi可能隔离设备。')
            hint.setWordWrap(True)
            layout.addWidget(hint)
            layout.addStretch(1)
            close = QtWidgets.QPushButton('关闭此窗口')
            close.clicked.connect(dialog.hide)
            root.addWidget(close)
        self.refresh_addresses()
        self.render_controls()
        self.dialog.show()
        self.dialog.raise_()

    def refresh_addresses(self):
        if self.dialog is None or self.service is not None:
            return
        previous = self.address.currentText()
        self.address.clear()
        try:
            self.address.addItems(self.address_provider())
        except Exception:
            pass
        if previous in [self.address.itemText(i) for i in range(self.address.count())]:
            self.address.setCurrentText(previous)
        self.toggle_button.setEnabled(self.address.count() > 0)
        if not self.address.count():
            self.status.setText('未找到局域网IPv4地址，请连接Wi-Fi或网线后刷新。')

    def copy_link(self):
        if self.service is None:
            return
        QtWidgets.QApplication.clipboard().setText(self.service.link)
        self.copy_button.setText('链接已复制')
        QtCore.QTimer.singleShot(1500, lambda: self.copy_button.setText('复制访问链接'))

    def toggle(self):
        if self.service is not None:
            self.stop()
            return
        try:
            self.start(self.address.currentText(), self.port.value())
        except Exception as exc:
            self.status.setText('开启失败，请检查网络地址或更换端口：' + str(exc))

    def start(self, host, port=8765):
        if self.service is not None:
            return
        service = monitor_server.Service(host, port)
        try:
            worker = PreviewWorker(service)
        except BaseException:
            service.stop()
            raise
        self.service, self.worker = service, worker
        self.last_frame = self.source = None
        self.next_network_check = time.monotonic() + 5
        self.button.setText('手机监控 · 已开启')
        self.timer.start()
        self.tick()
        self.render_controls()

    def stop(self):
        self.timer.stop()
        if self.service is not None:
            self.service.stop()
            self.worker.stop()
            self.retiring.append((self.service, self.worker))
        self.service = self.worker = None
        self.last_frame = self.source = None
        self.button.setText('手机监控…')
        self.render_controls()

    def render_controls(self):
        if self.dialog is None:
            return
        active = self.service is not None
        self.address.setEnabled(not active)
        self.port.setEnabled(not active)
        self.refresh_button.setEnabled(not active)
        self.toggle_button.setText('关闭手机监控' if active else '开启手机监控')
        self.toggle_button.setEnabled(active or self.address.count() > 0)
        self.copy_button.setEnabled(active)
        self.link.setText(self.service.link if active else '')
        self.link.setCursorPosition(0)
        self.key_label.setText('访问密钥：' + self.service.token if active else '')
        if active:
            self.address.clear()
            self.address.addItem(self.service.http.server_address[0])
            self.port.setValue(self.service.http.server_address[1])
            self.qr.setPixmap(QtGui.QPixmap.fromImage(qr_image(self.service.link)))
            self.status.setText('已开启 · 等待手机连接')
        else:
            self.qr.clear()
            self.status.setText('监控未开启' if self.address.count() else '未找到局域网IPv4地址，请连接网络后刷新。')

    def collect(self):
        p, w = self.p, self.w
        backend = w.backend
        meta = getattr(p, '_saturation_metadata', {}) if getattr(p, '_saturation_backend', None) is backend else {}
        values = {}
        for name in ('ExposureTime', 'Gain', 'ExposureAuto', 'GainAuto', 'BinningHorizontal', 'BinningVertical', 'PixelFormat'):
            entry = meta.get('effective_parameters', {}).get(name) or meta.get('nodes', {}).get(name) or {}
            values[name] = entry.get('value')
        samples = p.samples
        stride = max(1, math.ceil(len(samples) / 180))
        curve = [[float(t), int(v)] for t, v in itertools.islice(samples, 0, None, stride)]
        if samples and (not curve or curve[-1][0] != samples[-1][0]):
            curve.append([float(samples[-1][0]), int(samples[-1][1])])
        photos = w._workflow.photos
        photo_text = '未开启自动保存' if not photos.enabled.isChecked() else '等待外部触发照片'
        if photos.last is not None:
            status = photos.last.status()
            photo_text = '已保存 {} 张 · 待写 {} 张'.format(status['written'], status['pending'])
            if status['error']:
                photo_text += ' · 保存失败：' + status['error'].split('\n')[0][:120]
            elif status['frame_id_gaps']:
                photo_text += ' · 检测到帧号不连续'
        state = getattr(p, '_saturation_state', {})
        history = getattr(p, '_saturation_history', {})
        recorder = p._recording.recorder
        ever = history.get('detected', False) or bool(recorder and recorder.saturation_history.get('detected'))
        sat_text = p.saturation_label.text()
        if ever and not state.get('saturated') and '已记录' not in sat_text:
            sat_text += ' · 本次选区或记录曾检测到过曝'
        if meta.get('source') == 'replay_simulation':
            photo_text = '图片回放 · 不生成触发照片'
        return {'software_version': '1.9', 'camera': w.camera_combo.currentText(),
                'acquiring': bool(backend and backend.streaming), 'mode': w._workflow.mode_note.text(),
                'roi': list(p.bounds) if p.bounds is not None else None,
                'intensity': int(samples[-1][1]) if samples and p.bounds else None,
                'peak': p.peak_label.text(), 'curve': curve, 'parameters': values,
                'saturation': {'text': sat_text, 'saturated': state.get('saturated'), 'ever': ever},
                'recording': p._recording.label.text(), 'photos': photo_text,
                'preview_error': self.worker.error if self.worker else None}

    def tick(self):
        if self.service is None:
            return
        start_counter = time.perf_counter()
        began = time.monotonic()
        self.retiring = [(s, w) for s, w in self.retiring if not s.stopped.is_set() or not w.done.is_set()]
        if began >= self.next_network_check:
            self.next_network_check = began + 5
            host = self.service.http.server_address[0]
            if not host.startswith('127.') and host not in self.address_provider():
                self.stop()
                if self.dialog:
                    self.status.setText('网络地址已改变或断开，请刷新地址后重新开启。')
                return
        self.service.publish(self.collect())
        clients = self.service.active_clients()
        frame = self.p.frame
        source = (id(self.w.backend), frame.shape if frame is not None else None)
        if source != self.source:
            self.source, self.last_frame = source, None
            self.worker.clear()
        if clients and frame is not None and frame.ndim == 2 and frame.dtype == np.uint8:
            stamp = getattr(self.p, '_saturation_frame_time', None)
            if stamp is not None and stamp != self.last_frame:
                step = max(1, math.ceil(max(frame.shape[1]/960, frame.shape[0]/720)))
                pixels = np.array(frame[::step, ::step], dtype=np.uint8, order='C', copy=True)
                self.worker.submit(pixels, {'monotonic_s': float(stamp),
                    'received_local': datetime.fromtimestamp(time.time() - max(0, time.perf_counter()-stamp)).strftime('%H:%M:%S'),
                    'original_width': frame.shape[1], 'original_height': frame.shape[0]})
                self.last_frame = stamp
        if not clients and self.last_frame is not None:
            self.worker.clear()
            self.last_frame = None
        if self.dialog and self.dialog.isVisible():
            self.status.setText('已开启 · 最近连接 {} 台 · {}'.format(clients, self.service.url) +
                                ('\n预览编码失败：' + self.worker.error if self.worker.error else ''))
        self.last_tick_ms = (time.perf_counter() - start_counter) * 1000

    def eventFilter(self, obj, event):
        if obj is self.w and event.type() == QtCore.QEvent.Close:
            self.stop()
        return super().eventFilter(obj, event)
