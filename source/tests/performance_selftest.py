"""Real-Qt integration checks for throttled checks and sticky export warnings."""


def run_performance_tests(namespace):
    import copy
    import csv
    import io
    import json
    import time
    import traceback
    from pathlib import Path

    import numpy as np
    import pyqtgraph as pg
    import saturation
    from PyQt5 import QtCore, QtWidgets
    from roi_intensity import RoiIntensityPanel

    output = Path.cwd() / "_performance_test_output"
    output.mkdir(parents=True, exist_ok=True)
    result = {"status": "running", "hardware_access": False, "cases": [], "artifacts": {}}
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = None
    panel = None
    original_check = saturation.check_roi
    capture = namespace["capture_metadata"]
    original_take_capture = capture.take_capture
    original_messages = {}
    messages = []
    questions = []
    chooser_calls = []
    scan_calls = []
    question_behavior = {"answer": QtWidgets.QMessageBox.No, "during_question": None}
    clock = [time.perf_counter()]
    run_name = str(time.time_ns())
    requested_path = [output / ("not_saved_" + run_name + ".csv")]

    def check(condition, message):
        if not condition:
            raise AssertionError(message)

    def passed(name, **details):
        result["cases"].append({"name": name, "status": "passed", **details})

    def counting_check(*args, **kwargs):
        scan_calls.append(clock[0])
        return original_check(*args, **kwargs)

    def message_handler(kind):
        def record(*args, **kwargs):
            title = args[1] if len(args) > 1 else kwargs.get("title", "")
            text = args[2] if len(args) > 2 else kwargs.get("text", "")
            messages.append({"kind": kind, "title": str(title), "text": str(text)})
            if kind != "question":
                return QtWidgets.QMessageBox.Ok
            if title != "过曝数据保存确认":
                # Rerunning a test can encounter an old TXT; ordinary overwrite
                # confirmation is separate from the saturation consent tested.
                return QtWidgets.QMessageBox.Yes
            default = args[4] if len(args) > 4 else kwargs.get("defaultButton")
            questions.append({"title": str(title), "default": int(default) if default is not None else None})
            check(default == QtWidgets.QMessageBox.No, "Saturation confirmation must default to No")
            callback = question_behavior["during_question"]
            question_behavior["during_question"] = None
            if callback is not None:
                callback()
            return question_behavior["answer"]
        return record

    def choose_path(default_name):
        chooser_calls.append(default_name)
        return str(requested_path[0])

    def feed(frame, delta=0.01, process_events=False):
        clock[0] += delta
        image_view.setImage(frame, autoLevels=False, levels=(0, 255))
        panel.consume_frame(frame, clock[0])
        if process_events:
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

    def assert_current(saturated):
        state = panel._saturation_state
        check(state["status"] == "ok" and state["saturated"] is saturated,
              "Incorrect latest checked state: " + repr(state))

    def screenshot(name):
        app.processEvents()
        path = output / (name + ".png")
        check(window.grab().save(str(path)), "Could not save screenshot")
        result["artifacts"][name] = str(path)

    def read_report(path):
        text = path.with_suffix(".txt").read_text(encoding="utf-8-sig")
        report = json.loads(text.split("--- BEGIN JSON ---\n", 1)[1].split("\n--- END JSON ---", 1)[0])
        check('"mask"' not in text and '"pixel_data"' not in text,
              "Export contains pixel masks rather than scalar saturation information")
        rows = list(csv.reader(io.StringIO(path.read_text(encoding="utf-8-sig"))))
        return report, rows[1:]

    try:
        for kind in ("information", "warning", "critical", "question"):
            original_messages[kind] = getattr(QtWidgets.QMessageBox, kind)
            setattr(QtWidgets.QMessageBox, kind, message_handler(kind))
        saturation.check_roi = counting_check
        capture.take_capture = lambda frame: None
        namespace["install"]()
        window = QtWidgets.QWidget()
        window.setWindowTitle("FLIR throttled saturation — hardware-free verification")
        layout = QtWidgets.QVBoxLayout(window)
        image_view = pg.ImageView()
        image_view.ui.roiBtn.hide()
        image_view.ui.menuBtn.hide()
        image_view.getImageItem().setOpts(axisOrder="row-major")
        layout.addWidget(image_view, 1)
        panel = RoiIntensityPanel(image_view.ui.graphicsView, image_view.getImageItem())
        panel._choose_export_path = choose_path
        layout.addWidget(panel)
        panel.peak_timer.stop()
        window.resize(1500, 900)
        window.show()
        app.processEvents()

        normal = np.full((12, 16), 17, dtype=np.uint8)
        hot = normal.copy()
        hot[4, 3] = 255
        feed(normal)
        set_roi((2, 3, 7, 7))
        panel.reset_button.click()
        before_scans = len(scan_calls)
        started = time.perf_counter()
        for unused in range(600):
            feed(normal)
        duration = time.perf_counter() - started
        scans = len(scan_calls) - before_scans
        check(2 <= scans <= 6, "600 frames in six seconds triggered {} pixel scans".format(scans))
        check(len(panel.samples) == 600 and len(panel._parameter_records) == 600,
              "Throttling saturation dropped curve points or parameter records")
        states = [record["roi_saturation"] for record in panel._parameter_records]
        unchecked = [state for state in states if state.get("checked") is False]
        checked = [state for state in states if state.get("checked") is True]
        check(len(unchecked) >= 594 and 2 <= len(checked) <= 6,
              "Per-point check cadence does not match the actual scans")
        check(all(state["status"] == "not_checked" and state["saturated"] is None
                  for state in unchecked), "Unchecked frames were labelled as normal or saturated")
        check(not panel._saturation_history["detected"], "Normal sampling created sticky exposure history")
        curve_x, curve_y = panel.curve.getData()
        check(len(curve_x) == 600 and all(value == 20 * 17 for value in curve_y),
              "Throttling changed the intensity curve")
        passed("600_frames_keep_all_curve_points_with_at_most_six_roi_scans",
               scans=scans, unchecked_points=len(unchecked), execution_seconds=duration)

        feed(hot, delta=2.1)
        assert_current(True)
        first_history = copy.deepcopy(panel._saturation_history)
        check(first_history["detected"] and first_history["detection_count"] >= 1,
              "A checked saturated pixel did not set sticky history")
        check(first_history["first"]["saturated"] is True and first_history["last"]["saturated"] is True,
              "Sticky history does not preserve the detected checks")
        feed(normal, delta=2.1)
        assert_current(False)
        check(panel._saturation_history == first_history,
              "A later normal frame erased or altered sticky saturation history")
        check("过曝" in panel.saturation_label.text(), "Sticky warning disappeared after recovery")
        screenshot("sticky_warning_after_recovery")
        passed("checked_overexposure_remains_flagged_after_later_normal_check")

        question_behavior["answer"] = QtWidgets.QMessageBox.No
        question_count = len(questions)
        chooser_count = len(chooser_calls)
        check(panel.export_curve() is False, "Declining the saturation warning did not cancel export")
        check(len(questions) == question_count + 1, "Export did not ask about sticky overexposure")
        check(len(chooser_calls) == chooser_count, "A declined export still opened the file chooser")
        check(not requested_path[0].exists() and not requested_path[0].with_suffix(".txt").exists(),
              "Declining the warning wrote output files")
        passed("declining_overexposure_confirmation_prevents_file_dialog_and_files")

        frozen = namespace["freeze_export"](panel)
        expected_history = copy.deepcopy(panel._saturation_history)
        expected_samples = tuple(panel.samples)
        requested_path[0] = output / ("confirmed_frozen_" + run_name + ".csv")

        def mutate_live_curve_during_question():
            feed(normal, delta=2.1)
            panel.reset_button.click()
            feed(normal, delta=2.1)
            check(not panel._saturation_history["detected"], "Fixture failed to clear the live sticky state")

        question_behavior.update(answer=QtWidgets.QMessageBox.Yes,
                                 during_question=mutate_live_curve_during_question)
        question_count = len(questions)
        check(panel.export_curve() is True, "Accepted saturation export did not succeed")
        check(len(questions) == question_count + 1, "Accepted export skipped its warning")
        report, csv_rows = read_report(requested_path[0])
        check(len(csv_rows) == len(expected_samples) == report['sample_count'],
              "The prompt's frozen curve was replaced by later live samples")
        check(report['saturation_history']['detected'] == expected_history['detected']
              and report['saturation_history']['detection_count'] == expected_history['detection_count'],
              "Export did not preserve the saturation history reviewed at confirmation")
        check(frozen["saturation_history"] == expected_history,
              "freeze_export did not freeze sticky history")
        for row, point in zip(csv_rows, expected_samples):
            check(float(row[0]) == point[0] and int(row[1]) == point[1],
                  "Exported curve differs from the pre-confirmation snapshot")
        check('samples' not in report and 'roi_saturation' not in report,
              'TXT retained per-sample data instead of the sticky warning summary')
        result["artifacts"]["confirmed_csv"] = str(requested_path[0])
        result["artifacts"]["confirmed_txt"] = str(requested_path[0].with_suffix(".txt"))
        passed("accepting_exports_pre_prompt_snapshot_and_compact_sticky_warning")

        requested_path[0] = output / ("clean_" + run_name + ".csv")
        before_questions = len(questions)
        check(panel.export_curve() is True, "Clean curve export failed")
        check(len(questions) == before_questions, "A clean reset curve received a stale saturation warning")
        clean_report, unused = read_report(requested_path[0])
        check(not clean_report["saturation_history"]["detected"], "Clean export retained old saturation history")
        passed("reset_normal_curve_clears_sticky_export_warning")

        feed(hot, delta=2.1)
        assert_current(True)
        panel.reset_button.click()
        check(panel._saturation_history["detected"],
              "Reset incorrectly hides saturation still present in the current frame")
        check(len(panel.samples) == 0, "Reset inserted a curve point")
        passed("reset_rechecks_and_warns_if_current_frame_is_still_saturated")

        stopped = normal.copy()
        stopped[0, 0] = 255
        feed(stopped, delta=2.1)
        assert_current(False)
        set_roi((0, 0, 2, 2))
        assert_current(True)
        check(panel._saturation_history["detected"], "Stopped ROI move did not detect its hot pixel")
        set_roi((2, 3, 7, 7))
        assert_current(False)
        check(not panel._saturation_history["detected"], "History from a different ROI leaked into this selection")
        check(len(panel.samples) == 0, "Stopped ROI edits unexpectedly sampled new data")
        passed("stopped_roi_moves_force_checks_and_keep_history_isolated_to_selection")

        def fail_check(*args, **kwargs):
            raise RuntimeError("injected ROI saturation check failure")

        before_count = len(panel.samples)
        try:
            saturation.check_roi = fail_check
            feed(normal, delta=2.1)
            check(panel._saturation_state["status"] == "unknown"
                  and panel._saturation_state["saturated"] is None,
                  "Detector failure was reported as a definite saturation result")
            check(len(panel.samples) == before_count + 1 and panel.samples[-1][1] == 20 * 17,
                  "Detector failure interrupted curve sampling")
            check(panel._parameter_records[-1]["roi_saturation"]["status"] == "unknown",
                  "Detector failure was not associated with its curve point")
        finally:
            saturation.check_roi = counting_check
        feed(normal, delta=2.1)
        assert_current(False)
        passed("roi_check_failure_is_contained_and_next_scheduled_check_recovers")

        feed(hot, delta=2.1)
        check(panel._saturation_history["detected"], "Fixture did not establish history before source clear")
        panel.clear_source()
        check(panel._saturation_state["status"] == "no_roi"
              and not panel._saturation_history["detected"],
              "Clearing the source retained stale exposure state")
        screenshot("cleared_source")
        passed("source_clear_drops_sticky_history_and_current_roi_state")
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
        saturation.check_roi = original_check
        capture.take_capture = original_take_capture
        for kind, original in original_messages.items():
            setattr(QtWidgets.QMessageBox, kind, original)
        result["messages"] = messages
        result["questions"] = questions
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
