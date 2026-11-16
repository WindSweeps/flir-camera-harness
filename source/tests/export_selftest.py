"""Hardware-free integration checks for the injected ROI CSV export feature.

Executed inside the existing, bundled Python/Qt runtime.  The original
roi_intensity module and its real widget/curve are deliberately used here.
"""


def run_export_tests(namespace):
    import csv
    import io
    import json
    import math
    import traceback
    from pathlib import Path

    import numpy as np
    import pyqtgraph as pg
    from PyQt5 import QtCore, QtWidgets
    from roi_intensity import RoiIntensityPanel

    output = Path.cwd() / "_export_test_output"
    output.mkdir(parents=True, exist_ok=True)
    report = {
        "status": "running",
        "hardware_access": False,
        "cases": [],
        "artifacts": {},
    }
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = None
    panel = None
    messages = []
    original_messages = {}
    expected_header = [
        "经过时间_s", "光强总和_DN", "选区X_px", "选区Y_px",
        "选区宽度_px", "选区高度_px",
    ]

    def check(condition, message):
        if not condition:
            raise AssertionError(message)

    def passed(name, **details):
        report["cases"].append({"name": name, "status": "passed", **details})

    def read_csv(path):
        raw = path.read_bytes()
        check(raw.startswith(b"\xef\xbb\xbf"), "CSV must have a UTF-8 BOM")
        rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"))))
        check(bool(rows) and rows[0] == expected_header, "Unexpected CSV header")
        check(all(len(row) == 6 for row in rows), "CSV row has wrong column count")
        return rows[1:]

    def assert_rows(rows, samples, bounds):
        check(len(rows) == len(samples), "Exported sample count differs from snapshot")
        x0, y0, x1, y1 = bounds
        expected_roi = [x0, y0, x1 - x0, y1 - y0]
        for row, (elapsed, total) in zip(rows, samples):
            check(math.isclose(float(row[0]), float(elapsed), rel_tol=0, abs_tol=1e-9),
                  "Elapsed time was rounded or rebased")
            check(int(row[1]) == int(total), "Intensity sum was changed")
            check([int(value) for value in row[2:]] == expected_roi,
                  "ROI must contain X, Y, width, height from the snapshot")

    def assert_curve_matches_samples():
        xs, ys = panel.curve.getData()
        snapshot = tuple(panel.samples)
        check(xs is not None and ys is not None, "Real plot curve has no data")
        check(np.array_equal(xs, np.asarray([s[0] for s in snapshot])),
              "Real plot times differ from panel samples")
        check(np.array_equal(ys, np.asarray([s[1] for s in snapshot])),
              "Real plot values differ from panel samples")

    def set_roi(bounds):
        x0, y0, x1, y1 = bounds
        panel.roi.setPos((x0, y0))
        panel.roi.setSize((x1 - x0, y1 - y0))
        panel.roi.show()
        panel._region_changed()
        check(tuple(panel.bounds) == tuple(bounds), "Real ROI bounds did not update")

    def message_handler(kind):
        def record(*args, **kwargs):
            messages.append({"kind": kind, "text": " | ".join(map(str, args[1:]))})
            return QtWidgets.QMessageBox.Yes if kind == 'question' else QtWidgets.QMessageBox.Ok
        return record

    try:
        # Suppress blocking notifications, while retaining their observable result.
        for kind in ("information", "warning", "critical", "question"):
            original_messages[kind] = getattr(QtWidgets.QMessageBox, kind)
            setattr(QtWidgets.QMessageBox, kind, message_handler(kind))

        namespace["install"]()
        # Installation must be safe when the normal bootstrap already called it.
        namespace["install"]()
        window = QtWidgets.QWidget()
        window.setWindowTitle("FLIR ROI CSV export — hardware-free verification")
        layout = QtWidgets.QVBoxLayout(window)
        image_view = pg.ImageView()
        image_view.ui.roiBtn.hide()
        image_view.ui.menuBtn.hide()
        image_view.getImageItem().setOpts(axisOrder="row-major")
        layout.addWidget(image_view, 1)
        panel = RoiIntensityPanel(image_view.ui.graphicsView, image_view.getImageItem())
        layout.addWidget(panel)
        panel.peak_timer.stop()  # Synthetic timestamps; no real-time expiry in this test.
        window.resize(1700, 900)
        window.show()
        app.processEvents()

        controls = panel.layout().itemAt(0).layout()
        reset_index = controls.indexOf(panel.reset_button)
        check(reset_index >= 0, "Reset button is missing from the control row")
        check(controls.indexOf(panel.export_button) == reset_index + 1,
              "Export button must follow the reset button")
        check(sum(controls.itemAt(i).widget() is panel.export_button
                  for i in range(controls.count())) == 1,
              "Repeated installation duplicated the export button")
        passed("button_placement_and_idempotent_install")

        # Exercise the real Qt save dialog before replacing the selection hook
        # below. This verifies its nested event loop and default .csv suffix.
        def exercise_real_dialog(accept):
            state = {"ticks": 0, "acted": False, "finished": False, "error": None}
            heartbeat = QtCore.QTimer(panel)
            heartbeat.setInterval(10)

            def tick():
                state["ticks"] += 1

            def finish_dialog():
                if state["finished"]:
                    return
                dialog = app.activeModalWidget()
                try:
                    check(isinstance(dialog, QtWidgets.QFileDialog),
                          "Active modal widget is not the real QFileDialog")
                    check(dialog.testOption(QtWidgets.QFileDialog.DontUseNativeDialog),
                          "The save dialog must keep the Qt event loop running")
                    state["acted"] = True
                    if accept:
                        dialog.setDirectory(str(output))
                        filename_edit = dialog.findChild(QtWidgets.QLineEdit, "fileNameEdit")
                        check(filename_edit is not None, "Qt save dialog has no filename edit")
                        filename_edit.setText("dialog_no_suffix")
                        dialog.accept()
                    else:
                        dialog.reject()
                except Exception:
                    state["error"] = traceback.format_exc()
                    if dialog is not None:
                        dialog.reject()

            def timed_out():
                state["error"] = "The real save dialog did not complete within 5 seconds"
                dialog = app.activeModalWidget()
                if dialog is not None:
                    dialog.reject()

            timeout = QtCore.QTimer(panel)
            timeout.setSingleShot(True)
            timeout.timeout.connect(timed_out)
            heartbeat.timeout.connect(tick)
            heartbeat.start()
            timeout.start(5000)
            QtCore.QTimer.singleShot(120, finish_dialog)
            try:
                result = panel._choose_export_path("test_default.csv")
            finally:
                state["finished"] = True
                heartbeat.stop()
                timeout.stop()
                heartbeat.deleteLater()
                timeout.deleteLater()
            check(state["error"] is None, state["error"] or "Save dialog failed")
            check(state["acted"], "Save dialog returned before the scheduled interaction")
            check(state["ticks"] > 0, "Qt timers stopped while the save dialog was open")
            return result, state["ticks"]

        files_before = set(output.iterdir())
        selected, accepted_ticks = exercise_real_dialog(True)
        check(Path(selected).name == "dialog_no_suffix.csv",
              "Save dialog omitted .csv suffix; returned {!r}".format(selected))
        check(Path(selected).parent.resolve() == output.resolve(), "Save dialog chose wrong folder")
        rejected, rejected_ticks = exercise_real_dialog(False)
        check(rejected == "", "Rejecting the real save dialog did not return an empty path")
        check(set(output.iterdir()) == files_before, "Choosing a filename wrote a file")
        passed("real_save_dialog_suffix_cancel_and_event_loop",
               accepted_timer_ticks=accepted_ticks, rejected_timer_ticks=rejected_ticks)

        # Empty data must be handled before a file dialog is opened.
        dialog_calls = []

        def unexpected_dialog(default_name):
            dialog_calls.append(default_name)
            raise AssertionError("Empty curve must not open a save dialog")

        panel._choose_export_path = unexpected_dialog
        files_before = set(output.iterdir())
        message_count = len(messages)
        panel.export_curve()
        check(not dialog_calls, "Empty export opened a file dialog")
        check(len(messages) > message_count, "Empty export did not notify the user")
        check(set(output.iterdir()) == files_before, "Empty export created a file")
        passed("empty_curve_does_not_write")

        # Drive the original widget with actual uint8 image frames and ROI signals.
        frame_shape = (12, 16)
        frame = np.full(frame_shape, 7, dtype=np.uint8)
        image_view.setImage(frame, autoLevels=False, levels=(0, 255))
        panel.consume_frame(frame, 1000.0)
        bounds = (2, 3, 7, 7)  # 5 x 4 = 20 pixels, half-open image slice bounds.
        set_roi(bounds)
        for index, elapsed in enumerate((0.0, 10.0, 40.375, 60.0, 100.375)):
            frame = np.full(frame_shape, 7 + index, dtype=np.uint8)
            image_view.setImage(frame, autoLevels=False, levels=(0, 255))
            panel.consume_frame(frame, 1000.0 + elapsed)
        snapshot = tuple(panel.samples)
        check(snapshot == ((40.375, 180), (60.0, 200), (100.375, 220)),
              "Real acquisition/ROI/60-second rolling-window behavior differed")
        assert_curve_matches_samples()
        passed("real_roi_sum_and_60_second_window", samples=list(snapshot), bounds=list(bounds))

        # Cancel must preserve samples, origin, ROI and the real plot.
        sample_object = panel.samples
        origin = panel._origin
        panel._choose_export_path = lambda default_name: ""
        files_before = set(output.iterdir())
        panel.export_curve()
        check(set(output.iterdir()) == files_before, "Cancel created or removed a file")
        check(panel.samples is sample_object and tuple(panel.samples) == snapshot,
              "Cancel changed original samples")
        check(panel._origin == origin and panel.bounds == bounds, "Cancel changed ROI/timing")
        assert_curve_matches_samples()
        passed("cancel_preserves_curve")

        # Click the actual button so signal/slot wiring is tested too.
        saved_path = output / "当前曲线.csv"
        defaults = []

        def choose_saved_path(default_name):
            defaults.append(default_name)
            return str(saved_path)

        panel._choose_export_path = choose_saved_path
        panel.export_button.click()
        check(len(defaults) == 1, "Button did not open exactly one save dialog")
        check(saved_path.is_file(), "Button click did not create the CSV")
        assert_rows(read_csv(saved_path), snapshot, bounds)
        check(panel.samples is sample_object and tuple(panel.samples) == snapshot,
              "Successful export changed original samples")
        check(panel._origin == origin and panel.bounds == bounds,
              "Successful export changed ROI/timing")
        assert_curve_matches_samples()
        report["artifacts"]["visible_curve_csv"] = str(saved_path)
        passed("button_saves_bom_header_values_and_original_elapsed")

        # Direct writer checks that 64-bit-sized totals are not formatted as a
        # locale-dependent number or truncated to 32-bit, without mutating inputs.
        large_path = output / "large_values.csv"
        large_samples = ((123.125, 4_294_967_311), (124.875, 8_589_934_599))
        large_bounds = (7, 11, 107, 211)
        namespace["write_curve_csv"](large_path, large_samples, large_bounds)
        assert_rows(read_csv(large_path), large_samples, large_bounds)
        passed("csv_preserves_large_integer_totals")

        # A native save dialog runs a nested event loop. Simulate both incoming
        # frames and a changed ROI during that interval, then require the pre-dialog
        # snapshot in the export and the new state still present in the widget.
        concurrent_path = output / "dialog_snapshot.csv"
        newer_state = {}

        def choose_after_updates(default_name):
            panel.consume_frame(np.full(frame_shape, 12, dtype=np.uint8), 1101.375)
            set_roi((1, 1, 4, 5))
            panel.consume_frame(np.full(frame_shape, 13, dtype=np.uint8), 1200.0)
            newer_state["samples"] = tuple(panel.samples)
            newer_state["bounds"] = panel.bounds
            newer_state["origin"] = panel._origin
            return str(concurrent_path)

        panel._choose_export_path = choose_after_updates
        panel.export_curve()
        assert_rows(read_csv(concurrent_path), snapshot, bounds)
        check(panel.samples is sample_object, "Export replaced the live sample deque")
        check(tuple(panel.samples) == newer_state["samples"] == ((0.0, 156),),
              "Export disturbed samples arriving during the dialog")
        check(panel.bounds == newer_state["bounds"] == (1, 1, 4, 5),
              "Export restored stale ROI bounds after the dialog")
        check(panel._origin == newer_state["origin"] == 1200.0,
              "Export disturbed the new curve's origin")
        assert_curve_matches_samples()
        report["artifacts"]["snapshot_csv"] = str(concurrent_path)
        passed("samples_and_roi_frozen_before_dialog_live_state_preserved")

        # A directory with a .csv suffix is a real, deterministic write failure,
        # including when the implementation normalizes the filename extension.
        failure_path = output / "write_failure.csv"
        failure_path.mkdir(exist_ok=True)
        sentinel = failure_path / "keep.txt"
        sentinel.write_text("Do not change this existing directory.", encoding="utf-8")
        failure_snapshot = tuple(panel.samples)
        failure_bounds = panel.bounds
        failure_origin = panel._origin
        message_count = len(messages)
        panel._choose_export_path = lambda default_name: str(failure_path)
        panel.export_curve()
        check(any(m["kind"] in ("warning", "critical") for m in messages[message_count:]),
              "A failed save did not report an error")
        check(failure_path.is_dir() and sentinel.read_text(encoding="utf-8") ==
              "Do not change this existing directory.", "Failed save damaged existing data")
        check(tuple(panel.samples) == failure_snapshot and panel.bounds == failure_bounds
              and panel._origin == failure_origin, "Failed save disturbed the curve")
        assert_curve_matches_samples()
        passed("write_failure_reports_error_and_preserves_curve_and_destination")

        # Restore a readable multi-point curve for visual review of the real UI.
        set_roi(bounds)
        for index, elapsed in enumerate((0.0, 10.0, 40.375, 60.0, 100.375)):
            frame = np.full(frame_shape, 7 + index, dtype=np.uint8)
            image_view.setImage(frame, autoLevels=False, levels=(0, 255))
            panel.consume_frame(frame, 2000.0 + elapsed)
        app.processEvents()
        screenshot = output / "screenshot.png"
        check(window.grab().save(str(screenshot)), "Could not save GUI screenshot")
        report["artifacts"]["screenshot"] = str(screenshot)
        report["messages"] = messages
        report["status"] = "passed"
        return report
    except Exception:
        report["status"] = "failed"
        report["traceback"] = traceback.format_exc()
        report["messages"] = messages
        if window is not None:
            try:
                app.processEvents()
                window.grab().save(str(output / "screenshot.png"))
            except Exception:
                pass
        raise
    finally:
        (output / "result.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        for kind, original in original_messages.items():
            setattr(QtWidgets.QMessageBox, kind, original)
        if panel is not None:
            panel.peak_timer.stop()
        if window is not None:
            # ViewBox's destroyed callback refreshes every registered link menu.
            # During ordinary Python GC a different menu's QComboBox can already
            # be gone. Remove only this test window's views from that registry
            # while all their Qt children are still alive, then let Qt destroy
            # the complete widget/scene tree through DeferredDelete.
            owned_views = []
            for view_box in list(pg.ViewBox.AllViews):
                scene = view_box.scene()
                if scene is not None and any(
                    graphics_view.window() is window for graphics_view in scene.views()
                ):
                    owned_views.append(view_box)
            for view_box in owned_views:
                try:
                    view_box.destroyed.disconnect()
                except TypeError:
                    pass  # Unnamed views need not have a destroyed callback.
                view_box.setXLink(None)
                view_box.setYLink(None)
                view_box.unregister()
            window.close()
            window.deleteLater()
            QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)
        app.processEvents()
