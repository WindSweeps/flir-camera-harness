"""Frozen curve CSV plus compact parameter-only TXT; transactional paired export."""
import copy
import csv
import hashlib
import io
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
import compact_parameters

CSV_HEADER = ("经过时间_s", "光强总和_DN", "选区X_px", "选区Y_px", "选区宽度_px", "选区高度_px")
EXPLANATION = '''FLIR 曲线拍摄参数（1.9，精简版）
光强曲线数据只在CSV；本TXT仅含初始参数、参数变化及操作，不复制逐点数值、帧信息或像素。
initial_parameters.values为初始值；parameter_change.changed只覆盖变化键，removed删除失效键。
csv_line为含表头的CSV文件行；变化从该行开始适用。A→B→A分别保留。
camera.*为节点读回，frame.*为随帧Chunk参数，image.*为图像格式/尺寸；不等同于物理曝光时刻。
完整节点每2秒或操作后读回；曝光/增益优先核对随帧参数。自动曝光/增益变化也逐次记录。
software_roi的x/y/width/height单位px；硬件ROI、binning、decimation及模式另列。
source=replay_simulation表示回放；value=null表示未知。selector相关参数只代表当前selector。
conversion记录统计链路缩放；uint16不代表16位有效ADC。无标定系数，DN不直接等于物理功率。
manual_context是用户输入；export_time_manual_context为导出时附注，不改写历史参数。
过曝只保留是否发生及检查次数；采用2秒和操作后抽检，不保证捕获每个峰值。
CSV和TXT使用同一冻结数据，csv_sha256用于配对校验。
'''


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def fallback_metadata(panel, frame):
    backend = getattr(panel.window(), 'backend', None)
    if backend is not None and type(backend).__name__ == 'ImageReplayBackend':
        def entry(value, unit=None):
            return {'value': value, 'unit': unit, 'status': 'simulation', 'source': 'replay_control'}
        return {'source': 'replay_simulation', 'session_id': 'replay',
                'nodes': {'ExposureTime': entry(getattr(backend, 'exposure', None), 'us'),
                          'Gain': entry(getattr(backend, 'gain_db', None), 'dB'),
                          'ExposureAuto': entry(getattr(backend, 'automatic', None))},
                'image_info': {'shape': list(frame.shape), 'dtype': str(frame.dtype)},
                'conversion': {'statistics_input': 'replayed_grayscale8', 'changes_affect_pixels': False}}
    return {'source': 'unknown', 'nodes': {}, 'image_info': {'shape': list(frame.shape), 'dtype': str(frame.dtype)},
            'reason': 'No frame-associated camera metadata was captured; values have not been inferred.'}


def parameter_state(record):
    meta = record.get('metadata', {})
    chunks = meta.get('chunks', {})
    frame_parameters = {key: value for key, value in chunks.items()
                        if any(s in key.lower() for s in ('exposure', 'gain', 'blacklevel', 'width', 'height', 'offset'))}
    return {'source': meta.get('source', 'unknown'), 'session_id': meta.get('session_id'),
            'nodes': meta.get('nodes', {}), 'software_roi': list(record['bounds']),
            'manual_context': record.get('context', {}), 'conversion': meta.get('conversion'),
            'chunk_configuration': meta.get('chunk_configuration'), 'frame_parameters': frame_parameters}


def csv_bytes(samples, bounds):
    x0, y0, x1, y1 = map(int, bounds)
    if x1 <= x0 or y1 <= y0:
        raise ValueError('统计选区无效。')
    stream = io.StringIO(newline='')
    writer = csv.writer(stream)
    writer.writerow(CSV_HEADER)
    for elapsed, total in samples:
        writer.writerow((float(elapsed), int(total), x0, y0, x1-x0, y1-y0))
    return stream.getvalue().encode('utf-8-sig')


def make_report(path, data, samples, bounds, records, events=(), context=None, event_history=None, saturation_history=None):
    timeline = []
    changes = compact_parameters.Changes()
    if len(samples) != len(records):
        raise ValueError('曲线与参数历史数量不一致，已取消导出以避免错误配对。')
    for i, ((elapsed, total), record) in enumerate(zip(samples, records), 1):
        if record['elapsed_s'] != elapsed or record['sum_dn'] != total or tuple(record['bounds']) != tuple(bounds):
            raise ValueError('曲线与参数历史不匹配，已取消导出。')
        delta = changes.observe(compact_parameters.from_record(record),
                                **compact_parameters.at_sample(record, i))
        if delta:
            timeline.append(delta)
    event_changes = compact_parameters.Changes()
    return {'schema_version': 2, 'software_version': '1.9', 'exported_utc': utc_now(),
            'csv_name': Path(path).name, 'csv_sha256': hashlib.sha256(data).hexdigest(),
            'snapshot_scope': 'parameters_at_acquisition_frozen_before_save_dialog',
            'window_seconds': 60, 'sample_count': len(samples),
            'calibration': {'status': 'uncalibrated', 'physical_value': None,
                            'reason': 'No calibration coefficients supplied', 'dark_flat_applied': False},
            'export_time_manual_context': context or {},
            'parameter_timeline': timeline,
            'parameter_events': [compact_parameters.event_record(e, event_changes) for e in events],
            'saturation_history': compact_parameters.saturation_summary(saturation_history),
            'event_history': event_history or {}}


def _stage(destination, data):
    with tempfile.NamedTemporaryFile(dir=str(destination.parent), prefix='.flir_bundle_', suffix='.tmp', delete=False) as out:
        staged = Path(out.name)
        try:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        except BaseException:
            out.close()
            staged.unlink(missing_ok=True)
            raise
    return staged


def write_export_bundle(path, samples, bounds, records, events=(), context=None, event_history=None, saturation_history=None):
    """Commit matching CSV/TXT, restore the original pair on ordinary I/O failure.

    Multi-file filesystem transactions are not atomic across a power failure;
    the CSV SHA in TXT permits detection of an interrupted or mixed export.
    """
    samples, records = tuple(samples), tuple(records)
    if not samples or bounds is None:
        raise ValueError('当前选区没有曲线数据。')
    path = Path(path)
    txt = path.with_suffix('.txt')
    if path == txt:
        raise ValueError('请使用.csv文件名，参数日志保存在同名.txt文件。')
    data = csv_bytes(samples, bounds)
    report = make_report(path, data, samples, bounds, records, events, context, event_history, saturation_history)
    text = EXPLANATION + '\n--- BEGIN JSON ---\n' + json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n--- END JSON ---\n'
    staged, backups, committed = {}, {}, []
    try:
        for destination, contents in ((path, data), (txt, text.encode('utf-8-sig'))):
            if destination.exists() and not destination.is_file():
                raise IsADirectoryError(str(destination))
            staged[destination] = _stage(destination, contents)
            if destination.exists():
                backups[destination] = _stage(destination, destination.read_bytes())
        for destination in (path, txt):
            os.replace(str(staged[destination]), str(destination))
            del staged[destination]
            committed.append(destination)
    except BaseException as error:
        recovery_errors = []
        for destination in reversed(committed):
            try:
                if destination in backups:
                    os.replace(str(backups[destination]), str(destination))
                    del backups[destination]
                else:
                    destination.unlink(missing_ok=True)
            except OSError as recovery:
                recovery_errors.append(str(recovery))
        if recovery_errors:
            # Retain backup copies for manual recovery rather than deleting them.
            backup_paths = [str(p) for p in backups.values()]
            backups.clear()
            raise OSError('导出失败且恢复受阻，原文件备份位于：' + repr(backup_paths)) from error
        raise
    finally:
        for temporary in list(staged.values()) + list(backups.values()):
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    return len(samples)


def freeze_export(panel, capture_module):
    samples = tuple(panel.samples)
    bounds = tuple(panel.bounds) if panel.bounds is not None else None
    records = copy.deepcopy(tuple(getattr(panel, '_parameter_records', ())))
    # Never synthesize history using the camera's current values at export time.
    if len(records) != len(samples):
        records = tuple({'elapsed_s': s[0], 'sum_dn': s[1], 'bounds': bounds,
                         'metadata': {'source': 'unknown', 'nodes': {}, 'reason': 'historical metadata unavailable'},
                         'context': {}} for s in samples)
    events = []
    for record in records:
        events.extend(record['metadata'].get('parameter_events', []))
    backend = getattr(panel.window(), 'backend', None)
    event_history = {}
    if backend is not None:
        try:
            events.extend(capture_module.get_parameter_events(backend=backend))
            event_history = capture_module.get_event_history_status(backend=backend)
        except Exception:
            pass
    first_host = records[0].get('host_monotonic_s') if records else None
    unique = {}
    for event in events:
        event_time = event.get('host_monotonic_s', event.get('monotonic_s'))
        if first_host is not None and event_time is not None and event_time < first_host:
            continue
        unique[json.dumps(event, sort_keys=True, ensure_ascii=False)] = event
    return {'samples': samples, 'bounds': bounds, 'records': records,
            'events': copy.deepcopy(list(unique.values())),
            'event_history': copy.deepcopy(event_history),
            'saturation_history': copy.deepcopy(getattr(panel, '_saturation_history', {})),
            'context': copy.deepcopy(getattr(panel, '_experiment_context', {}))}
