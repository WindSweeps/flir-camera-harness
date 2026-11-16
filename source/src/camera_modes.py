"""Verified GenICam mode changes with stop/configure/readback/resume and rollback.

References: FLIR BFS-U3-16S2 AcquisitionControl, DigitalIOControl,
AnalogControl and AutoAlgorithmControl (URLs in README_1.7.txt).
"""
import math

import capture_metadata as metadata


class ConfigurationError(RuntimeError):
    pass


def _node(backend, name):
    node, kind, unused = metadata._find_node(backend, name)
    if node is None or not backend._spin.IsAvailable(node) or not backend._spin.IsReadable(node):
        raise ConfigurationError('{} 不可读取或设备不支持。'.format(name))
    return node, kind


def read(backend, name):
    node, kind = _node(backend, name)
    if kind == 'enumeration':
        return node.GetCurrentEntry(False, True).GetSymbolic()
    return node.GetValue(False, True)


def supported_entry(backend, name, value):
    try:
        node, kind = _node(backend, name)
        entry = node.GetEntryByName(value)
        return kind == 'enumeration' and backend._spin.IsAvailable(entry) and backend._spin.IsReadable(entry)
    except Exception:
        return False


def write(backend, name, value):
    node, kind = _node(backend, name)
    before = read(backend, name)
    if before == value:
        return before  # Fixed-input Line0 can legitimately be read-only.
    if not backend._spin.IsWritable(node):
        raise ConfigurationError('{} 当前不可写，实际值为 {}。'.format(name, before))
    if kind == 'enumeration':
        if not supported_entry(backend, name, value):
            raise ConfigurationError('{} 不支持 {}。'.format(name, value))
        node.SetIntValue(node.GetEntryByName(value).GetValue())
    else:
        if kind in ('float', 'integer'):
            if not math.isfinite(float(value)) or not node.GetMin() <= value <= node.GetMax():
                raise ConfigurationError('{} 超出设备范围 [{}，{}]。'.format(name, node.GetMin(), node.GetMax()))
        node.SetValue(value)
    actual = read(backend, name)
    equal = math.isclose(float(actual), float(value), rel_tol=1e-7, abs_tol=1e-5) if kind == 'float' else actual == value
    if not equal:
        raise ConfigurationError('{} 读回不一致：请求 {}，实际 {}。'.format(name, value, actual))
    return actual


def _stop(backend):
    if backend.streaming:
        try:
            metadata._camera(backend).EndAcquisition()
        except Exception as exc:
            backend.streaming = False
            raise ConfigurationError('停止采集失败，设备状态需重新确认：' + str(exc)) from exc
        backend.streaming = False


def transaction(backend, action, request, configure, log=True):
    """Undo selector-scoped writes in reverse order if any setting fails."""
    with metadata._state(backend)['lock']:
        started = metadata._clock()
        was_streaming = bool(backend.streaming)
        journal = []
        paused = False
        result = None
        def set_value(name, value):
            old = read(backend, name)
            if old != value:
                # Include the attempted write: an exception may follow a partial write.
                journal.append((name, old))
            return write(backend, name, value)
        try:
            _stop(backend)
            paused = True
            result = configure(set_value)
            metadata.invalidate_capture(backend, action)
            if was_streaming:
                backend.start()
            if log:
                metadata._record_event(backend, action, request, started, 'returned', result=result)
            return result
        except Exception as exc:
            errors = []
            if paused:
                try:
                    _stop(backend)
                except Exception as stop_error:
                    errors.append(str(stop_error))
                for name, old in reversed(journal):
                    try:
                        write(backend, name, old)
                    except Exception as rollback_error:
                        errors.append('{}: {}'.format(name, rollback_error))
                metadata.invalidate_capture(backend, action + ':failed')
                if was_streaming and not errors:
                    try:
                        backend.start()
                    except Exception as restart_error:
                        errors.append('恢复采集失败：' + str(restart_error))
            suffix = ('；恢复未完成，采集已停止：' + '; '.join(errors)) if errors else ('；原设置已恢复。' if paused else '')
            failure = ConfigurationError(str(exc) + suffix)
            if log:
                try:
                    metadata._record_event(backend, action, request, started, 'raised', error=failure)
                except Exception:
                    pass
            raise failure from exc


def set_trigger_mode(backend, mode, delay_us=9.0):
    if mode not in ('continuous', 'external'):
        raise ValueError('Unsupported acquisition mode')
    delay_us = float(delay_us)
    if not math.isfinite(delay_us) or delay_us < 0:
        raise ValueError('触发延迟必须为非负有限数值。')
    def configure(set_value):
        # Clear other acquisition gates so "continuous" really is free-running.
        for selector in ('AcquisitionStart', 'FrameBurstStart'):
            if supported_entry(backend, 'TriggerSelector', selector):
                set_value('TriggerSelector', selector)
                set_value('TriggerMode', 'Off')
        set_value('TriggerSelector', 'FrameStart')
        set_value('TriggerMode', 'Off')
        set_value('AcquisitionMode', 'Continuous')
        set_value('ExposureMode', 'Timed')
        if getattr(backend, '_photo_autosave_enabled', False) or getattr(backend, '_photo_buffer_managed', False):
            # Preview may discard old frames; photo acquisition must consume FIFO.
            save_photos = mode == 'external' and getattr(backend, '_photo_autosave_enabled', False)
            set_value('StreamBufferHandlingMode', 'OldestFirst' if save_photos else 'NewestOnly')
        if mode == 'external':
            set_value('LineSelector', 'Line0')
            set_value('LineMode', 'Input')
            set_value('LineInverter', False)
            set_value('TriggerSource', 'Line0')
            set_value('TriggerActivation', 'RisingEdge')
            set_value('TriggerOverlap', 'Off')
            set_value('TriggerDelay', delay_us)
            # The enable write is last, after all settings have read back correctly.
            set_value('TriggerMode', 'On')
        names = ('TriggerSelector', 'TriggerMode', 'AcquisitionMode', 'ExposureMode')
        if getattr(backend, '_photo_autosave_enabled', False) or getattr(backend, '_photo_buffer_managed', False):
            names += ('StreamBufferHandlingMode',)
        if mode == 'external':
            names += ('LineSelector', 'LineMode', 'LineInverter', 'TriggerSource',
                      'TriggerActivation', 'TriggerOverlap', 'TriggerDelay')
        return {'mode': mode, 'readback': {name: read(backend, name) for name in names},
                'scope': 'FrameStart; AcquisitionStart/FrameBurstStart gates disabled when supported'}
    result = transaction(backend, 'set_trigger_mode', {'mode': mode, 'delay_us': delay_us}, configure)
    backend._trigger_configuration = result
    if 'StreamBufferHandlingMode' in result['readback']:
        backend._photo_buffer_managed = True
    backend._last_frame_received_monotonic_s = None
    return result


def auto_brightness(backend, enabled, gain_upper=None):
    """Camera auto algorithm controls both exposure and gain, never a UI feedback loop."""
    desired = 'Continuous' if enabled else 'Off'
    def configure(set_value):
        set_value('ExposureAuto', 'Off')
        set_value('GainAuto', 'Off')
        if enabled:
            set_value('ExposureMode', 'Timed')
            if gain_upper is not None:
                lower = read(backend, 'AutoExposureGainLowerLimit')
                if gain_upper < lower:
                    raise ConfigurationError('自动增益上限不能低于设备下限 {} dB。'.format(lower))
                set_value('AutoExposureGainUpperLimit', float(gain_upper))
            set_value('GainAuto', desired)
            set_value('ExposureAuto', desired)
        result = {'ExposureAuto': read(backend, 'ExposureAuto'), 'GainAuto': read(backend, 'GainAuto')}
        if enabled and gain_upper is not None:
            result['AutoExposureGainUpperLimit'] = read(backend, 'AutoExposureGainUpperLimit')
        return result
    return transaction(backend, 'set_auto_brightness', {'enabled': bool(enabled), 'gain_upper_db': gain_upper}, configure, log=False)


def gain_limit_range(backend):
    node, kind = _node(backend, 'AutoExposureGainUpperLimit')
    if not backend._spin.IsWritable(node):
        raise ConfigurationError('自动增益上限只读。')
    lower = max(float(node.GetMin()), float(read(backend, 'AutoExposureGainLowerLimit')))
    return lower, float(node.GetMax()), float(read(backend, 'AutoExposureGainUpperLimit'))


def install(backend_class):
    if getattr(backend_class, '_mode_controls_installed', False):
        return
    original_exposure = backend_class.set_exposure_us
    original_gain = backend_class.set_gain_db
    def set_auto(self, enabled):
        return auto_brightness(self, bool(enabled), getattr(self, '_auto_gain_upper_requested', None))
    def set_exposure(self, value):
        auto_brightness(self, False)
        return original_exposure(self, value)
    def set_gain(self, value):
        auto_brightness(self, False)
        return original_gain(self, value)
    backend_class.set_exposure_auto = set_auto
    backend_class.set_exposure_us = set_exposure
    backend_class.set_gain_db = set_gain
    backend_class.set_trigger_mode = set_trigger_mode
    backend_class._mode_controls_installed = True
