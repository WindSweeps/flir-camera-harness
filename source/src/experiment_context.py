"""Session-only, user-entered experiment context; no radiometric correction.

Importing this module constructs no widgets and touches no camera or files.
File references are hashed only when the user saves the dialog. A hash records
file identity, not validity of a calibration or application of a correction.
"""
import copy
import hashlib
import math
import os
from datetime import datetime, timezone
from pathlib import Path

from PyQt5 import QtWidgets


_NUMBER_FIELDS = (
    ("wavelength_nm", "波长（nm）"),
    ("sensor_pixel_pitch_um_x", "传感器像元尺寸 X（µm）"),
    ("sensor_pixel_pitch_um_y", "传感器像元尺寸 Y（µm）"),
    ("magnification", "成像倍率"),
    ("effective_output_pixel_size_um_x", "物方有效输出像素尺度 X（µm/px）"),
    ("effective_output_pixel_size_um_y", "物方有效输出像素尺度 Y（µm/px）"),
    ("nd_transmission_fraction", "ND 透过率（大于 0，且不超过 1）"),
)
_TEXT_FIELDS = (
    ("reference_plane", "参考平面"),
    ("optical_path_description", "光路说明"),
    ("calibration_source", "标定 / 校准资料来源"),
    ("dark_reference_path", "暗场文件完整路径（仅关联）"),
    ("flat_reference_path", "平场文件完整路径（仅关联）"),
)


def get_default_context():
    """Return a fresh JSON-safe context with unknown information left null."""
    context = {key: None for key, _ in _NUMBER_FIELDS + _TEXT_FIELDS}
    context.update({
        "notes": None,
        "source": None,
        "updated_utc": None,
        "calibration_status": "uncalibrated",
        "dark_correction_applied": False,
        "flat_correction_applied": False,
    })
    for prefix in ("dark_reference", "flat_reference"):
        for suffix in ("resolved_path", "sha256", "hash_status", "hash_error"):
            context[prefix + "_" + suffix] = None
    return context


def _parse_number(text, key, label):
    text = text.strip()
    if not text:
        return None
    try:
        value = float(text)
    except (ValueError, OverflowError):
        raise ValueError("{}：请输入有效数字，或留空表示未知。".format(label))
    if not math.isfinite(value) or value <= 0:
        raise ValueError("{}：必须是大于 0 的有限数字。".format(label))
    if key == "nd_transmission_fraction" and value > 1:
        raise ValueError("ND 透过率必须大于 0 且不超过 1；例如 10% 应填写 0.1。")
    return value


def _reference_hash(path_text):
    """Record provenance without loading an image or interpreting its pixels."""
    result = {"resolved_path": None, "sha256": None,
              "hash_status": "not_provided", "hash_error": None}
    if not path_text:
        return result
    try:
        path = Path(path_text).expanduser().resolve()
        result["resolved_path"] = str(path)
        if not path.exists():
            result.update(hash_status="not_found", hash_error="文件不存在。")
            return result
        if not path.is_file():
            result.update(hash_status="not_file", hash_error="该路径不是普通文件。")
            return result
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            while True:
                block = stream.read(1024 * 1024)
                if not block:
                    break
                digest.update(block)
            after = os.fstat(stream.fileno())
        current = path.stat()
        signature = lambda item: (item.st_size, item.st_mtime_ns, item.st_ino)
        if signature(before) != signature(after) or signature(after) != signature(current):
            result.update(hash_status="changed_during_read",
                          hash_error="读取期间文件发生变化；未记录 SHA-256。")
            return result
        result.update(sha256=digest.hexdigest(), hash_status="sha256_computed")
    except (OSError, ValueError, RuntimeError) as exc:
        result.update(sha256=None, hash_status="read_error", hash_error=str(exc))
    return result


def edit_context(panel):
    """Edit panel._experiment_context; return True only after an accepted save.

    Cancel leaves the previous object untouched. Values are descriptive user
    input, and no pixel scale, intensity or optical power is calculated.
    """
    previous = getattr(panel, "_experiment_context", None)
    context = get_default_context()
    if isinstance(previous, dict):
        context.update(copy.deepcopy(previous))

    dialog = QtWidgets.QDialog(panel)
    dialog.setWindowTitle("实验信息（用于后续标定）")
    dialog.resize(760, 680)
    layout = QtWidgets.QVBoxLayout(dialog)
    explanation = QtWidgets.QLabel(
        "这些信息由用户填写，仅在当前会话中记录，用于后续标定。"
        "当前光强仍以 DN 表示，不进行物理量换算，不应用暗场或平场校正。\n"
        "空白项表示未知；物方像素尺度需填写已有标定值，不会由像元尺寸或倍率推算。\n"
        "保存后的信息关联后续新采样，不回填已有曲线。停止后填写的信息仅作为导出时补充说明。", dialog)
    explanation.setWordWrap(True)
    layout.addWidget(explanation)

    scroll = QtWidgets.QScrollArea(dialog)
    scroll.setWidgetResizable(True)
    body = QtWidgets.QWidget(scroll)
    form = QtWidgets.QFormLayout(body)
    form.setFieldGrowthPolicy(QtWidgets.QFormLayout.AllNonFixedFieldsGrow)
    editors = {}
    for key, label in _NUMBER_FIELDS + _TEXT_FIELDS:
        editor = QtWidgets.QLineEdit(body)
        editor.setObjectName("experiment_" + key)
        value = context.get(key)
        editor.setText("" if value is None else str(value))
        editor.setPlaceholderText("未知可留空")
        if key.endswith("_reference_path"):
            editor.setPlaceholderText("填写完整文件路径；保存时计算 SHA-256，不应用校正")
        editors[key] = editor
        form.addRow(label, editor)
    notes = QtWidgets.QPlainTextEdit(body)
    notes.setObjectName("experiment_notes")
    notes.setPlainText("" if context.get("notes") is None else str(context["notes"]))
    notes.setPlaceholderText("可记录 ND 透过率的测量波长、光路变化、资料版本等。")
    notes.setMaximumHeight(90)
    form.addRow("备注", notes)
    scroll.setWidget(body)
    layout.addWidget(scroll)

    buttons = QtWidgets.QDialogButtonBox(
        QtWidgets.QDialogButtonBox.Save | QtWidgets.QDialogButtonBox.Cancel, parent=dialog)
    buttons.button(QtWidgets.QDialogButtonBox.Save).setText("保存到当前会话")
    buttons.button(QtWidgets.QDialogButtonBox.Cancel).setText("取消")
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    pending = {}
    warnings = []

    def save():
        draft = copy.deepcopy(context)
        try:
            for key, label in _NUMBER_FIELDS:
                draft[key] = _parse_number(editors[key].text(), key, label)
        except ValueError as exc:
            QtWidgets.QMessageBox.warning(dialog, "实验信息尚未保存", str(exc))
            editors[key].setFocus()
            return
        for key, _ in _TEXT_FIELDS:
            draft[key] = editors[key].text().strip() or None
        draft["notes"] = notes.toPlainText().strip() or None
        warnings.clear()
        for prefix, label in (("dark_reference", "暗场"), ("flat_reference", "平场")):
            identity = _reference_hash(draft[prefix + "_path"])
            for suffix, value in identity.items():
                draft[prefix + "_" + suffix] = value
            if identity["hash_status"] not in ("not_provided", "sha256_computed"):
                warnings.append("{}：{}".format(label, identity["hash_error"]))
        draft.update(source="user_entered_not_verified",
                     updated_utc=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                     calibration_status="uncalibrated",
                     dark_correction_applied=False, flat_correction_applied=False)
        pending["context"] = draft
        dialog.accept()

    buttons.accepted.connect(save)
    if dialog.exec_() != QtWidgets.QDialog.Accepted or "context" not in pending:
        return False
    panel._experiment_context = pending["context"]
    if warnings:
        QtWidgets.QMessageBox.warning(
            panel, "实验信息已保存，部分文件未能核验",
            "文件路径已记录，失败项的 SHA-256 留空；未应用任何校正。\n\n" + "\n".join(warnings))
    return True
