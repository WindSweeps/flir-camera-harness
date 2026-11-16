"""Frame-associated camera metadata for the reviewed FLIR 1.1 backend.

No pixel arrays or saturation masks are retained or copied here. Full node
readbacks and scalar ROI saturation checks are polled, while image chunks remain
per-frame. The original grab_frame owns display conversion and image release.
Cached node timestamps retain their actual read time; chunks take priority.
"""
import functools
import itertools
import math
import threading
import time
import weakref
from collections import OrderedDict
from datetime import datetime, timezone

import numpy as np
import saturation


NODE_NAMES = tuple("""
ExposureTime ExposureAuto ExposureMode ExposureTimeMode ExposureTimeSelector
AutoExposureExposureTimeLowerLimit AutoExposureExposureTimeUpperLimit
Gain GainAuto GainSelector AutoExposureGainLowerLimit AutoExposureGainUpperLimit
BlackLevel BlackLevelRaw BlackLevelSelector BlackLevelClampingEnable
GammaEnable Gamma AdcBitDepth ADCBitDepth PixelFormat PixelSize PixelColorFilter
PixelDynamicRangeMin PixelDynamicRangeMax Width Height OffsetX OffsetY
WidthMax HeightMax SensorWidth SensorHeight
BinningHorizontal BinningVertical BinningHorizontalMode BinningVerticalMode BinningSelector
DecimationHorizontal DecimationVertical DecimationHorizontalMode DecimationVerticalMode DecimationSelector
ReverseX ReverseY IspEnable LUTEnable LUTSelector SharpeningEnable TestPattern
BalanceWhiteAuto BalanceRatio BalanceRatioSelector
TriggerMode TriggerSelector TriggerSource TriggerActivation TriggerOverlap TriggerDelay
LineSelector LineMode LineInverter LineSource LineFormat
AutoExposureControlPriority AutoExposureTargetGreyValueAuto AutoExposureTargetGreyValue
AcquisitionMode AcquisitionFrameRateEnable AcquisitionFrameRate AcquisitionResultingFrameRate
AcquisitionFrameRateAuto DeviceLinkThroughputLimit DeviceLinkThroughputLimitMode
DeviceTemperature DeviceTemperatureSelector DeviceModelName DeviceSerialNumber DeviceFirmwareVersion
DeviceVersion DeviceVendorName GevTimestampTickFrequency TimestampFrequency
ChunkModeActive ChunkExposureTime ChunkGain
StreamBufferHandlingMode StreamBufferCountManual
""".split())

_UNITS = {
    "ExposureTime": "us", "TriggerDelay": "us", "Gain": "dB",
    "AcquisitionFrameRate": "Hz", "AcquisitionResultingFrameRate": "Hz",
    "DeviceTemperature": "degC", "GevTimestampTickFrequency": "Hz",
    "TimestampFrequency": "Hz", "ChunkExposureTime": "us", "ChunkGain": "dB",
}
for _name in ("Width", "Height", "OffsetX", "OffsetY", "WidthMax", "HeightMax", "SensorWidth", "SensorHeight"):
    _UNITS[_name] = "px"

_CHUNKS = {"ExposureTime": "GetExposureTime", "Gain": "GetGain",
           "FrameID": "GetFrameID", "Timestamp": "GetTimestamp"}
_pending = OrderedDict()
_events = []
_session_windows = {}
_truncated_counts = {}
_event_ids = itertools.count(1)
_session_ids = itertools.count(1)
_registry_lock = threading.RLock()
SNAPSHOT_POLL_SECONDS = 2.0
SATURATION_POLL_SECONDS = 2.0


def _clock():
    return {"utc": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
            "unix_ns": time.time_ns(), "perf_counter_ns": time.perf_counter_ns(),
            "host_monotonic_s": time.perf_counter(), "host_clock": "time.perf_counter"}


def _plain(value):
    """Return JSON-compatible values without accidentally serializing pixels."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, (float, np.floating)):
        number = float(value)
        return number if math.isfinite(number) else str(number)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(v) for v in value]
    return str(value)[:256]


def _error(exc):
    return {"type": type(exc).__name__, "message": str(exc)[:1000]}


def _camera(backend):
    camera = getattr(backend, "camera", None)
    return camera._original if isinstance(camera, _CameraProxy) else camera


def _state(backend):
    state = getattr(backend, "_capture_metadata_state", None)
    if state is None:
        state = {"session_id": "{}-{}".format(time.time_ns(), next(_session_ids)),
                 "camera_identity": None, "nodes": {}, "lock": threading.RLock(),
                 "last_event_id": 0, "emitted_event_id": 0,
                 "chunk_configuration": {"status": "not_attempted"}}
        backend._capture_metadata_state = state
    camera_identity = id(_camera(backend)) if _camera(backend) is not None else None
    if state["camera_identity"] != camera_identity:
        state["nodes"].clear()
        state["camera_identity"] = camera_identity
        state["chunk_configuration"] = {"status": "not_attempted"}
        state["snapshot"] = None
        state["snapshot_reason"] = "camera_changed"
        state["saturation_force"] = "camera_changed"
        state["saturation_checked_at"] = None
        state["saturation_signature"] = None
    return state


def invalidate_capture(backend, reason="camera_setting_change"):
    """Request a full metadata poll and ROI check on the next complete frame."""
    state = _state(backend)
    with state["lock"]:
        state["snapshot"] = None
        state["snapshot_reason"] = str(reason)
        state["saturation_force"] = str(reason)


def _find_node(backend, name):
    state = _state(backend)
    if name in state["nodes"]:
        return state["nodes"][name]
    camera = _camera(backend)
    if camera is None:
        return None, None, None
    spin = backend._spin
    maps = (("GetTLStreamNodeMap",) if name.startswith("StreamBuffer") else
            (("GetNodeMap", "GetTLDeviceNodeMap") if name.startswith("Device") else ("GetNodeMap",)))
    last_error = None
    for map_method in maps:
        try:
            node = getattr(camera, map_method)().GetNode(name)
            if node is None or not spin.IsAvailable(node):
                continue
            interface = node.GetPrincipalInterfaceType()
            for interface_name, constructor, kind in (
                ("intfIFloat", "CFloatPtr", "float"),
                ("intfIInteger", "CIntegerPtr", "integer"),
                ("intfIBoolean", "CBooleanPtr", "boolean"),
                ("intfIEnumeration", "CEnumerationPtr", "enumeration"),
                ("intfIString", "CStringPtr", "string"),
            ):
                if interface == getattr(spin, interface_name, object()):
                    result = (getattr(spin, constructor)(node), kind, map_method)
                    state["nodes"][name] = result
                    return result
            return node, "unsupported", map_method
        except Exception as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    # Do not cache unavailable nodes: access can change with camera settings.
    return None, None, None


def _read_node(backend, name):
    result = {"source": "node_readback", "frame_associated": False, "value": None,
              "read_at": _clock(), "unit": None,
              "association": "read_at_observation; not guaranteed to describe this frame",
              "selector_scope": "current selector only; selectors are not changed for readback"}
    try:
        node, kind, nodemap = _find_node(backend, name)
        result["nodemap"] = nodemap
        if node is None or not backend._spin.IsAvailable(node):
            result["status"] = "unavailable"
            return result
        if not backend._spin.IsReadable(node):
            result["status"] = "unreadable"
            return result
        if kind == "unsupported":
            result["status"] = "unsupported_type"
            return result
        if kind == "enumeration":
            entry = node.GetCurrentEntry(False, True)
            if not backend._spin.IsReadable(entry):
                result["status"] = "unreadable"
                return result
            value = entry.GetSymbolic()
        else:
            value = node.GetValue(False, True)  # IgnoreCache=True, even for dynamic auto modes.
        result.update(status="ok", value=_plain(value), kind=kind)
        if kind in ("float", "integer"):
            try:
                unit = node.GetUnit()
                if unit:
                    result.update(unit=str(unit), unit_source="node.GetUnit")
            except Exception:
                pass
            if result["unit"] is None and name in _UNITS:
                result.update(unit=_UNITS[name], unit_source="standard_feature_convention")
    except Exception as exc:
        result.update(status="read_error", error=_error(exc))
    return result


def read_parameter_snapshot(backend):
    """Read selected camera/transport nodes; never write selector values here."""
    started = _clock()
    nodes = {name: _read_node(backend, name) for name in NODE_NAMES}
    read_times = {name: record.pop("read_at") for name, record in nodes.items()}
    return {"read_started": started, "read_finished": _clock(), "nodes": nodes,
            "node_read_times": read_times,
            "frame_associated": False, "atomic_snapshot": False,
            "scope": "readable whitelist; selector-dependent values describe current selector only"}


def _refresh_snapshot(backend, reason):
    # Once shared with a frame/event, none of this snapshot's objects is mutated.
    state = _state(backend)
    snapshot = read_parameter_snapshot(backend)
    state["snapshot"] = snapshot
    state["snapshot_at"] = snapshot["read_finished"]["host_monotonic_s"]
    state["snapshot_reason"] = reason
    return snapshot


def _snapshot_for_frame(backend):
    state = _state(backend)
    snapshot = state.get("snapshot")
    now = time.perf_counter()
    refreshed = snapshot is None or now - state.get("snapshot_at", 0.0) >= SNAPSHOT_POLL_SECONDS
    if refreshed:
        reason = state.get("snapshot_reason", "initial") if snapshot is None else "interval"
        snapshot = _refresh_snapshot(backend, reason)
    info = {"reused": not refreshed,
            "age_s": max(0.0, time.perf_counter() - state["snapshot_at"]),
            "poll_interval_s": SNAPSHOT_POLL_SECONDS,
            "refresh_reason": state["snapshot_reason"], "fresh_dynamic_nodes": []}
    return snapshot, info


def _dynamic_fallback(backend, snapshot, chunks, cache_info):
    """Only small live exposure/gain readbacks when their frame chunks are absent."""
    nodes, read_times = snapshot["nodes"], snapshot["node_read_times"]
    if not cache_info["reused"]:
        return nodes, read_times
    names = []
    if chunks["ExposureTime"].get("status") != "ok":
        names.extend(("ExposureTime", "ExposureAuto"))
    if chunks["Gain"].get("status") != "ok":
        names.extend(("Gain", "GainAuto"))
    if names:
        # Copy only maps, sharing untouched records. Cached/event snapshots stay
        # immutable, including when auto modes or external manual settings change.
        nodes, read_times = dict(nodes), dict(read_times)
        for name in names:
            record = _read_node(backend, name)
            read_times[name] = record.pop("read_at")
            nodes[name] = record
        cache_info["fresh_dynamic_nodes"] = names
    return nodes, read_times


def _record_event(backend, action, request, started, status, result=None, error=None):
    state = _state(backend)
    event = {"session_id": state["session_id"], "action": action,
             "started_at": started, "completed_at": _clock(), "request": _plain(request),
             "host_monotonic_s": started["host_monotonic_s"],
             "status": status, "result": _plain(result),
             "origin": "backend_method; may be UI or programmatic; not proof of hardware application"}
    if error is not None:
        event["error"] = _error(error)
    try:
        event["readback"] = _refresh_snapshot(backend, "setter:" + action)
    except Exception as exc:
        event["readback"] = {"status": "read_error", "error": _error(exc)}
    with _registry_lock:
        event["id"] = next(_event_ids)
        _events.append(event)
        state["last_event_id"] = event["id"]
        _prune_events()
    return event


def _prune_events():
    # A stopped plot retains its time window. Wall time must not age out its
    # settings, including multiple requests made after its last captured frame.
    # Caller owns _registry_lock. One preceding complete snapshot per session
    # anchors each rolling window. A separate count limit is explicitly reported.
    baseline = {}
    retained = []
    for event in _events:
        latest_frame = _session_windows.get(event["session_id"])
        if latest_frame is not None and event["host_monotonic_s"] < latest_frame - 60.0:
            baseline[event["session_id"]] = event
        else:
            retained.append(event)
    retained.extend(baseline.values())
    retained.sort(key=lambda event: event["id"])
    excess = max(0, len(retained) - 10000)
    for event in retained[:excess]:
        session = event["session_id"]
        _truncated_counts[session] = _truncated_counts.get(session, 0) + 1
    _events[:] = retained[excess:]


def get_parameter_events(since_id=0, backend=None):
    """Last captured 60 s plus baseline/later requests; stopped windows stay intact."""
    session = _state(backend)["session_id"] if backend is not None else None
    with _registry_lock:
        _prune_events()
        return [_plain(event) for event in _events
                if event["id"] > since_id and (session is None or event["session_id"] == session)]


def get_event_history_status(backend=None):
    """Report the count ceiling so any loss can also be reported after stopping."""
    session = _state(backend)["session_id"] if backend is not None else None
    with _registry_lock:
        return {"retention": "last captured 60 seconds per session plus baseline and all later requests",
                "event_count_limit": 10000,
                "truncated_count": _truncated_counts.get(session, 0) if session is not None else sum(_truncated_counts.values()),
                "truncated_by_session": dict(_truncated_counts) if session is None else {session: _truncated_counts.get(session, 0)}}


def _configure_chunks(backend):
    """Best effort before BeginAcquisition; failures never suppress normal capture."""
    state = _state(backend)
    report = {"attempted_at": _clock(), "status": "attempted", "fields": {}}
    state["chunk_configuration"] = report
    old_selector = None
    selector = None
    try:
        mode, _, _ = _find_node(backend, "ChunkModeActive")
        if mode is None or not backend._spin.IsWritable(mode):
            report["status"] = "unavailable"
            report["reason"] = "ChunkModeActive not writable before start"
            return
        mode.SetValue(True)
        report["mode_readback"] = _read_node(backend, "ChunkModeActive")
        report["mode_readback"].pop("read_at", None)
        selector, _, _ = _find_node(backend, "ChunkSelector")
        if selector is None or not backend._spin.IsWritable(selector):
            report["status"] = "unavailable"
            report["reason"] = "ChunkSelector not writable"
            return
        try:
            old_selector = selector.GetCurrentEntry(False, True).GetSymbolic()
        except Exception as exc:
            report["selector_initial_read_error"] = _error(exc)
        for name in _CHUNKS:
            field = {"status": "unavailable"}
            report["fields"][name] = field
            try:
                entry = selector.GetEntryByName(name)
                if not backend._spin.IsAvailable(entry) or not backend._spin.IsReadable(entry):
                    continue
                selector.SetIntValue(entry.GetValue())
                enabled, _, _ = _find_node(backend, "ChunkEnable")
                if enabled is None or not backend._spin.IsWritable(enabled):
                    continue
                enabled.SetValue(True)
                actual = enabled.GetValue(False, True)
                field.update(status="enabled" if actual else "not_enabled", readback=bool(actual))
            except Exception as exc:
                field.update(status="enable_error", error=_error(exc))
    except Exception as exc:
        report.update(status="enable_error", error=_error(exc))
    finally:
        for name in _CHUNKS:
            report["fields"].setdefault(name, {"status": "unavailable"})
        if selector is not None and old_selector is not None:
            try:
                entry = selector.GetEntryByName(old_selector)
                selector.SetIntValue(entry.GetValue())
                report["selector_restored"] = True
            except Exception as exc:
                report["selector_restored"] = False
                report["selector_restore_error"] = _error(exc)


def _read_image_info(image):
    result = {}
    for name, method, unit in (
        ("FrameID", "GetFrameID", None), ("Timestamp", "GetTimeStamp", "ns"),
        ("PixelFormat", "GetPixelFormatName", None), ("PixelFormatCode", "GetPixelFormat", None),
        ("Width", "GetWidth", "px"), ("Height", "GetHeight", "px"),
        ("BitsPerPixel", "GetBitsPerPixel", "bit"),
    ):
        record = {"source": "image_api." + method, "frame_associated": True, "unit": unit, "value": None}
        try:
            record.update(status="ok", value=_plain(getattr(image, method)()))
        except AttributeError:
            record["status"] = "unavailable"
        except Exception as exc:
            record.update(status="read_error", error=_error(exc))
        result[name] = record
    return result


def _read_chunks(backend, image, nodes):
    state = _state(backend)
    result = {name: {"status": "unavailable", "source": "image_chunk",
                     "frame_associated": True, "unit": None, "value": None} for name in _CHUNKS}
    try:
        if not image.HasChunkData():
            return result
        chunk = image.GetChunkData()  # Read all scalars before the original Release.
    except Exception as exc:
        return {name: dict(record, status="read_error", error=_error(exc)) for name, record in result.items()}
    for name, method in _CHUNKS.items():
        record = result[name]
        config = state["chunk_configuration"].get("fields", {}).get(name, {})
        # Do not mistake a default zero from an unsupported chunk accessor for data.
        if config.get("status") != "enabled":
            record["reason"] = "chunk enablement not confirmed"
            continue
        try:
            record.update(status="ok", value=_plain(getattr(chunk, method)()), accessor=method)
            unit_info = nodes.get("Chunk" + name, {})
            if unit_info.get("unit"):
                record.update(unit=unit_info["unit"], unit_source=unit_info.get("unit_source"))
            elif name in ("ExposureTime", "Gain"):
                unit_info = nodes.get(name, {})
                record.update(unit=unit_info.get("unit") or _UNITS[name],
                              unit_source="corresponding_feature_unit")
            elif name == "Timestamp":
                record.update(unit="device_timestamp_units_unspecified", unit_source="not_reported")
        except Exception as exc:
            record.update(status="read_error", error=_error(exc))
    return result


def _conversion_description(array):
    """Describe the original backend algorithm; retain no reference to array."""
    channel = array[:, :, 0] if array.ndim == 3 else array
    description = {"implementation": "original_backend_1.1_unchanged",
                   "input_dtype": str(array.dtype), "input_shape": list(array.shape),
                   "selected_channel": 0 if array.ndim == 3 else None,
                   "channel_policy": "first_channel" if array.ndim == 3 else "single_plane",
                   "pixel_values_retained": False}
    if channel.dtype == np.uint8:
        description["scaling"] = {"method": "none"}
    else:
        is_integer = np.issubdtype(channel.dtype, np.integer)
        denominator = float(np.iinfo(channel.dtype).max) if is_integer else float(np.nanmax(channel) or 1)
        description["scaling"] = {
            "method": "dtype_max" if is_integer else "per_frame_nanmax_or_one",
            "denominator": _plain(denominator),
            "operation": "clip(channel.astype(float32) * (255 / denominator), 0, 255).astype(uint8)",
        }
    return description


def _sdk_version(backend):
    state = _state(backend)
    if "sdk_version" not in state:
        record = {"source": "existing_system.GetLibraryVersion", "status": "unavailable"}
        try:
            system = getattr(backend, "system", None)
            if system is not None:
                version = system.GetLibraryVersion()
                parts = {name: int(getattr(version, name)) for name in ("major", "minor", "type", "build")}
                record.update(status="ok", components=parts,
                              value="{major}.{minor}.{type}.{build}".format(**parts))
        except Exception as exc:
            record.update(status="read_error", error=_error(exc))
        state["sdk_version"] = record
    return _plain(state["sdk_version"])


def _saturation_for_frame(backend, array, metadata):
    """Return a scalar check only when due; retain no frame/mask/result cache."""
    state = _state(backend)
    request = getattr(backend, "_roi_saturation_request", None)
    if not isinstance(request, dict) or request.get("bounds") is None:
        return None
    try:
        requested = tuple(request["bounds"])
        bounds = tuple(int(value) for value in requested)
        if len(bounds) != 4 or requested != bounds or array.ndim < 2:
            return None
        x0, y0, x1, y1 = bounds
        if not (0 <= x0 < x1 <= array.shape[1] and 0 <= y0 < y1 <= array.shape[0]):
            return None
        signature = (request.get("generation"), bounds)
    except (TypeError, ValueError, OverflowError):
        return None
    now = time.perf_counter()
    last_checked = state.get("saturation_checked_at")
    if signature != state.get("saturation_signature"):
        trigger = "roi_generation"
    elif state.get("saturation_force"):
        trigger = state["saturation_force"]
    elif last_checked is None or now - last_checked >= SATURATION_POLL_SECONDS:
        trigger = "interval"
    else:
        return None
    checked = _clock()
    try:
        result = saturation.check_roi(array, metadata, bounds)
        if not isinstance(result, dict) or "mask" in result:
            raise TypeError("ROI saturation must return scalar JSON metadata only")
        def reject_pixels(value):
            if isinstance(value, np.ndarray):
                raise TypeError("ROI saturation must not retain pixel arrays")
            if isinstance(value, dict):
                for item in value.values():
                    reject_pixels(item)
            elif isinstance(value, (tuple, list)):
                for item in value:
                    reject_pixels(item)
        reject_pixels(result)
        result = _plain(result)
    except Exception as exc:
        result = {"status": "unknown", "reason": str(exc)}
    result.update(checked=True, checked_host_monotonic_s=checked["host_monotonic_s"],
                  checked_utc=checked["utc"], bounds=list(bounds), trigger=trigger)
    state["saturation_checked_at"] = checked["host_monotonic_s"]
    state["saturation_signature"] = signature
    state["saturation_force"] = None
    return result


def _photo_session(backend):
    if getattr(backend, '_trigger_configuration', {}).get('mode') == 'external':
        return getattr(backend, '_photo_session', None)
    return None


class _ImageProxy:
    def __init__(self, image, backend, holder):
        self._original, self._backend, self._holder = image, backend, holder

    def __getattr__(self, name):
        return getattr(self._original, name)

    def IsIncomplete(self):
        incomplete = self._original.IsIncomplete()
        session = _photo_session(self._backend)
        if incomplete and session is not None:
            raise session.reject('相机返回不完整图像，未保存此帧；采集已停止。')
        return incomplete

    def GetNDArray(self, *args, **kwargs):
        try:
            array = self._original.GetNDArray(*args, **kwargs)
        except Exception as exc:
            session = _photo_session(self._backend)
            if session is not None:
                raise session.reject('读取原始照片失败：' + str(exc)) from exc
            raise
        # All failures below are metadata failures, never changes to display/Release.
        try:
            backend = self._backend
            state = _state(backend)
            captured = _clock()
            snapshot, cache_info = _snapshot_for_frame(backend)
            self._holder["did_full_poll"] = not cache_info["reused"]
            chunks = _read_chunks(backend, self._original, snapshot["nodes"])
            nodes, read_times = _dynamic_fallback(backend, snapshot, chunks, cache_info)
            image_info = _read_image_info(self._original)
            effective = {}
            for name in ("ExposureTime", "Gain", "PixelFormat", "Width", "Height", "FrameID", "Timestamp"):
                selected = chunks.get(name)
                if selected is None or selected.get("status") != "ok":
                    selected = image_info.get(name)
                if selected is None or selected.get("status") != "ok":
                    selected = nodes.get(name, {"status": "unavailable", "value": None})
                effective[name] = selected
            metadata = {"schema_version": 1, "source": "camera", "session_id": state["session_id"],
                        "captured_host": captured, "nodes": nodes, "readback_cache": cache_info,
                        "node_read_times": read_times, "sdk_version": _sdk_version(backend),
                        "image_retrieved_at": self._holder.get("image_retrieved_at"),
                        "readback_window": {key: snapshot[key] for key in ("read_started", "read_finished", "atomic_snapshot", "scope")},
                        "chunks": chunks, "image_info": image_info, "effective_parameters": effective,
                        "chunk_configuration": state["chunk_configuration"],
                        "parameter_event_id": state["last_event_id"],
                        "parameter_events": (get_parameter_events(state["emitted_event_id"], backend)
                                             if state["last_event_id"] > state["emitted_event_id"] else []),
                        "event_history": get_event_history_status(backend),
                        "calibration": {"status": "not_supplied", "absolute_radiometric_units": False}}
            try:
                metadata["conversion"] = _conversion_description(array)
            except Exception as exc:
                metadata["conversion"] = {"status": "read_error", "error": _error(exc)}
            self._holder["metadata"] = metadata
        except Exception as exc:
            self._holder["metadata"] = {"schema_version": 1, "source": "camera", "status": "read_error", "error": _error(exc)}
        # Only compute on the requested ROI, before display conversion/Release.
        # Neither a full-frame mask nor a previous result is retained here.
        try:
            self._holder["saturation"] = _saturation_for_frame(self._backend, array, self._holder["metadata"])
        except Exception as exc:
            self._holder["saturation"] = {"status": "unknown", "reason": str(exc)}
        session = _photo_session(self._backend)
        if session is not None:
            # Saving faults deliberately propagate to the original acquisition
            # error handler. Release still runs in original grab_frame.finally.
            context = getattr(self._backend, '_photo_context', lambda: {})()
            session.offer(array, self._holder['metadata'], context, self._holder.get('saturation'))
        return array


class _CameraProxy:
    def __init__(self, camera, backend, holder):
        self._original, self._backend, self._holder = camera, backend, holder

    def __getattr__(self, name):
        return getattr(self._original, name)

    def GetNextImage(self, *args, **kwargs):
        try:
            image = self._original.GetNextImage(*args, **kwargs)
        except Exception as exc:
            session = _photo_session(self._backend)
            code = getattr(exc, 'errorcode', None)
            if session is not None and code is not None and code != getattr(self._backend._spin, 'SPINNAKER_ERR_TIMEOUT', -1011):
                raise session.fail('相机取图失败：' + str(exc)) from exc
            raise
        self._holder["image_retrieved_at"] = _clock()
        return _ImageProxy(image, self._backend, self._holder)


def _publish(frame, payload):
    key = id(frame)
    def expired(reference):
        with _registry_lock:
            existing = _pending.get(key)
            if existing is not None and existing[0] is reference:
                _pending.pop(key, None)
    reference = weakref.ref(frame, expired)
    with _registry_lock:
        _pending[key] = (reference, payload)
        while len(_pending) > 8:
            _pending.popitem(last=False)


def take_capture(frame):
    """Consume metadata for this exact returned ndarray; replay/other arrays give None."""
    with _registry_lock:
        item = _pending.get(id(frame))
        if item is None or item[0]() is not frame:
            return None
        _pending.pop(id(frame), None)
        return item[1]


def install_capture_hooks(backend_class):
    """Add observations to the reviewed backend without replacing grab_frame logic."""
    if getattr(backend_class, "_capture_metadata_installed", False):
        return
    original_grab = backend_class.grab_frame

    @functools.wraps(original_grab)
    def grab_frame(self, *args, **kwargs):
        state = _state(self)
        with state["lock"]:
            original_camera = _camera(self)
            if original_camera is None or not getattr(self, "streaming", False):
                return original_grab(self, *args, **kwargs)
            holder = {"acquisition_started": _clock()}
            proxy = _CameraProxy(original_camera, self, holder)
            self.camera = proxy
            try:
                frame = original_grab(self, *args, **kwargs)
            finally:
                if self.camera is proxy:
                    self.camera = original_camera
            if frame is not None and holder.get("metadata") is not None:
                try:
                    metadata = holder["metadata"]
                    metadata["acquisition_window"] = {"started": holder["acquisition_started"], "finished": _clock(),
                                                      "scope": "host grab_frame call including retrieval, metadata reads, display conversion and release"}
                    metadata.setdefault("conversion", {}).update(output_dtype=str(frame.dtype), output_shape=list(frame.shape))
                    _publish(frame, {"metadata": metadata, "saturation": holder.get("saturation")})
                    self._last_frame_received_monotonic_s = time.perf_counter()
                    state["emitted_event_id"] = state["last_event_id"]
                    with _registry_lock:
                        _session_windows[state["session_id"]] = time.perf_counter()
                        if holder.get("did_full_poll"):
                            _prune_events()
                except Exception:
                    pass
            return frame

    backend_class.grab_frame = grab_frame
    for method_name, unit in (("set_exposure_us", "us"), ("set_gain_db", "dB"), ("set_exposure_auto", None)):
        original = getattr(backend_class, method_name, None)
        if original is None:
            continue
        def wrap_setter(method, name, request_unit):
            @functools.wraps(method)
            def setter(self, *args, **kwargs):
                with _state(self)["lock"]:
                    invalidate_capture(self, "camera_setting:" + name)
                    started = _clock()
                    request = {"args": _plain(args), "kwargs": _plain(kwargs), "unit": request_unit}
                    try:
                        result = method(self, *args, **kwargs)
                    except Exception as exc:
                        try:
                            _record_event(self, name, request, started, "raised", error=exc)
                        except Exception:
                            pass
                        raise
                    try:
                        _record_event(self, name, request, started, "returned", result=result)
                    except Exception:
                        pass
                    return result
            return setter
        setattr(backend_class, method_name, wrap_setter(original, method_name, unit))

    original_start = backend_class.start
    @functools.wraps(original_start)
    def start(self, *args, **kwargs):
        with _state(self)["lock"]:
            if not getattr(self, "streaming", False):
                invalidate_capture(self, "start")
                try:
                    _configure_chunks(self)
                except Exception as exc:
                    _state(self)["chunk_configuration"] = {"status": "enable_error", "error": _error(exc)}
            return original_start(self, *args, **kwargs)
    backend_class.start = start

    original_close = getattr(backend_class, "close", None)
    if original_close is not None:
        @functools.wraps(original_close)
        def close(self, *args, **kwargs):
            state = _state(self)
            with state["lock"]:
                # Drop native node references before original DeInit/ReleaseInstance.
                state["nodes"].clear()
                state["camera_identity"] = None
                state["snapshot"] = None
                return original_close(self, *args, **kwargs)
        backend_class.close = close
    backend_class._capture_metadata_installed = True
