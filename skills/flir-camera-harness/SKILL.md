---
name: flir-camera-harness
description: "Operate the user's FLIR ROI camera through the flir-camera MCP harness: configure exposure/trigger, acquire ROI intensity curves, record, export, and inspect saved results. Use for camera operation and experiment capture, including Chinese requests about FLIR 相机、外部触发、光强记录."
---

# FLIR Camera Harness

Use the connected `flir_*` MCP tools. The executable and schemas are in this
plugin package; this skill alone does not install or establish an MCP connection.

Start with `flir_status`. For a new device, enumerate with `flir_list_cameras`,
open the selected `camera_id`, configure, then call `flir_start_capture` when the
user requests acquisition. One process owns one camera. Opening and acquiring
are separate operations. A second GUI/AI process may already own the hardware.

Preserve these experiment invariants:

- Check `source`. `replay_simulation` tests controls and exports; its exposure and
  gain do not change pixels, and it cannot validate hardware triggering.
- Exposure is **microseconds**, gain **dB**, ROI coordinates **pixels of the full
  displayed image**, not preview thumbnail coordinates. `flir_preview` reports
  both image and preview dimensions. Read back device values after changing them.
- Curve values are sums of displayed grayscale DN. They are not radiometric
  power or raw sensor counts. Calibration/context entries are provenance only;
  physical values remain unknown. Binning and hardware ROI are readable metadata,
  not setters in this version.
- External trigger means FrameStart, Line0 input, RisingEdge, non-inverted,
  Overlap Off; default delay 9 us. A waiting camera can be healthy without frames.
  No tool creates an electrical trigger. Configure photo name/directory before
  starting; inspect saved counts and exact paths instead of assuming a pulse saved.
- ROI changes/clear reset the displayed curve. Continuous records retain prior
  samples, ROI changes, parameter changes and their overexposure history.
- `SATURATION_WARNING` means explain that data may be clipped and ask whether
  to save, unless the user already explicitly agreed for this data. Only then
  retry with `allow_saturated=true`. Never reset ROI/history to avoid the warning.
  `INCOMPLETE_RECORDING` similarly needs agreement about missing data. These are
  save-specific decisions, not a blanket approval requirement for other tools.
- Export calls return a job. Poll `flir_job_status` until `done=true` and
  `status=succeeded` before saying files were saved; report the returned CSV/TXT
  paths and counts. Never infer success from a submitted job or a requested path.
  Default is no overwrite; choose a new filename unless overwrite is authorized.
- `record_stop` stops sample intake; wait for `record_status.done=true` before
  exporting. Preserve cache paths on errors. Successful exports remove only
  this process's owned record cache; recovered external caches remain.
- Poll status/record/job around 0.5–2 seconds while waiting. Do not repeatedly
  fetch images, curves or refresh SDK nodes. Status uses cached information;
  parameter refresh is throttled to 2 seconds. Preview is a bounded JPEG.
- Enable LAN monitoring only when requested. The returned link contains a
  private access token and the phone service is read-only. Do not expose a
  control endpoint or change firewall/router settings as part of routine capture.

For recording, recovery, trigger/photo sequences, read
[references/workflows.md](references/workflows.md). The live `tools/list` is the
authoritative schema. Unsupported nodes are unknown, not successful settings.
Complete already-authorized operations without repeatedly asking permission.
