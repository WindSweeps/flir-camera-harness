"""Hardware-free checks of the real ROI widget and its saturation indicator."""


def run_saturation_tests(namespace):
    import copy
    import json
    import traceback
    from pathlib import Path

    import numpy as np
    import pyqtgraph as pg
    from PyQt5 import QtCore, QtWidgets
    from roi_intensity import RoiIntensityPanel
    import saturation

    output = Path.cwd() / "_saturation_test_output"
    output.mkdir(parents=True, exist_ok=True)
    result = {"status": "running", "hardware_access": False, "cases": [], "artifacts": {}}
    capture = namespace["capture_metadata"]
    original_take_capture = capture.take_capture
    pending = {}
    clock = [1000.0]
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = None
    panel = None

    def check(condition, message):
        if not condition:
            raise AssertionError(message)

    def passed(name):
        result["cases"].append({"name": name, "status": "passed"})

    def take_fake_capture(frame):
        item = pending.pop(id(frame), None)
        if item is None:
            return None
        check(item[0] is frame, "Fake capture frame identity mismatch")
        return item[1]

    def feed(frame, payload=None):
        clock[0] += 0.1
        if payload is not None:
            pending[id(frame)] = (frame, payload)
        image_view.setImage(frame, autoLevels=False, levels=(0, 255))
        panel.consume_frame(frame, clock[0])
        check(id(frame) not in pending, "Frame-associated saturation was not consumed")
        app.processEvents()
        return copy.deepcopy(panel._saturation_state)

    def set_roi(bounds):
        x0, y0, x1, y1 = bounds
        panel.roi.setPos((x0, y0))
        panel.roi.setSize((x1 - x0, y1 - y0))
        panel.roi.show()
        panel._region_changed()
        app.processEvents()
        check(panel.bounds == bounds, "Real ROI bounds did not update")

    def assert_status(saturated, count, total=20):
        state = panel._saturation_state
        check(state["status"] == "ok", "Expected known saturation state: " + repr(state))
        check(state["saturated"] is saturated, "Incorrect saturation state: " + repr(state))
        check(state["count"] == count and state["total"] == total,
              "Wrong ROI pixel counts: " + repr(state))
        if saturated:
            check("过曝" in panel.saturation_label.text()
                  and "未检测到" not in panel.saturation_label.text(),
                  "Saturated ROI does not display a warning")
        else:
            check("未检测到过曝" in panel.saturation_label.text(),
                  "Unsaturated ROI did not clear its warning")

    def screenshot(name):
        app.processEvents()
        path = output / (name + ".png")
        check(window.grab().save(str(path)), "Could not save UI screenshot")
        result["artifacts"][name] = str(path)

    try:
        capture.take_capture = take_fake_capture
        namespace["install"]()
        window = QtWidgets.QWidget()
        window.setWindowTitle("FLIR ROI saturation — hardware-free verification")
        layout = QtWidgets.QVBoxLayout(window)
        image_view = pg.ImageView()
        image_view.ui.roiBtn.hide()
        image_view.ui.menuBtn.hide()
        image_view.getImageItem().setOpts(axisOrder="row-major")
        layout.addWidget(image_view, 1)
        panel = RoiIntensityPanel(image_view.ui.graphicsView, image_view.getImageItem())
        layout.addWidget(panel)
        panel.peak_timer.stop()
        window.resize(1500, 900)
        window.show()
        app.processEvents()
        check(panel.saturation_label.objectName() == "roiSaturationLabel",
              "Saturation indicator is missing from the real widget")
        check(panel._saturation_state["status"] == "no_roi", "Initial state must wait for an ROI")
        feed(np.zeros((12, 16), dtype=np.uint8))
        check(panel._saturation_state["status"] == "no_roi", "No ROI was incorrectly reported as normal")
        set_roi((2, 3, 7, 7))
        passed("initial_and_unselected_states_are_not_normal")

        frame = np.full((12, 16), 254, dtype=np.uint8)
        frame[0, 0] = 255
        feed(frame)
        assert_status(False, 0)
        normal_style = panel.saturation_label.styleSheet()
        passed("254_is_not_saturated_and_hot_pixel_outside_roi_is_ignored")

        frame = frame.copy()
        frame[4, 3] = 255
        feed(frame)
        assert_status(True, 1)
        check(panel.saturation_label.styleSheet() != normal_style,
              "Warning does not have a distinct visual style")
        screenshot("single_pixel_warning")
        passed("one_pixel_at_255_triggers_visible_warning")

        frame = frame.copy()
        frame[3, 2] = 255
        frame[6, 6] = 255
        feed(frame)
        assert_status(True, 3)
        check(abs(panel._saturation_state["fraction"] - 0.15) < 1e-12,
              "Saturated-pixel fraction is incorrect")
        feed(np.full((12, 16), 254, dtype=np.uint8))
        assert_status(False, 0)
        screenshot("recovered_normal")
        passed("counts_all_roi_pixels_and_clears_on_next_unsaturated_frame")

        frame = np.zeros((12, 16), dtype=np.uint8)
        frame[0, 0] = 255
        feed(frame)
        set_roi((0, 0, 2, 2))
        assert_status(True, 1, 4)
        check(len(panel.samples) == 0, "Changing ROI unexpectedly inserted a curve sample")
        panel.reset_button.click()
        assert_status(True, 1, 4)
        set_roi((2, 3, 7, 7))
        assert_status(False, 0)
        passed("moving_roi_without_new_frames_recomputes_and_reset_keeps_current_warning")

        meta = {
            "source": "camera",
            "image_info": {"PixelFormat": {"status": "ok", "value": "Mono16"}},
            "nodes": {"PixelDynamicRangeMax": {"status": "ok", "value": 65520},
                      "AdcBitDepth": {"status": "ok", "value": "Bit12"}},
        }
        raw = np.zeros((12, 16), dtype=np.uint16)
        raw[4, 3] = 65520
        shown = np.clip(raw.astype(np.float32) * (255 / 65535), 0, 255).astype(np.uint8)
        check(shown[4, 3] == 254, "Fixture must exercise saturation hidden by display conversion")
        payload = {"metadata": meta, "saturation": saturation.prepare_frame(raw, meta)}
        feed(shown, payload)
        assert_status(True, 1)
        check(panel._saturation_state["threshold"] == 65520, "Raw threshold was replaced by display 255")
        screenshot("raw_mono16_warning")
        passed("raw_mono16_limit_warns_even_when_display_pixel_is_254")

        # This capture is deliberately uninterpretable; a displayed 255 must not
        # turn an unknown original range into a definitive saturation finding.
        unknown_meta = {"source": "camera", "nodes": {}, "image_info": {}}
        unknown_raw = np.full((12, 16), 0.8, dtype=np.float32)
        unknown_payload = {"metadata": unknown_meta,
                           "saturation": saturation.prepare_frame(unknown_raw, unknown_meta)}
        feed(np.full((12, 16), 255, dtype=np.uint8), unknown_payload)
        check(panel._saturation_state["status"] == "unknown"
              and panel._saturation_state["saturated"] is None,
              "Unknown raw threshold was falsely reported as saturated or normal")
        check("未检测到过曝" not in panel.saturation_label.text(), "Unknown state was rendered as normal")
        screenshot("unknown_threshold")
        passed("unknown_raw_range_is_not_inferred_from_normalized_display")

        panel.reset_button.click()
        for value in (254, 255, 254):
            feed(np.full((12, 16), value, dtype=np.uint8))
        frozen = namespace["freeze_export"](panel)
        path = output / "saturation_history.csv"
        namespace["write_export_bundle"](path, **frozen)
        text = path.with_suffix(".txt").read_text(encoding="utf-8-sig")
        report = json.loads(text.split("--- BEGIN JSON ---\n", 1)[1].split("\n--- END JSON ---", 1)[0])
        states = [item["roi_saturation"] for item in report["samples"]]
        check([state["saturated"] for state in states] == [False, True, False]
              and [state["count"] for state in states] == [0, 20, 0],
              "TXT does not preserve per-point saturation history")
        check('"mask"' not in text and '"pixel_data"' not in text,
              "Saturation export unexpectedly serialized per-pixel data")
        result["artifacts"]["history_csv"] = str(path)
        result["artifacts"]["history_txt"] = str(path.with_suffix(".txt"))
        passed("txt_records_per_sample_saturation_without_pixel_masks")

        def detector_failure(*args, **kwargs):
            raise RuntimeError("injected saturation detector failure")

        for method_name in ("prepare_frame", "summarize_roi"):
            original_detector = getattr(saturation, method_name)
            before_count = len(panel.samples)
            try:
                setattr(saturation, method_name, detector_failure)
                feed(np.full((12, 16), 17, dtype=np.uint8))
                check(panel._saturation_state["status"] == "unknown"
                      and panel._saturation_state["saturated"] is None,
                      method_name + " failure did not produce an unknown state")
                check(len(panel.samples) == before_count + 1
                      and panel.samples[-1][1] == 20 * 17,
                      method_name + " failure interrupted normal curve sampling")
                check(panel._parameter_records[-1]["roi_saturation"]["status"] == "unknown",
                      method_name + " failure was not associated with its curve point")
                curve_x, curve_y = panel.curve.getData()
                check(len(curve_x) == len(panel.samples) and curve_y[-1] == 20 * 17,
                      method_name + " failure prevented plot refresh")
            finally:
                setattr(saturation, method_name, original_detector)
        feed(np.full((12, 16), 254, dtype=np.uint8))
        assert_status(False, 0)
        passed("detector_exceptions_report_unknown_without_interrupting_sampling_or_plot")

        feed(np.full((12, 16), 255, dtype=np.uint8))
        assert_status(True, 20)
        feed(np.zeros((9, 10), dtype=np.uint8))
        check(panel.bounds is None and panel._saturation_state["status"] == "no_roi",
              "Frame size change left a stale ROI warning")
        set_roi((0, 0, 2, 2))
        feed(np.full((9, 10), 255, dtype=np.uint8))
        assert_status(True, 4, 4)
        panel.clear_source()
        check(panel._saturation_state["status"] == "no_roi",
              "Clearing the source left a stale saturation warning")
        passed("frame_size_change_and_source_clear_remove_stale_warning")

        result["status"] = "passed"
        return result
    except Exception:
        result["status"] = "failed"
        result["traceback"] = traceback.format_exc()
        if window is not None:
            try:
                screenshot("failure")
            except Exception:
                pass
        raise
    finally:
        capture.take_capture = original_take_capture
        (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                          encoding="utf-8")
        if panel is not None:
            panel.peak_timer.stop()
        if window is not None:
            owned_views = []
            for view_box in list(pg.ViewBox.AllViews):
                scene = view_box.scene()
                if scene is not None and any(view.window() is window for view in scene.views()):
                    owned_views.append(view_box)
            for view_box in owned_views:
                try:
                    view_box.destroyed.disconnect()
                except TypeError:
                    pass
                view_box.setXLink(None)
                view_box.setYLink(None)
                view_box.unregister()
            window.close()
            window.deleteLater()
            QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)
        app.processEvents()
