"""Deterministic no-hardware tests of polling, invalidation and scalar payloads."""
import json
import time as real_time
import types

import numpy as np


def run_capture_performance_tests(module=None):
    if module is None:
        import capture_metadata as module
    originals = {name: getattr(module, name) for name in (
        "time", "read_parameter_snapshot", "_read_node", "_read_chunks", "_read_image_info",
        "_configure_chunks", "_sdk_version", "_prune_events", "saturation")}
    now = [100.0]
    counts = {"full": 0, "dynamic": 0, "chunks": 0, "saturation": 0, "prune": 0,
              "copies": 0, "release": 0, "arrays": 0}
    saved_events = list(module._events)
    saved_windows = dict(module._session_windows)
    saved_truncated = dict(module._truncated_counts)
    retained_frames = []

    def node(value, unit=None):
        return {"status": "ok", "value": value, "unit": unit,
                "source": "node_readback", "frame_associated": False}

    def full_snapshot(backend):
        counts["full"] += 1
        clock = module._clock()
        nodes = {"ExposureTime": node(backend.exposure, "us"), "Gain": node(backend.gain, "dB"),
                 "ExposureAuto": node(backend.auto), "GainAuto": node("Off"),
                 "Gamma": node(backend.gamma), "PixelFormat": node("Mono8")}
        return {"read_started": clock, "read_finished": clock, "nodes": nodes,
                "node_read_times": {name: clock for name in nodes},
                "atomic_snapshot": False, "scope": "mock full snapshot"}

    def dynamic_node(backend, name):
        counts["dynamic"] += 1
        values = {"ExposureTime": backend.exposure, "Gain": backend.gain,
                  "ExposureAuto": backend.auto, "GainAuto": "Off"}
        return dict(node(values[name]), read_at=module._clock())

    def chunks(backend, image, nodes):
        counts["chunks"] += 1
        return {name: {"status": "ok" if backend.has_chunks else "unavailable",
                       "value": value if backend.has_chunks else None,
                       "source": "image_chunk", "frame_associated": True}
                for name, value in (("ExposureTime", backend.exposure), ("Gain", backend.gain),
                                    ("FrameID", counts["chunks"]), ("Timestamp", int(now[0] * 1e9)))}

    checked_arrays = []
    def check_roi(array, metadata, bounds):
        counts["saturation"] += 1
        checked_arrays.append(id(array))
        x0, y0, x1, y1 = bounds
        return {"status": "ok", "count": int(np.count_nonzero(array[y0:y1, x0:x1] == 255)),
                "threshold": 255, "ratio": 0.25}

    def prune():
        counts["prune"] += 1
        return originals["_prune_events"]()

    class CountedArray(np.ndarray):
        def copy(self, *args, **kwargs):
            counts["copies"] += 1
            return np.asarray(self).copy(*args, **kwargs)

    class Image:
        def __init__(self):
            self.array = np.array([[255, 17], [0, 42]], dtype=np.uint8).view(CountedArray)
        def GetNDArray(self):
            counts["arrays"] += 1
            return self.array
        def Release(self):
            counts["release"] += 1

    class Camera:
        def GetNextImage(self, timeout):
            self.image = Image()
            return self.image

    class Backend:
        def __init__(self):
            self.camera = Camera()
            self.streaming = False
            self.exposure, self.gain, self.auto, self.gamma = 20000.0, 1.0, "Off", 1.0
            self.has_chunks = True
        def start(self):
            self.streaming = True
            return "start-result"
        def close(self):
            self.streaming = False
            return "close-result"
        def set_exposure_us(self, value):
            if value < 0:
                raise ValueError("invalid request")
            self.exposure = value
            return value
        def set_gain_db(self, value):
            self.gain = value
            return value
        def set_exposure_auto(self, enabled):
            self.auto = "Continuous" if enabled else "Off"
        def grab_frame(self):
            image = self.camera.GetNextImage(100)
            try:
                return image.GetNDArray().copy()
            finally:
                image.Release()

    def frame(backend, advance=0.01):
        now[0] += advance
        array = backend.grab_frame()
        retained_frames.append(array)
        payload = module.take_capture(array)
        assert module.take_capture(array) is None
        assert module.take_capture(array.copy()) is None
        json.dumps(payload, allow_nan=False)
        return payload

    try:
        module.time = types.SimpleNamespace(perf_counter=lambda: now[0], perf_counter_ns=lambda: int(now[0] * 1e9),
                                           time_ns=real_time.time_ns)
        module.read_parameter_snapshot = full_snapshot
        module._read_node = dynamic_node
        module._read_chunks = chunks
        module._read_image_info = lambda _image: {"PixelFormat": dict(node("Mono8"), source="image_api", frame_associated=True)}
        module._configure_chunks = lambda _backend: None
        module._sdk_version = lambda _backend: {"status": "ok", "value": "mock"}
        module._prune_events = prune
        module.saturation = types.SimpleNamespace(check_roi=check_roi)
        module._events.clear()
        module._session_windows.clear()
        module._truncated_counts.clear()
        module.install_capture_hooks(Backend)
        backend = Backend()
        assert backend.start() == "start-result"
        first = frame(backend)
        assert first["saturation"] is None and counts["saturation"] == 0
        assert first["metadata"]["readback_cache"]["reused"] is False
        first_frozen = json.dumps(first, sort_keys=True)
        for _ in range(30):
            current = frame(backend)
            assert current["metadata"]["nodes"] is first["metadata"]["nodes"]
            assert current["metadata"]["node_read_times"] == first["metadata"]["node_read_times"]
        assert counts["full"] == 1 and counts["dynamic"] == 0 and counts["chunks"] == 31
        assert counts["prune"] == 1, "never scan/prune event history on ordinary frames"
        assert current["metadata"]["readback_cache"]["age_s"] > 0.2

        backend._roi_saturation_request = {"bounds": (0, 0, 2, 2), "generation": 1}
        checked = frame(backend)
        assert checked["saturation"]["count"] == 1 and checked["saturation"]["checked"] is True
        assert checked["saturation"]["bounds"] == [0, 0, 2, 2]
        assert checked["saturation"]["trigger"] == "roi_generation"
        assert checked_arrays[-1] == id(backend.camera.image.array), "check original pixels before Release"
        scalar_frozen = json.dumps(checked["saturation"], sort_keys=True)
        backend.camera.image.array[:] = 0
        assert json.dumps(checked["saturation"], sort_keys=True) == scalar_frozen
        for _ in range(20):
            assert frame(backend)["saturation"] is None
        assert counts["saturation"] == 1
        backend._roi_saturation_request["generation"] += 1
        assert frame(backend)["saturation"]["checked"] is True, "same bounds reselection must recheck"
        backend._roi_saturation_request = {"bounds": None, "generation": 3}
        before_clear = counts["saturation"]
        assert frame(backend)["saturation"] is None and counts["saturation"] == before_clear
        backend._roi_saturation_request = {"bounds": (1, 0, 2, 2), "generation": 4}
        assert frame(backend)["saturation"]["count"] == 0
        before_interval = counts["saturation"]
        refreshed = frame(backend, advance=2.01)
        assert refreshed["saturation"]["trigger"] == "interval"
        assert counts["saturation"] == before_interval + 1 and counts["full"] == 2

        before_setter = counts["full"]
        assert backend.set_exposure_us(40000) == 40000
        assert counts["full"] == before_setter + 1, "setter immediate full readback"
        after_setter = frame(backend)
        assert counts["full"] == before_setter + 1, "reuse setter snapshot rather than double poll"
        assert after_setter["saturation"]["trigger"].startswith("camera_setting:")
        assert after_setter["metadata"]["effective_parameters"]["ExposureTime"]["value"] == 40000
        after_setter_frozen = json.dumps(after_setter, sort_keys=True)
        assert frame(backend)["saturation"] is None
        backend.has_chunks = False
        backend.exposure = 25000
        dynamic_before = counts["dynamic"]
        fallback = frame(backend)
        assert counts["dynamic"] == dynamic_before + 4
        assert fallback["metadata"]["nodes"]["ExposureTime"]["value"] == 25000
        assert fallback["metadata"]["nodes"]["Gamma"] is after_setter["metadata"]["nodes"]["Gamma"]
        assert fallback["metadata"]["node_read_times"]["ExposureTime"] != after_setter["metadata"]["node_read_times"]["ExposureTime"]
        assert fallback["metadata"]["node_read_times"]["Gamma"] == after_setter["metadata"]["node_read_times"]["Gamma"]
        assert json.dumps(after_setter, sort_keys=True) == after_setter_frozen
        backend.auto = "Continuous"
        backend.exposure = 27000
        auto = frame(backend)
        assert auto["metadata"]["nodes"]["ExposureTime"]["value"] == 27000
        assert auto["metadata"]["nodes"]["ExposureAuto"]["value"] == "Continuous"
        before_invalidation = counts["full"]
        module.invalidate_capture(backend, "test_configuration_change")
        invalidated = frame(backend)
        assert counts["full"] == before_invalidation + 1
        assert invalidated["saturation"]["trigger"] == "test_configuration_change"
        assert json.dumps(first, sort_keys=True) == first_frozen, "published caches remain immutable"
        try:
            backend.set_exposure_us(-1)
        except ValueError:
            pass
        else:
            raise AssertionError("original setter error swallowed")
        failure_event = module.get_parameter_events(backend=backend)[-1]
        assert failure_event["status"] == "raised" and failure_event["readback"]["nodes"]["ExposureTime"]["value"] == 27000
        assert frame(backend)["saturation"]["checked"] is True
        backend.streaming = False
        backend.start()
        assert frame(backend)["saturation"]["trigger"] == "start"
        assert counts["copies"] == counts["arrays"] == counts["release"], "no added copies or lost release"
        assert all("saturation" not in key or key in ("saturation_signature", "saturation_checked_at", "saturation_force")
                   for key in backend._capture_metadata_state), "no saturation frame/mask cache"
        assert backend.close() == "close-result"
        return {"status": "passed", "frames": counts["arrays"], "counts": counts,
                "checks": "2s polling, immediate triggers, no ROI/no work, per-frame chunks, four-node fallback, immutable snapshots, no masks/copies"}
    finally:
        for name, value in originals.items():
            setattr(module, name, value)
        module._events[:] = saved_events
        module._session_windows.clear()
        module._session_windows.update(saved_windows)
        module._truncated_counts.clear()
        module._truncated_counts.update(saved_truncated)


if __name__ == "__main__":
    import pathlib
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
    print(json.dumps(run_capture_performance_tests()))
