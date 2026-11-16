"""Low-rate ROI checks and small, sticky summaries; never cache pixel arrays."""
import copy
import time
from datetime import datetime, timezone
from PyQt5 import QtWidgets
import saturation

INTERVAL_S = 2.0


def unchecked(reason='此帧未抽检，不能作为未过曝的证据。'):
    return {'status': 'not_checked', 'checked': False, 'saturated': None, 'reason': reason}


def empty_history():
    return {'detected': False, 'detection_count': 0, 'first': None, 'last': None,
            'scope': 'current ROI since last reset; may precede retained 60-second curve',
            'check_interval_s': INTERVAL_S}


def init(panel):
    panel._saturation_history = empty_history()
    panel._saturation_state = unchecked()
    panel._saturation_metadata = {}
    panel._saturation_last_check = float('-inf')
    panel._saturation_generation = 0
    panel._saturation_in_consume = False
    panel._saturation_frame_time = None
    panel._saturation_backend = None
    panel._saturation_replay_settings = None
    panel._saturation_manual_key = None
    panel.saturation_label = QtWidgets.QLabel(panel)
    panel.saturation_label.setObjectName('roiSaturationLabel')
    panel.saturation_label.setWordWrap(True)
    panel.layout().insertWidget(2, panel.saturation_label)
    render(panel)


def request(panel):
    panel._saturation_generation += 1
    backend = getattr(panel.window(), 'backend', None)
    panel._saturation_backend = backend
    if backend is not None:
        backend._roi_saturation_request = {
            'bounds': tuple(panel.bounds) if panel.bounds is not None else None,
            'generation': panel._saturation_generation}


def accept(panel, state, now, trigger):
    state = dict(state)
    state.setdefault('checked_host_monotonic_s', time.perf_counter())
    state.update(checked=True, displayed_frame_host_monotonic_s=panel._saturation_frame_time,
                 trigger=state.get('trigger', trigger))
    panel._saturation_last_check = now
    panel._saturation_state = state
    callback = getattr(panel, '_record_saturation', None)
    if callback is not None:
        callback(state)
    if state.get('saturated') is True:
        history = panel._saturation_history
        history['detected'] = True
        history['detection_count'] += 1
        if history['first'] is None:
            history['first'] = copy.deepcopy(state)
        history['last'] = copy.deepcopy(state)
    render(panel)
    return state


def local_check(panel, now, trigger):
    if panel.frame is None or panel.bounds is None:
        panel._saturation_state = {'status': 'no_roi', 'checked': False, 'saturated': None}
        render(panel)
        return unchecked()
    meta = panel._saturation_metadata
    backend = getattr(panel.window(), 'backend', None)
    replay = backend is None or type(backend).__name__ == 'ImageReplayBackend'
    # With no raw-pixel cache, a transformed camera preview cannot prove the
    # original code. Request a check of the next raw frame instead.
    conversion = meta.get('conversion', {})
    if not replay and not (conversion.get('input_dtype') == 'uint8'
                           and conversion.get('scaling', {}).get('method') == 'none'
                           and conversion.get('selected_channel') is None):
        panel._saturation_state = unchecked('等待下一原始帧检查；停止时不从缩放后的预览推测。')
        render(panel)
        return unchecked()
    try:
        state = saturation.check_roi(panel.frame, meta, panel.bounds, replay=replay)
    except Exception as exc:
        state = {'status': 'unknown', 'saturated': None, 'reason': str(exc)}
    state['checked_utc'] = datetime.now(timezone.utc).isoformat()
    return accept(panel, state, now, trigger)


def reset(panel):
    if not hasattr(panel, 'saturation_label'):
        return
    panel._saturation_history = empty_history()
    panel._saturation_last_check = float('-inf')
    panel._saturation_state = unchecked()
    panel._saturation_manual_key = None
    editing = getattr(panel, '_editing', False) or getattr(panel, '_draw_start', None) is not None
    if editing:
        render(panel)
        return
    if panel._saturation_in_consume:
        request(panel)
    else:
        manual_check(panel, request_new=False)


def manual_check(panel, request_new=True):
    if not hasattr(panel, 'saturation_label') or panel._saturation_in_consume:
        return
    if getattr(panel, '_editing', False) or getattr(panel, '_draw_start', None) is not None:
        return
    key = (id(panel.frame), tuple(panel.bounds) if panel.bounds else None)
    if key == panel._saturation_manual_key:
        return
    panel._saturation_manual_key = key
    request(panel)
    now = panel._saturation_frame_time
    local_check(panel, time.perf_counter() if now is None else now, 'roi_or_clear')


def clear(panel):
    panel._saturation_metadata = {}
    panel._saturation_frame_time = None
    panel._saturation_manual_key = None
    panel._saturation_history = empty_history()
    panel._saturation_state = {'status': 'no_roi', 'checked': False, 'saturated': None}
    panel._saturation_last_check = float('-inf')
    request(panel)
    render(panel)


def before_consume(panel, metadata, now):
    panel._saturation_in_consume = True
    panel._saturation_metadata = metadata
    panel._saturation_frame_time = float(now)


def after_consume(panel, captured, now):
    panel._saturation_in_consume = False
    backend = getattr(panel.window(), 'backend', None)
    if backend is not panel._saturation_backend:
        request(panel)
    if panel.bounds is None:
        panel._saturation_state = {'status': 'no_roi', 'checked': False, 'saturated': None}
        render(panel)
        return unchecked()
    state = captured.get('saturation') if captured else None
    if state and state.get('checked') and tuple(state.get('bounds') or ()) == tuple(panel.bounds):
        return accept(panel, state, float(now), 'camera')
    if backend is None or type(backend).__name__ == 'ImageReplayBackend':
        settings = tuple(getattr(backend, name, None) for name in ('exposure', 'gain_db', 'automatic'))
        changed = settings != panel._saturation_replay_settings
        panel._saturation_replay_settings = settings
        if changed or now - panel._saturation_last_check >= INTERVAL_S:
            return local_check(panel, float(now), 'replay_setting' if changed else 'interval')
    return unchecked()


def render(panel):
    if not hasattr(panel, 'saturation_label'):
        return
    state, history = panel._saturation_state, panel._saturation_history
    if panel.bounds is None:
        text, fg, bg, border = '过曝检测：待框选统计区域', '#555555', '#f3f4f6', '#d1d5db'
    elif state.get('saturated'):
        text = '过曝！最近检测 {:,} / {:,} 像素触顶（{:.3f}%） · 上限 {} DN'.format(
            state['count'], state['total'], state['fraction'] * 100, state['threshold'])
        fg, bg, border = '#991b1b', '#fef2f2', '#dc2626'
    elif history['detected']:
        text, fg, bg, border = '本次选区曾检测到过曝 · 已记录，保存前将再次确认', '#854d0e', '#fffbeb', '#d97706'
    elif state.get('status') == 'ok':
        text, fg, bg, border = '最近检测未见过曝 · 每 2 秒抽检，框选/清空/改参数时检查', '#166534', '#f0fdf4', '#86a788'
    else:
        text, fg, bg, border = '过曝状态未知 / 等待检测 · 每 2 秒抽检', '#854d0e', '#fffbeb', '#d97706'
    label = panel.saturation_label
    if label.text() != text:
        label.setText(text)
    style = 'QLabel#roiSaturationLabel {{ color: {}; background: {}; border: 1px solid {}; border-radius: 5px; padding: 5px 8px; font-weight: bold; }}'.format(fg, bg, border)
    if label.styleSheet() != style:
        label.setStyleSheet(style)
    label.setToolTip('显示最近一次抽检；未抽检帧不推定为正常。短暂过曝可能漏检。\n'
                     '过曝记录保留至重新框选、清空曲线或切换图像源；可能早于当前60秒窗口。\n'
                     '不保留原始像素/掩码缓存；高位深相机在下一原始帧检查。\n'
                     + str(state.get('reason') or ''))
