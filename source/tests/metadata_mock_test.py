"""No-hardware tests of metadata hooks, including the original display boundary."""
import json
import types

import numpy as np


def run_metadata_tests(metadata=None):
    if metadata is None:
        import capture_metadata as metadata

    class SpinError(Exception):
        pass

    class Entry:
        available = readable = True
        def __init__(self, value):
            self.value = value
        def GetSymbolic(self):
            return self.value
        def GetValue(self):
            return self.value

    class Node:
        available = readable = writable = True
        def __init__(self, kind, value, unit=""):
            self.kind, self.value, self.unit = kind, value, unit
            self.reads = []
        def GetPrincipalInterfaceType(self):
            return self.kind
        def GetValue(self, verify=False, ignore_cache=False):
            self.reads.append((verify, ignore_cache))
            if isinstance(self.value, Exception):
                raise self.value
            return self.value
        def SetValue(self, value):
            self.value = value
        def GetUnit(self):
            return self.unit
        def GetCurrentEntry(self, verify=False, ignore_cache=False):
            self.reads.append((verify, ignore_cache))
            return Entry(self.value)
        def GetEntryByName(self, name):
            return Entry(name)
        def SetIntValue(self, value):
            self.value = value

    class Spin:
        SpinnakerException = SpinError
        intfIFloat, intfIInteger, intfIBoolean, intfIEnumeration, intfIString = range(5)
        casts = 0
        @staticmethod
        def IsAvailable(node):
            return node is not None and node.available
        @staticmethod
        def IsReadable(node):
            return node is not None and node.readable
        @staticmethod
        def IsWritable(node):
            return node is not None and node.writable
        @staticmethod
        def cast(node):
            Spin.casts += 1
            return node
        CFloatPtr = CIntegerPtr = CBooleanPtr = CEnumerationPtr = CStringPtr = cast

    class Camera:
        def __init__(self):
            self.nodes = {
                "ExposureTime": Node(Spin.intfIFloat, 20000.0, "us"),
                "ExposureAuto": Node(Spin.intfIEnumeration, "Off"),
                "Gain": Node(Spin.intfIFloat, 2.0, "dB"),
                "ChunkModeActive": Node(Spin.intfIBoolean, False),
                "ChunkSelector": Node(Spin.intfIEnumeration, "Timestamp"),
                "ChunkEnable": Node(Spin.intfIBoolean, False),
                "DeviceModelName": Node(Spin.intfIString, "mock camera"),
                "GammaEnable": Node(Spin.intfIBoolean, True),
                "Width": Node(Spin.intfIInteger, 2, "px"),
                "DeviceTemperature": Node(Spin.intfIFloat, SpinError("unreadable sensor")),
            }
            self.nodes["Width"].readable = False
            self.image = None
            self.node_lookups = {}
        def GetNodeMap(self):
            return self
        def GetTLDeviceNodeMap(self):
            return self
        def GetNode(self, name):
            self.node_lookups[name] = self.node_lookups.get(name, 0) + 1
            return self.nodes.get(name)
        def GetNextImage(self, timeout):
            if isinstance(self.image, Exception):
                raise self.image
            return self.image

    class CountedArray(np.ndarray):
        copies = 0
        def copy(self, *args, **kwargs):
            CountedArray.copies += 1
            return np.asarray(self).copy(*args, **kwargs)

    class Image:
        def __init__(self, pixels, incomplete=False, chunks=True):
            self.array = np.asarray(pixels).view(CountedArray)
            self.incomplete, self.chunks = incomplete, chunks
            self.releases, self.ndarray_calls = 0, 0
        def IsIncomplete(self):
            return self.incomplete
        def GetNDArray(self):
            self.ndarray_calls += 1
            return self.array
        def Release(self):
            self.releases += 1
        def HasChunkData(self):
            return self.chunks
        def GetChunkData(self):
            return types.SimpleNamespace(GetExposureTime=lambda: 19000.0, GetGain=lambda: 1.75,
                                         GetFrameID=lambda: 123, GetTimestamp=lambda: 888)
        def GetFrameID(self):
            return 123
        def GetTimeStamp(self):
            return 888
        def GetPixelFormatName(self):
            return "Mono16"
        def GetPixelFormat(self):
            return 17301505
        def GetWidth(self):
            return self.array.shape[1]
        def GetHeight(self):
            return self.array.shape[0]
        def GetBitsPerPixel(self):
            return self.array.dtype.itemsize * 8

    class Backend:
        def __init__(self):
            self.camera, self._spin, self.streaming = Camera(), Spin, False
            self.system = types.SimpleNamespace(GetLibraryVersion=lambda: types.SimpleNamespace(major=4, minor=3, type=0, build=189))
            self.start_error = False
        def start(self):
            if self.start_error:
                raise RuntimeError("start failure")
            self.streaming = True
            return "original start return"
        def close(self):
            self.camera = None
            return "original close return"
        def grab_frame(self, timeout_ms=10):
            if not self.streaming:
                return None
            image = None
            try:
                image = self.camera.GetNextImage(int(max(1, timeout_ms)))
                if image.IsIncomplete():
                    return None
                frame = image.GetNDArray().copy()
                if frame.ndim == 3:
                    frame = frame[:, :, 0]
                if frame.dtype != np.uint8:
                    info = np.iinfo(frame.dtype) if np.issubdtype(frame.dtype, np.integer) else None
                    scale = float(info.max) if info is not None else float(np.nanmax(frame) or 1)
                    frame = np.clip(frame.astype(np.float32) * (255 / scale), 0, 255).astype(np.uint8)
                return frame
            except SpinError:
                return None
            finally:
                if image is not None:
                    image.Release()
        def set_exposure_us(self, value):
            if value < 0:
                raise ValueError("invalid exposure")
            self.camera.nodes["ExposureTime"].value = value
            return value
        def set_gain_db(self, value):
            self.camera.nodes["Gain"].value = value
            return value
        def set_exposure_auto(self, enabled):
            self.camera.nodes["ExposureAuto"].value = "Continuous" if enabled else "Off"

    metadata.install_capture_hooks(Backend)
    wrapped = Backend.grab_frame
    metadata.install_capture_hooks(Backend)
    assert Backend.grab_frame is wrapped, "installation must be idempotent"
    backend = Backend()
    original_camera = backend.camera
    assert backend.start() == "original start return"
    assert backend.camera.nodes["ChunkSelector"].value == "Timestamp", "restore chunk selector"
    pixels = np.array([[[0, 123], [65535, 42]], [[32768, 11], [1000, 22]]], dtype=np.uint16)
    backend.camera.image = image = Image(pixels)
    CountedArray.copies = 0
    frame = backend.grab_frame()
    assert image.releases == 1 and image.ndarray_calls == 1 and CountedArray.copies == 1
    assert backend.camera is original_camera
    assert np.array_equal(frame, np.array([[0, 255], [127, 3]], dtype=np.uint8)), "original display conversion"
    assert metadata.take_capture(frame.copy()) is None, "equal pixels are not frame identity"
    payload = metadata.take_capture(frame)
    assert metadata.take_capture(frame) is None, "single consumption"
    assert set(payload) == {"metadata", "saturation"}, "only metadata and transient saturation mask"
    meta = payload["metadata"]
    assert meta["source"] == "camera"
    assert meta["effective_parameters"]["ExposureTime"]["value"] == 19000
    assert meta["nodes"]["ExposureTime"]["value"] == 20000
    assert meta["effective_parameters"]["ExposureTime"]["frame_associated"] is True
    assert meta["nodes"]["ExposureTime"]["frame_associated"] is False
    assert meta["nodes"]["Width"]["status"] == "unreadable"
    assert meta["nodes"]["AdcBitDepth"]["status"] == "unavailable" and meta["nodes"]["AdcBitDepth"]["value"] is None
    assert meta["nodes"]["DeviceTemperature"]["status"] == "read_error"
    assert "read_at" not in meta["nodes"]["Gain"]
    assert meta["sdk_version"]["value"] == "4.3.0.189"
    assert meta["acquisition_window"]["started"]["host_monotonic_s"] <= meta["acquisition_window"]["finished"]["host_monotonic_s"]
    json.dumps(meta, allow_nan=False)
    # Only requested ROI summaries survive Release; no mask is retained.
    saturation_pixels = np.array([[254, 255], [0, 42]], dtype=np.uint8)
    original_camera.image = saturation_image = Image(saturation_pixels)
    saturation_image.GetPixelFormatName = lambda: "Mono8"
    backend._roi_saturation_request = {'bounds': (0, 0, 2, 2), 'generation': 1}
    saturation_frame = backend.grab_frame()
    saturation_payload = metadata.take_capture(saturation_frame)
    assert saturation_image.releases == 1
    saturated = saturation_payload['saturation']
    assert saturated['status'] == 'ok' and saturated['count'] == 1 and saturated['threshold'] == 255
    saturation_image.array[:] = 0
    assert saturation_payload['saturation']['count'] == 1 and 'mask' not in saturated
    json.dumps(saturation_payload['metadata'], allow_nan=False)
    lookup_count = original_camera.node_lookups["ExposureTime"]
    original_camera.nodes["ExposureTime"].value = 40000.0
    original_camera.image = Image(np.array([[10, 20]], dtype=np.uint8), chunks=False)
    frame2 = backend.grab_frame()
    meta2 = metadata.take_capture(frame2)["metadata"]
    assert meta2["effective_parameters"]["ExposureTime"]["value"] == 40000
    assert meta2["chunks"]["ExposureTime"]["status"] == "unavailable"
    assert original_camera.node_lookups["ExposureTime"] == lookup_count, "cache pointer"
    assert all(ignore_cache for _, ignore_cache in original_camera.nodes["ExposureTime"].reads), "never cache dynamic values"
    assert meta["nodes"]["ExposureTime"]["value"] == 20000, "old snapshot unchanged"
    baseline = metadata.get_parameter_events(backend=backend)
    assert backend.set_exposure_us(20000) == 20000
    assert backend.set_gain_db(6) == 6
    backend.set_exposure_auto(True)
    try:
        backend.set_exposure_us(-1)
    except ValueError:
        pass
    else:
        raise AssertionError("setter failure swallowed")
    events = metadata.get_parameter_events(backend=backend)[len(baseline):]
    assert len(events) == 4 and events[-1]["status"] == "raised"
    assert len({e["id"] for e in events}) == 4
    assert events[0]["readback"]["nodes"]["ExposureTime"]["value"] == 20000
    assert events[2]["readback"]["nodes"]["ExposureAuto"]["value"] == "Continuous"
    original_camera.image = incomplete = Image(np.array([[1]], dtype=np.uint8), incomplete=True)
    assert backend.grab_frame() is None and incomplete.releases == 1 and incomplete.ndarray_calls == 0
    original_camera.image = SpinError("timeout")
    assert backend.grab_frame() is None and backend.camera is original_camera
    failing = Backend()
    failing.start_error = True
    try:
        failing.start()
    except RuntimeError:
        pass
    else:
        raise AssertionError("original start failure swallowed")
    partial = Backend()
    selector = partial.camera.nodes["ChunkSelector"]
    set_selector = selector.SetIntValue
    def fail_gain_only(value):
        if value == "Gain":
            raise SpinError("unsupported Gain chunk")
        return set_selector(value)
    selector.SetIntValue = fail_gain_only
    assert partial.start() == "original start return"
    fields = partial._capture_metadata_state["chunk_configuration"]["fields"]
    assert fields["ExposureTime"]["status"] == "enabled" and fields["Gain"]["status"] == "enable_error"
    assert fields["FrameID"]["status"] == "enabled" and fields["Timestamp"]["status"] == "enabled"
    original_camera.image = Image(np.array([[1]], dtype=np.uint8))
    old_snapshot = metadata.read_parameter_snapshot
    try:
        def broken_snapshot(_backend):
            raise RuntimeError("metadata failure")
        metadata.read_parameter_snapshot = broken_snapshot
        metadata.invalidate_capture(backend, 'test_injected_failure')
        frame3 = backend.grab_frame()
        assert np.array_equal(frame3, [[1]]) and original_camera.image.releases == 1
        assert metadata.take_capture(frame3)["metadata"]["status"] == "read_error"
    finally:
        metadata.read_parameter_snapshot = old_snapshot
    assert backend.close() == "original close return"
    assert not backend._capture_metadata_state["nodes"], "release native cached pointers before close"
    saved_events = list(metadata._events)
    saved_windows = dict(metadata._session_windows)
    saved_truncated = dict(metadata._truncated_counts)
    try:
        def event(number, at, session="retention-test"):
            return {"id": number, "session_id": session, "host_monotonic_s": at}
        metadata._events[:] = [event(1, 100), event(2, 120), event(3, 170), event(4, 230), event(5, 300)]
        metadata._session_windows.clear()
        metadata._session_windows["retention-test"] = 220
        metadata._prune_events()
        assert [e["id"] for e in metadata._events] == [2, 3, 4, 5], "window baseline plus post-stop changes"
        # Calling get later uses capture time, not the current wall/perf time.
        assert [e["id"] for e in metadata.get_parameter_events()] == [2, 3, 4, 5]
        metadata._events[:] = [event(i, 300) for i in range(10002)]
        metadata._session_windows.clear()
        metadata._truncated_counts.clear()
        metadata._prune_events()
        assert len(metadata._events) == 10000
        assert metadata.get_event_history_status()["truncated_count"] == 2, "explicit count ceiling disclosure"
    finally:
        metadata._events[:] = saved_events
        metadata._session_windows.clear()
        metadata._session_windows.update(saved_windows)
        metadata._truncated_counts.clear()
        metadata._truncated_counts.update(saved_truncated)
    return {"status": "passed", "checks": "identity, no pixel retention/copy, Release, conversion, chunks, live nodes, unknowns, history, errors, SDK and clocks"}


if __name__ == "__main__":
    import pathlib
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
    print(json.dumps(run_metadata_tests()))
