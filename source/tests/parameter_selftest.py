"""Hardware-free integration tests for compact parameter TXT export."""


def run_parameter_tests(namespace):
    import copy
    import csv
    import hashlib
    import io
    import json
    import os
    import traceback
    from pathlib import Path

    import numpy as np
    import pyqtgraph as pg
    from PyQt5 import QtCore, QtWidgets
    from roi_intensity import RoiIntensityPanel

    output = Path.cwd() / "_parameter_test_output"
    output.mkdir(parents=True, exist_ok=True)
    result = {"status": "running", "hardware_access": False, "cases": [], "artifacts": {}}
    capture = namespace["capture_metadata"]
    original_take_capture = capture.take_capture
    original_replace = os.replace
    original_messages = {}
    messages = []
    pending = {}
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = None
    panel = None
    bounds = (2, 3, 7, 7)
    frame_number = [0]

    def check(condition, message):
        if not condition:
            raise AssertionError(message)

    def passed(name, **details):
        result["cases"].append({"name": name, "status": "passed", **details})

    def message_handler(kind):
        def record(*args, **kwargs):
            messages.append({"kind": kind, "text": " | ".join(map(str, args[1:]))})
            return QtWidgets.QMessageBox.Yes if kind == "question" else QtWidgets.QMessageBox.Ok
        return record

    def take_fake_capture(frame):
        item = pending.pop(id(frame), None)
        if item is None:
            return None
        check(item[0] is frame, "Fake capture frame identity mismatch")
        return item[1]

    def metadata(exposure_us, auto=False, chunk_us=None):
        frame_number[0] += 1
        number = frame_number[0]
        exposure = {
            "status": "ok", "value": exposure_us, "unit": "us",
            "source": "node_readback", "frame_associated": False,
        } if exposure_us is not None else {
            "status": "unavailable", "unit": "us", "source": "node_readback",
            "frame_associated": False,
        }
        data = {
            "schema_version": 1, "source": "camera", "session_id": "parameter-test-session",
            "nodes": {
                "ExposureTime": exposure,
                "ExposureAuto": {"status": "ok", "value": "Continuous" if auto else "Off"},
                "Gain": {"status": "ok", "value": 0.0, "unit": "dB",
                         "source": "node_readback", "frame_associated": False},
                "Gamma": {"status": "unavailable", "source": "node_readback"},
            },
            "chunks": {},
            "image_info": {"frame_id": number, "width": 16, "height": 12},
            "readback_started_utc": "2026-11-05T08:00:{:02d}.000000+00:00".format(number % 60),
            "conversion": {"numpy_dtype": "uint8", "numpy_shape": [12, 16]},
        }
        if chunk_us is not None:
            data["chunks"]["ExposureTime"] = {
                "status": "ok", "value": chunk_us, "unit": "us",
                "source": "image_chunk", "frame_associated": True,
            }
        return data

    def feed(now, data, value=11):
        frame = np.full((12, 16), value, dtype=np.uint8)
        if data is not None:
            pending[id(frame)] = (frame, {"metadata": data})
        image_view.setImage(frame, autoLevels=False, levels=(0, 255))
        panel.consume_frame(frame, now)
        check(id(frame) not in pending, "consume_frame did not take the frame's metadata")
        return frame

    def set_roi(new_bounds):
        x0, y0, x1, y1 = new_bounds
        panel.roi.setPos((x0, y0))
        panel.roi.setSize((x1 - x0, y1 - y0))
        panel.roi.show()
        panel._region_changed()
        check(panel.bounds == new_bounds, "Real ROI bounds did not update")

    def parse_pair(path, frozen):
        raw_csv = path.read_bytes()
        check(raw_csv.startswith(b"\xef\xbb\xbf"), "CSV lost the Excel-compatible UTF-8 BOM")
        rows = list(csv.reader(io.StringIO(raw_csv.decode("utf-8-sig"))))
        check(rows[0] == ["经过时间_s", "光强总和_DN", "选区X_px", "选区Y_px",
                          "选区宽度_px", "选区高度_px"], "Existing six-column CSV format changed")
        text_path = path.with_suffix(".txt")
        text = text_path.read_text(encoding="utf-8-sig")
        begin, end = "--- BEGIN JSON ---\n", "\n--- END JSON ---"
        check(begin in text and end in text, "TXT is missing its machine-readable report")
        report = json.loads(text.split(begin, 1)[1].split(end, 1)[0])
        check(report["csv_sha256"] == hashlib.sha256(raw_csv).hexdigest(),
              "TXT hash does not match its paired CSV")
        check(report["calibration"]["status"] == "uncalibrated"
              and report["calibration"]["physical_value"] is None,
              "Uncalibrated DN was presented as a calibrated physical quantity")
        check(len(rows) - 1 == len(frozen["samples"]) == report['sample_count'],
              "CSV and TXT sample counts do not match the frozen curve")
        check('samples' not in report and 'frame_metadata' not in text and 'sum_dn' not in text,
              'TXT duplicated curve data or per-frame diagnostics')
        for csv_row, expected in zip(rows[1:], frozen['samples']):
            elapsed, total = expected
            check(float(csv_row[0]) == float(elapsed),
                  "CSV/TXT elapsed time changed or was rebased")
            check(int(csv_row[1]) == int(total),
                  "CSV/TXT intensity mismatch")
        for point in report['parameter_timeline']:
            check(frozen['samples'][point['csv_line'] - 2][0] == point['elapsed_s'],
                  'Parameter change does not identify its CSV line/time')
        result["artifacts"][path.stem] = {"csv": str(path), "txt": str(text_path)}
        return report

    def states(report):
        values = {}
        result = []
        for item in report['parameter_timeline']:
            values.update(item.get('values', item.get('changed', {})))
            for key in item.get('removed', []):
                values.pop(key, None)
            result.append(dict(values))
        return result

    def write_pair(stem, frozen=None, events=None):
        frozen = namespace["freeze_export"](panel) if frozen is None else frozen
        path = output / (stem + ".csv")
        count = namespace["write_export_bundle"](
            path, frozen["samples"], frozen["bounds"], frozen["records"],
            events=frozen["events"] if events is None else events,
            context=frozen["context"],
        )
        check(count == len(frozen["samples"]), "Bundle writer returned an incorrect row count")
        return parse_pair(path, frozen)

    def dictionaries(value):
        if isinstance(value, dict):
            yield value
            for child in value.values():
                yield from dictionaries(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                yield from dictionaries(child)

    try:
        for kind in ("information", "warning", "critical", "question"):
            original_messages[kind] = getattr(QtWidgets.QMessageBox, kind)
            setattr(QtWidgets.QMessageBox, kind, message_handler(kind))
        capture.take_capture = take_fake_capture
        namespace["install"]()
        window = QtWidgets.QWidget()
        window.setWindowTitle("FLIR parameter export — hardware-free verification")
        layout = QtWidgets.QVBoxLayout(window)
        image_view = pg.ImageView()
        image_view.ui.roiBtn.hide()
        image_view.ui.menuBtn.hide()
        image_view.getImageItem().setOpts(axisOrder="row-major")
        layout.addWidget(image_view, 1)
        panel = RoiIntensityPanel(image_view.ui.graphicsView, image_view.getImageItem())
        layout.addWidget(panel)
        panel.peak_timer.stop()
        window.resize(1700, 900)
        window.show()
        app.processEvents()
        feed(999.0, None)
        set_roi(bounds)

        # Returning to 20 ms is a new segment, even though its values match P0001.
        for elapsed, exposure in ((0, 20000), (10, 20000), (20, 40000),
                                  (40, 40000), (60, 20000)):
            feed(1000.0 + elapsed, metadata(exposure))
        full_window = namespace["freeze_export"](panel)
        check(len(full_window["records"]) == 5, "Each visible point needs a parameter record")
        report = write_pair("manual_20_40_20ms", full_window)
        segments = report['parameter_timeline']
        check(len(segments) == 3, "20→40→20 ms must produce exactly three contiguous segments")
        check([s['csv_line'] for s in segments] == [2, 4, 6], "Parameter changes were merged across time")
        check([s['camera.ExposureTime']['value'] for s in states(report)]
              == [20000, 40000, 20000], "Exposure segment values are incorrect")
        check(all(set(s['changed']) == {'camera.ExposureTime'} for s in segments[1:]),
              'Unchanged parameters were duplicated in a delta')
        passed("full_60_second_20_40_20ms_segments_and_csv_txt_rows")

        feed(1061.0, metadata(20000))
        trimmed = namespace["freeze_export"](panel)
        check([p[0] for p in trimmed["samples"]] == [10, 20, 40, 60, 61],
              "Visible rolling curve did not discard the old boundary point")
        check([r["elapsed_s"] for r in trimmed["records"]] == [10, 20, 40, 60, 61],
              "Metadata retention drifted from the visible 60-second curve")
        write_pair("trimmed_60_seconds", trimmed)
        passed("metadata_follows_visible_60_second_window")

        panel.reset_button.click()
        check(not panel.samples and not panel._parameter_records,
              "Reset left samples or historical metadata behind")
        for offset, exposure in enumerate((21000, 22000, 24000)):
            feed(2000.0 + offset, metadata(exposure, auto=True))
        automatic = namespace["freeze_export"](panel)
        report = write_pair("automatic_exposure", automatic)
        check([s['camera.ExposureTime']['value'] for s in states(report)] == [21000, 22000, 24000],
              "Automatic exposure was not retained for each received point")
        check(all(s['camera.ExposureAuto']['value'] == 'Continuous'
                  for s in states(report)), "Automatic mode was lost")
        passed("reset_clears_records_and_auto_exposure_tracks_each_frame")

        panel.reset_trace()
        feed(3000.0, metadata(None))
        report = write_pair("unknown_and_valid_zero")
        nodes = {k.removeprefix('camera.'): v for k, v in states(report)[0].items() if k.startswith('camera.')}
        check(nodes["ExposureTime"]["status"] == "unavailable"
              and nodes["ExposureTime"].get("value") is None,
              "Unavailable exposure was fabricated as a numeric value")
        check(nodes["Gamma"]["status"] == "unavailable" and nodes["Gamma"].get("value") is None,
              "Unavailable gamma was fabricated as zero")
        check(nodes["Gain"].get('status', 'ok') == "ok" and nodes["Gain"]["value"] == 0.0,
              "A valid zero gain was incorrectly treated as unknown")
        passed("unknown_fields_remain_unknown_valid_zero_is_retained")

        panel.reset_trace()
        feed(4000.0, metadata(20000, auto=True, chunk_us=21500))
        report = write_pair("chunk_and_readback_provenance")
        values = states(report)[0]
        check(values['frame.ExposureTime']['value'] == 21500,
              "Same-frame chunk exposure or its provenance was lost")
        check(values['camera.ExposureTime']['value'] == 20000,
              "Readback exposure was not distinguished from same-frame chunk data")
        passed("same_frame_chunk_and_later_nodemap_read_are_distinguished")

        event = {"id": 17, "operation": "set_exposure", "requested_us": 40000,
                 "readback": {"ExposureTime": {"value": 40000, "status": "ok"}},
                 "utc": "2026-11-05T08:00:00+00:00"}
        report = write_pair("parameter_operation_event", events=[event])
        saved_event = report['parameter_events'][0]
        check(saved_event['id'] == 17 and saved_event['requested_us'] == 40000
              and saved_event['readback']['values']['camera.ExposureTime']['value'] == 40000,
              "UI operation event and readback were dropped")
        passed("parameter_operation_events_are_exported")

        # No capture metadata must not inherit the last real camera's settings.
        panel.reset_trace()
        window.backend = type("ImageReplayBackend", (), {})()
        feed(5000.0, None)
        replay_frozen = namespace["freeze_export"](panel)
        check(replay_frozen["records"][0]["metadata"]["source"] == "replay_simulation",
              "Replay frame was not marked as simulation")
        report = write_pair("replay_simulation", replay_frozen)
        check(states(report)[0]['source'] == "replay_simulation",
              "Simulation provenance was lost in the TXT")
        check(not any(d.get("source") == "image_chunk" for d in dictionaries(report)),
              "Replay incorrectly reused real camera chunk metadata")
        del window.backend
        panel.reset_trace()
        feed(6000.0, None)
        unknown = namespace["freeze_export"](panel)
        check(unknown["records"][0]["metadata"]["source"] == "unknown",
              "Missing metadata was silently represented as real camera data")
        passed("replay_and_unknown_metadata_never_inherit_real_camera_parameters")

        # Freeze both files before the save dialog's nested event loop can modify
        # the live curve, ROI, or nested parameter dictionaries.
        panel.reset_trace()
        for offset, exposure in enumerate((20000, 40000, 20000)):
            feed(7000.0 + offset, metadata(exposure))
        expected = namespace["freeze_export"](panel)
        expected_copy = copy.deepcopy(expected)
        snapshot_path = output / "dialog_snapshot.csv"
        live_state = {}

        def choose_while_changing(default_name):
            panel._parameter_records[0]["metadata"]["nodes"]["ExposureTime"]["value"] = 999999
            feed(7010.0, metadata(80000))
            set_roi((1, 1, 4, 5))
            feed(7100.0, metadata(160000), value=13)
            live_state["samples"] = tuple(panel.samples)
            live_state["records"] = copy.deepcopy(tuple(panel._parameter_records))
            live_state["bounds"] = panel.bounds
            return str(snapshot_path)

        panel._choose_export_path = choose_while_changing
        check(panel.export_curve() is not False, "Snapshot export failed")
        report = parse_pair(snapshot_path, expected_copy)
        check([s['camera.ExposureTime']['value'] for s in states(report)] == [20000, 40000, 20000],
              "Dialog changes leaked into frozen TXT parameter data")
        check(expected["records"] == expected_copy["records"],
              "freeze_export kept mutable references to live metadata")
        check(tuple(panel.samples) == live_state["samples"]
              and tuple(panel._parameter_records) == live_state["records"]
              and panel.bounds == live_state["bounds"], "Export rewound the live capture state")
        passed("csv_and_txt_share_deep_frozen_pre_dialog_snapshot")

        # Fail only the first TXT commit, allowing the writer's restoration calls
        # to run. Existing files must survive with byte-for-byte original content.
        rollback_csv = output / "rollback.csv"
        rollback_txt = rollback_csv.with_suffix(".txt")
        rollback_csv.write_bytes(b"previous CSV bytes\r\n")
        rollback_txt.write_bytes(b"previous TXT bytes\r\n")
        before_files = set(output.iterdir())
        failed = [False]

        def fail_txt_commit_once(source, destination, *args, **kwargs):
            if Path(destination).resolve() == rollback_txt.resolve() and not failed[0]:
                failed[0] = True
                raise PermissionError("Injected TXT commit failure")
            return original_replace(source, destination, *args, **kwargs)

        os.replace = fail_txt_commit_once
        caught = False
        try:
            namespace["write_export_bundle"](
                rollback_csv, full_window["samples"], full_window["bounds"], full_window["records"],
                events=full_window["events"], context=full_window["context"],
            )
        except OSError:
            caught = True
        finally:
            os.replace = original_replace
        check(failed[0] and caught, "Injected second-file commit failure was not surfaced")
        check(rollback_csv.read_bytes() == b"previous CSV bytes\r\n"
              and rollback_txt.read_bytes() == b"previous TXT bytes\r\n",
              "Failed bundle export did not restore both original files")
        check(set(output.iterdir()) == before_files, "Failed bundle export left temporary/backup files")
        passed("second_file_failure_restores_both_existing_files_and_cleans_temporary_files")

        for artifact in result["artifacts"].values():
            text = Path(artifact["txt"]).read_text(encoding="utf-8-sig")
            report_json = json.loads(text.split("--- BEGIN JSON ---\n", 1)[1]
                                     .split("\n--- END JSON ---", 1)[0])
            check(not any(key in d for d in dictionaries(report_json)
                          for key in ("pixels", "pixel_data", "frame_array", "image_data")),
                  "Parameter-only export unexpectedly contains per-pixel data")
        passed("parameter_txt_contains_no_pixel_arrays")
        app.processEvents()
        screenshot = output / "screenshot.png"
        check(window.grab().save(str(screenshot)), "Could not save test screenshot")
        result["screenshot"] = str(screenshot)
        result["status"] = "passed"
        return result
    except Exception:
        result["status"] = "failed"
        result["traceback"] = traceback.format_exc()
        if window is not None:
            try:
                window.grab().save(str(output / "screenshot.png"))
            except Exception:
                pass
        raise
    finally:
        capture.take_capture = original_take_capture
        os.replace = original_replace
        result["messages"] = messages
        (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                          encoding="utf-8")
        for kind, original in original_messages.items():
            setattr(QtWidgets.QMessageBox, kind, original)
        if panel is not None:
            panel.peak_timer.stop()
        if window is not None:
            owned_views = []
            for view_box in list(pg.ViewBox.AllViews):
                scene = view_box.scene()
                if scene is not None and any(v.window() is window for v in scene.views()):
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
