# Workflows and result handling

## Continuous ROI curve

1. `flir_status`; if needed list/open a camera. Opening does not start capture.
2. Set manual exposure/gain, or `flir_set_auto_brightness` (controls both). A
   manual setting disables both automatic controls. Use device range/readback.
3. `flir_set_trigger {"mode":"continuous"}`; `flir_start_capture`.
4. Wait for `frame_shape`; use `flir_preview` to inspect, then `flir_set_roi` with
   original-image coordinates. Setting ROI clears only the current displayed trace.
5. Short experiment: `flir_export_curve {"path":"experiment/curve.csv"}` exports
   the current frozen 60-second window. It does not export the entire acquisition.
6. Long experiment: `flir_record_start` → keep `record_id` → at requested end
   `flir_record_stop` → poll record until drained → `flir_record_export` → poll
   job until succeeded. Stopping capture alone does not end continuous recording.

Relative paths resolve under `status.output_directory`. CSV has time, DN sum,
ROI x/y/width/height. The paired TXT holds initial camera parameters and changes,
binning/format/exposure/gain/ROI and provenance where available. It does not hold
raw pixels, duplicate intensity samples or one full parameter block per point.
Every change means every **observed** change: chunks when supported; global
node snapshots otherwise run every 2 s or after operations, not every exposure.

## External trigger photos

Stop capture and wait for prior photo writes to drain. Call
`flir_photo_configure {"enabled":true,"name":"experiment_A","directory":"photos"}`,
then `flir_set_trigger {"mode":"external","delay_us":9}` and start capture.
Do not describe waiting for Line0 as an acquisition failure. Poll photo status
at low frequency. The output is a new batch directory containing full raw
grayscale TIFFs and parameter TXTs, not ROI crops or JPEG screenshots.

Filename: `YYYYMMDD-HHMMSS-mmm_name_000001.tif`, sequence resets per batch.
The filename timestamp is host frame reception time in UTC+08:00. Hardware frame
timestamp is separate metadata where available; do not equate the two clocks.
`written` counts completed files. `submitted` includes queued work. Report
`unsaved`, `rejected`, errors or frame-ID gaps. Do not claim one photo per
electrical pulse. Stop acquisition then wait for pending writes to reach zero.

## Save warnings and recovery

`SATURATION_WARNING` is a tool error, or `job_status.error.code` if detected during
streaming recovery inspection. Show the user the warning and scope. After their
agreement, repeat with `allow_saturated=true`. A sampled non-detection does not
prove all frames were unsaturated; the check runs every 2 s/on operations.

On client restart, old record/job IDs are invalid. Use the returned absolute
`session.jsonl` cache path with `flir_record_recover`; poll its job for a new
record_id. The recovery scan reads incrementally and never modifies the source.
An incomplete journal requires `allow_incomplete=true` after agreement. Complete
lines can be recovered after a torn final line; never promise lost frames exist.
`BUSY` during the UI's final inspection is transient: poll status, then retry.
Failed export preserves the cache. `ALREADY_EXPORTED` gives previous output paths.

## Phone monitor and local GUI

List `flir_monitor_addresses`, choose the intended reachable LAN address, then
`flir_monitor_start {"host":"192.168.x.x","port":8765}`. Give the access link to
the requesting user; it contains a secret fragment token. Monitor stop invalidates
the link. `flir_status` does not return the token. AI control remains on stdio;
the phone HTTP service has no write/control API.

`flir_show_window {"visible":true}` shows the same camera instance owned by the
MCP process. Hiding it with `visible=false` keeps the session running; clicking
the window close button stops/drains the session and ends its MCP process.
In MCP mode modal information/warnings are recorded in
`status.recent_events`; save questions default to No. Use MCP export flags for
approved warning cases. Launch `open_camera_gui.cmd` for the ordinary interactive
GUI, after closing the MCP-owned camera.

## Connection and failure interpretation

This package uses initialize-based MCP over local stdio (2025-11-25,
2025-06-18, 2025-03-26, 2024-11-05). Newer clients need legacy negotiation/fallback.
No HTTP MCP transport, arbitrary code execution, raw node writes, or file server
is offered. The paired CSV/TXT and photos remain on the camera computer.

EOF stops acquisition, stops recording, stops the LAN server, and drains pending
jobs/writes. A client force-kill cannot be made safe by the server; recover its
cache next launch. Do not keep issuing new capture commands while disconnecting.
