"""Detect output-code saturation before display scaling; retain only a bool mask.

PixelDynamicRangeMax describes the largest output component (SFNC/FLIR). A
model-specific fallback is deliberately restricted to BFS-U3-16S2: its reference
documents left-aligned Mono16/Bayer16 ADC output. Other FLIR models can differ;
for example FFY Mono16 depends on ISP, so ADC bit count alone is insufficient.

References checked 2026-11-10:
https://softwareservices.flir.com/BFS-U3-16S2/latest/Model/public/ImageFormatControl.html
https://softwareservices.flir.com/FFY-U3-16S2/latest/Model/public/ImageFormatControl.html
"""
import re

import numpy as np


_MONO8 = {"Mono8", "BayerRG8", "BayerGR8", "BayerGB8", "BayerBG8"}
_MONO16 = {"Mono16", "BayerRG16", "BayerGR16", "BayerGB16", "BayerBG16"}
_RGB8 = {"RGB8", "RGB8Packed", "BGR8", "BGR8Packed"}
_RGBA8 = {"RGBa8", "RGBa8Packed", "BGRa8", "BGRa8Packed"}


def _value(mapping, name):
    record = mapping.get(name, {}) if isinstance(mapping, dict) else {}
    if isinstance(record, dict) and record.get("status") == "ok":
        return record.get("value")
    return None


def _format(value):
    if not isinstance(value, str):
        return None
    return value.removeprefix("PixelFormat_")


def _integer(value):
    # Camera nodes are integer; reject bools, strings and nonintegral floats.
    if isinstance(value, (int, np.integer)) and not isinstance(value, (bool, np.bool_)):
        return int(value)
    return None


def _adc_bits(nodes):
    value = _value(nodes, "AdcBitDepth")
    if value is None:
        value = _value(nodes, "ADCBitDepth")
    if isinstance(value, str):
        match = re.fullmatch(r"(?:AdcBitDepth_|ADCBitDepth_)?Bit(8|10|12|14|16)", value)
        return int(match.group(1)) if match else None
    value = _integer(value)
    return value if value in (8, 10, 12, 14, 16) else None


def _unknown(result, reason):
    result.update(status="unknown", reason=reason)
    result.pop("mask", None)
    return result


def prepare_frame(array, metadata, replay=False):
    """Return scalar provenance and an independent 2D bool ``mask`` when known.

    Call with the original GetNDArray result before image Release, never with a
    camera frame normalized for display. No original pixel array is retained.
    RGB/BGR count a pixel once if any color component clips; alpha is excluded.
    Packed, signed, floating and unknown layouts are intentionally not guessed.
    ``mask`` must stay outside exported/deep-copied parameter metadata.
    """
    result = {"status": "unknown", "threshold": None, "source": None,
              "reason": None, "channel_policy": None, "pixel_format": None,
              "shape": None, "comparison": "value == upper_limit",
              "unit": "raw_output_DN", "raw_pixels_retained": False,
              "scope": "output_code_saturation; not a sensor full-well measurement"}
    if not isinstance(array, np.ndarray) or array.ndim not in (2, 3) or array.size == 0:
        return _unknown(result, "没有可判定的图像数组。")
    result["shape"] = [int(array.shape[0]), int(array.shape[1])]
    metadata = metadata if isinstance(metadata, dict) else {}
    nodes = metadata.get("nodes", {})
    image_info = metadata.get("image_info", {})
    fmt = _format(_value(image_info, "PixelFormat"))
    node_fmt = _format(_value(nodes, "PixelFormat"))
    fmt_source = "image_api.PixelFormat"
    if fmt is None:
        fmt = _format(_value(metadata.get("effective_parameters", {}), "PixelFormat")) or node_fmt
        fmt_source = "parameter_readback.PixelFormat"
    result.update(pixel_format=fmt, format_source=fmt_source)

    if replay:
        if array.ndim != 2 or array.dtype != np.uint8:
            return _unknown(result, "回放图像不是已确认的 8 位灰度数据。")
        result.update(threshold=255, source="replay_uint8_full_scale", channel_policy="single_plane",
                      pixel_format="replay_gray8", format_source="replay_array")
        values = array
    else:
        if array.dtype.kind != "u":
            return _unknown(result, "原始数据不是受支持的无符号整数；不使用显示归一化的 255 作为上限。")
        if fmt in _MONO8 | _MONO16:
            expected_bytes = 1 if fmt in _MONO8 else 2
            if array.ndim != 2 or array.dtype.itemsize != expected_bytes:
                return _unknown(result, "像素格式与原始数组的位宽或通道布局不一致。")
            values = array
            result["channel_policy"] = "single_plane_bayer_sites" if fmt.startswith("Bayer") else "single_plane"
        elif fmt in _RGB8 | _RGBA8:
            expected_channels = 4 if fmt in _RGBA8 else 3
            if array.ndim != 3 or array.shape[2] != expected_channels or array.dtype.itemsize != 1:
                return _unknown(result, "彩色像素格式与原始数组的通道布局不一致。")
            values = array[:, :, :3]
            result["channel_policy"] = "any_rgb_component_alpha_ignored" if fmt in _RGBA8 else "any_rgb_component"
        else:
            return _unknown(result, "当前像素格式的有效上限或解包布局未确认，无法判定过曝。")

        for field, actual in (("Width", array.shape[1]), ("Height", array.shape[0])):
            declared = _integer(_value(image_info, field))
            if declared is not None and declared != actual:
                return _unknown(result, "图像尺寸与原始数组不一致，无法对应当前选区。")

        # An image's format describes this frame. Live node values may already
        # describe another format after a setting change, so do not mix them.
        range_value = _value(nodes, "PixelDynamicRangeMax")
        if fmt_source == "image_api.PixelFormat" and node_fmt is not None and fmt != node_fmt:
            return _unknown(result, "帧格式与相机参数读回不一致，无法确认该帧的上限。")
        if range_value is not None:
            upper = _integer(range_value)
            if upper is None or not 0 < upper <= int(np.iinfo(values.dtype).max):
                return _unknown(result, "相机报告的像素上限无效或超出原始数组位宽。")
            result.update(threshold=upper, source="node_readback.PixelDynamicRangeMax")
        elif fmt in _MONO8 | _RGB8 | _RGBA8:
            result.update(threshold=255, source="pixel_format_8bit_component_full_scale")
        else:
            # Do not extrapolate one model's alignment to all Spinnaker cameras.
            model = _value(nodes, "DeviceModelName")
            bits = _adc_bits(nodes)
            if not isinstance(model, str) or not re.fullmatch(r"BFS-U3-16S2(?:M|C)?(?:-CS|-C|-BD)?", model) or bits is None:
                return _unknown(result, "16 位格式缺少可靠的有效上限；不能仅按容器位宽或 ADC 位数推测。")
            shift = 16 - bits
            upper = ((1 << bits) - 1) << shift
            result.update(threshold=upper, source="BFS-U3-16S2_documented_left_aligned_ADC",
                          adc_bits=bits, alignment="left", padding_bits=shift)
            if shift and bool(np.any(np.bitwise_and(values, (1 << shift) - 1))):
                return _unknown(result, "实际像素低位与该型号的 ADC 左对齐规则不一致。")

    # A value beyond a purported maximum indicates mismatched metadata or an
    # unconfirmed representation. Do not incorrectly label all high data clipped.
    if bool(np.any(values > result["threshold"])):
        return _unknown(result, "原始像素超过所报告的上限，数据与参数的对应关系未确认。")
    mask = values == result["threshold"]
    if mask.ndim == 3:
        mask = np.any(mask, axis=2)
    result.update(status="ok", reason=None, mask=mask)
    return result


def summarize_roi(payload, bounds, shape):
    """Summarize the current selection; the result is JSON-safe and has no mask."""
    payload = payload if isinstance(payload, dict) else {}
    result = {key: payload.get(key) for key in (
        "threshold", "source", "channel_policy", "pixel_format", "format_source",
        "comparison", "unit", "scope", "adc_bits", "alignment", "padding_bits")}
    result.update(status="unknown", saturated=None, count=None, total=None,
                  fraction=None, reason=payload.get("reason"), bounds=None)
    if bounds is None:
        result.update(status="no_roi", reason="请先选择统计区域。")
        return result
    try:
        if len(bounds) != 4 or any(_integer(value) is None for value in bounds):
            raise ValueError("invalid bounds")
        x0, y0, x1, y1 = map(int, bounds)
        result["bounds"] = [x0, y0, x1, y1]
        if x1 <= x0 or y1 <= y0:
            result.update(status="no_roi", reason="当前选区为空。")
            return result
        total = (x1 - x0) * (y1 - y0)
        result["total"] = total
        if shape is None or len(shape) != 2:
            raise ValueError("missing shape")
        height, width = map(int, shape)
        if x0 < 0 or y0 < 0 or x1 > width or y1 > height:
            raise ValueError("bounds outside frame")
    except (TypeError, ValueError, OverflowError):
        result["reason"] = "当前选区与图像尺寸不匹配。"
        return result
    mask = payload.get("mask")
    if payload.get("status") != "ok":
        result["reason"] = result["reason"] or "该帧缺少可靠的过曝判定数据。"
        return result
    if not isinstance(mask, np.ndarray) or mask.dtype != np.bool_ or mask.shape != (height, width):
        result["reason"] = "过曝掩码与当前图像不匹配。"
        return result
    count = int(np.count_nonzero(mask[y0:y1, x0:x1]))
    result.update(status="ok", saturated=count > 0, count=count, fraction=count / total,
                  reason=None)
    return result


def check_roi(array, metadata, bounds, replay=False):
    """Check only the selected pixels and return scalars, with no retained mask.

    ``bounds`` are (x0, y0, x1, y1) in the full raw frame, end exclusive. The
    caller chooses when to sample; this function does not read clocks or cache
    previous results. All pixel comparisons operate on an ROI view. A temporary
    boolean mask exists only within this call and is absent from its result.
    """
    shape = array.shape[:2] if isinstance(array, np.ndarray) and array.ndim in (2, 3) else None
    result = summarize_roi(None, bounds, shape)
    result.update(checked=True, check_scope="roi_only")
    if bounds is None or result["status"] == "no_roi":
        return result
    if not isinstance(array, np.ndarray) or array.ndim not in (2, 3) or array.size == 0:
        result["reason"] = "没有可判定的图像数组。"
        return result
    # Revalidate explicitly; unknown also describes a valid selection without
    # a payload, so summarize_roi's status alone cannot distinguish bad bounds.
    try:
        if len(bounds) != 4 or any(_integer(value) is None for value in bounds):
            raise ValueError("invalid bounds")
        x0, y0, x1, y1 = map(int, bounds)
        height, width = map(int, shape)
        if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
            raise ValueError("bounds outside frame")
    except (TypeError, ValueError, OverflowError):
        result["reason"] = "当前选区与图像尺寸不匹配。"
        return result

    metadata = metadata if isinstance(metadata, dict) else {}
    image_info = metadata.get("image_info", {})
    image_info = image_info if isinstance(image_info, dict) else {}
    for field, actual in (("Width", width), ("Height", height)):
        declared = _integer(_value(image_info, field))
        if declared is not None and declared != actual:
            result["reason"] = "图像尺寸与原始数组不一致，无法对应当前选区。"
            return result

    # Width/Height describe the full image. After checking them above, adapt a
    # local shallow copy to the cropped view, without changing caller metadata.
    roi_metadata = dict(metadata)
    roi_info = dict(image_info)
    for field, value in (("Width", x1 - x0), ("Height", y1 - y0)):
        entry = image_info.get(field, {})
        record = dict(entry) if isinstance(entry, dict) else {}
        record.update(status="ok", value=value)
        roi_info[field] = record
    roi_metadata["image_info"] = roi_info
    roi = array[y0:y1, x0:x1]
    payload = prepare_frame(roi, roi_metadata, replay=replay)
    result = summarize_roi(payload, (0, 0, x1 - x0, y1 - y0), roi.shape[:2])
    result.update(bounds=[x0, y0, x1, y1], checked=True, check_scope="roi_only")
    return result
