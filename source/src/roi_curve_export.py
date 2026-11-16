"""CSV export extension for the supplied FLIR ROI Camera 1.1 application.

Installed before the unmodified main script; uses only existing Python/Qt modules.
"""
import csv
import os
import tempfile
import copy
from collections import deque
from datetime import datetime
from pathlib import Path

os.environ["PYQTGRAPH_QT_LIB"] = "PyQt5"
from PyQt5 import QtWidgets
from roi_intensity import RoiIntensityPanel
from camera_backend import FlirCameraBackend
import capture_metadata
import parameter_log
import saturation
import saturation_ui
import recording_ui
import camera_modes
import workflow_ui
from parameter_log import write_export_bundle
from experiment_context import get_default_context, edit_context

CSV_HEADER = ("经过时间_s", "光强总和_DN", "选区X_px", "选区Y_px", "选区宽度_px", "选区高度_px")


def write_curve_csv(path, samples, bounds):
    """Write a complete frozen curve atomically; leave an existing file on failure."""
    rows = tuple(samples)
    if not rows or bounds is None:
        raise ValueError("当前选区没有可导出的曲线数据。")
    x0, y0, x1, y1 = map(int, bounds)
    if x1 <= x0 or y1 <= y0:
        raise ValueError("当前选区无效。")
    path = Path(path)
    temporary = None
    try:
        # Same directory allows an atomic replace on the same filesystem.
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8-sig", newline="",
                                         dir=str(path.parent), prefix=".flir_csv_",
                                         suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            writer = csv.writer(stream)
            writer.writerow(CSV_HEADER)
            for elapsed, total in rows:
                writer.writerow((float(elapsed), int(total), x0, y0, x1 - x0, y1 - y0))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temporary), str(path))
        temporary = None
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass
    return len(rows)


def choose_export_path(panel, default_name):
    title = '保存全部连续记录' if default_name.startswith('FLIR_ROI_record_') else '导出当前选区光强曲线'
    dialog = QtWidgets.QFileDialog(panel, title)
    # Qt's dialog continues dispatching acquisition timers during file selection.
    dialog.setOption(QtWidgets.QFileDialog.DontUseNativeDialog, True)
    dialog.setAcceptMode(QtWidgets.QFileDialog.AcceptSave)
    dialog.setFileMode(QtWidgets.QFileDialog.AnyFile)
    dialog.setNameFilter("CSV 表格 (*.csv)")
    dialog.setDefaultSuffix("csv")
    directory = getattr(panel, "_curve_export_directory", None)
    if directory:
        dialog.setDirectory(directory)
    dialog.selectFile(default_name)
    if dialog.exec_() != QtWidgets.QDialog.Accepted:
        return ""
    selected = dialog.selectedFiles()
    return selected[0] if selected else ""


def export_current_curve(panel):
    # Freeze before opening a modal dialog: Qt may continue receiving new frames.
    snapshot = freeze_export(panel)
    samples, bounds = snapshot['samples'], snapshot['bounds']
    if not samples or bounds is None:
        QtWidgets.QMessageBox.information(panel, "暂无曲线数据", "请先框选统计区域并采集曲线，再导出。")
        return False
    history = snapshot.get('saturation_history', {})
    if history.get('detected'):
        choice = QtWidgets.QMessageBox.question(panel, '过曝数据保存确认',
            '本次选区自上次清空以来检测到过曝（{} 次检查）。\n'
            '记录可能早于当前60秒曲线窗口，相关光强数据可能已失真。\n'
            '是否仍然保存当前 CSV 和 TXT？'.format(history.get('detection_count', 0)),
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No, QtWidgets.QMessageBox.No)
        if choice != QtWidgets.QMessageBox.Yes:
            return False
    filename = datetime.now().strftime("FLIR_ROI_curve_%Y%m%d_%H%M%S.csv")
    path = panel._choose_export_path(filename)
    if not path:
        return False
    txt_path = Path(path).with_suffix('.txt')
    if txt_path.exists():
        choice = QtWidgets.QMessageBox.question(panel, '覆盖参数日志',
            '同名参数日志已存在，是否同时更新 CSV 和 TXT？\n' + str(txt_path),
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No, QtWidgets.QMessageBox.No)
        if choice != QtWidgets.QMessageBox.Yes:
            return False
    try:
        count = write_export_bundle(path, **snapshot)
    except Exception as exc:
        QtWidgets.QMessageBox.warning(panel, "导出失败", "无法保存 CSV 和参数 TXT，请检查保存位置或文件是否被占用。\n\n" + str(exc))
        return False
    panel._curve_export_directory = str(Path(path).parent)
    QtWidgets.QMessageBox.information(
        panel, "导出完成", "已保存 {} 个数据点和对应的参数历史。\n当前未标定物理量。\n\n{}\n{}".format(count, path, txt_path))
    return True



def install():
    if getattr(RoiIntensityPanel, "_curve_csv_export_installed", False):
        return
    original_init = RoiIntensityPanel.__init__
    original_consume = RoiIntensityPanel.consume_frame
    original_reset = RoiIntensityPanel.reset_trace
    original_show_value = RoiIntensityPanel._show_value
    original_clear_source = RoiIntensityPanel.clear_source

    def init_with_export(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self._parameter_records = deque()
        self._experiment_context = get_default_context()
        saturation_ui.init(self)
        self.export_button = QtWidgets.QPushButton("导出当前曲线 CSV", self)
        self.export_button.setObjectName("exportCurrentCurveButton")
        self.export_button.setToolTip("保存当前曲线 CSV 及同名 TXT 参数历史；包含曝光、增益、binning 等，停止后也可导出。")
        self.export_button.clicked.connect(self.export_curve)
        controls = self.layout().itemAt(0).layout()
        controls.insertWidget(controls.indexOf(self.reset_button) + 1, self.export_button)
        self.context_button = QtWidgets.QPushButton('实验/标定信息', self)
        self.context_button.clicked.connect(lambda: edit_context(self))
        controls.insertWidget(controls.indexOf(self.export_button) + 1, self.context_button)
        recording_ui.install(self)
        workflow_ui.install_for_panel(self)

    def reset_with_parameters(self, *args, **kwargs):
        # QPushButton.clicked may supply a bool to this flexible wrapper.
        original_reset(self)
        if hasattr(self, '_parameter_records'):
            self._parameter_records.clear()
        saturation_ui.reset(self)
        recording_ui.marker(self, 'curve_reset_or_roi_change')

    def show_value_with_saturation(self):
        total = original_show_value(self)
        saturation_ui.manual_check(self)
        return total

    def clear_source_with_saturation(self):
        original_clear_source(self)
        saturation_ui.clear(self)
        recording_ui.marker(self, 'source_cleared')

    def consume_with_parameters(self, frame, now):
        # Capture retrieval before the original function; association is by identity.
        try:
            captured = capture_metadata.take_capture(frame)
        except Exception as exc:
            captured = {'metadata': {'source': 'metadata_error', 'reason': str(exc), 'nodes': {}}}
        metadata = captured['metadata'] if captured else parameter_log.fallback_metadata(self, frame)
        saturation_ui.before_consume(self, metadata, now)
        before = self.samples[-1] if self.samples else None
        try:
            original_consume(self, frame, now)
        finally:
            self._saturation_in_consume = False
        checked = saturation_ui.after_consume(self, captured, now)
        if not self.samples or self.samples[-1] is before:
            return
        elapsed, total = self.samples[-1]
        record = {'elapsed_s': elapsed, 'sum_dn': total, 'bounds': tuple(self.bounds),
                  'received_utc': parameter_log.utc_now(), 'host_monotonic_s': float(now),
                  'metadata': metadata, 'context': self._experiment_context,
                  'roi_saturation': checked}
        self._parameter_records.append(record)
        recording_ui.append(self, record)
        while len(self._parameter_records) > len(self.samples):
            self._parameter_records.popleft()

    RoiIntensityPanel.__init__ = init_with_export
    RoiIntensityPanel.reset_trace = reset_with_parameters
    RoiIntensityPanel.consume_frame = consume_with_parameters
    RoiIntensityPanel._show_value = show_value_with_saturation
    RoiIntensityPanel.clear_source = clear_source_with_saturation
    RoiIntensityPanel._choose_export_path = choose_export_path
    RoiIntensityPanel.export_curve = export_current_curve
    RoiIntensityPanel._curve_csv_export_installed = True
    camera_modes.install(FlirCameraBackend)
    capture_metadata.install_capture_hooks(FlirCameraBackend)


def freeze_export(panel):
    return parameter_log.freeze_export(panel, capture_metadata)


install()
