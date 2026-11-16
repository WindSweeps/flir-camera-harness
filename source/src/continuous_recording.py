"""Bounded producer queue, append-only disk journal and streaming CSV/TXT export.

Only scalar measurements/metadata cross the queue; serialization and disk I/O
run on a worker. No full recording is ever collected in an in-memory list.
"""
import copy
import csv
import hashlib
import json
import os
import queue
import shutil
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import parameter_log
import compact_parameters

SCHEMA = 'flir_continuous_recording_v1'
FLUSH_SECONDS = 1.0
QUEUE_LIMIT = 512


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def cache_root():
    return Path(tempfile.gettempdir()) / 'FLIR_ROI_Camera_records'


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False) + '\n'


def empty_history():
    return {'detected': False, 'detection_count': 0, 'first': None, 'last': None,
            'scope': 'entire continuous recording, including ROI changes and curve resets'}


class Recorder:
    def __init__(self, root=None, start_monotonic=None, queue_limit=QUEUE_LIMIT):
        root = Path(root) if root is not None else cache_root()
        root.mkdir(parents=True, exist_ok=True)
        self.directory = Path(tempfile.mkdtemp(prefix='record_', dir=str(root)))
        self.path = self.directory / 'session.jsonl'
        self.started = time.perf_counter() if start_monotonic is None else float(start_monotonic)
        self.started_utc = utc_now()
        self.queue = queue.Queue(maxsize=queue_limit)
        self.lock = threading.Lock()
        self.stop_requested = threading.Event()
        self.done = threading.Event()
        self.accepting = True
        self.error = None
        self.submitted = self.written = self.flushed = self.rejected = 0
        self.saturation_history = empty_history()
        self._last_saturation = None
        self.stop_info = None
        self.thread = threading.Thread(target=self._run, name='FLIR-record-writer', daemon=True)
        self.thread.start()

    def _offer(self, kind, value):
        # Called only by the UI producer; never block waiting for disk.
        if not self.accepting:
            return False
        try:
            self.queue.put_nowait((kind, value))
            return True
        except queue.Full:
            if kind == 'sample':
                self.rejected += 1
            self.error = '后台写入跟不上，缓冲队列已满；记录已停止。'
            self.stop(reason='queue_full')
            return False

    def append(self, record):
        if not self.accepting:
            return False
        # Records and nested camera snapshots are immutable after publication.
        # Copy only the outer map; no frame arrays are provided by the caller.
        item = dict(record)
        item['elapsed_s'] = float(record['host_monotonic_s']) - self.started
        if item['elapsed_s'] < 0:
            return False
        if self._offer('sample', item):
            self.submitted += 1
            self.note_saturation(record.get('roi_saturation', {}), emit=False)
            return True
        return False

    def note_saturation(self, state, emit=True):
        if not self.accepting or not state.get('checked') or state.get('saturated') is not True:
            return
        key = (state.get('checked_host_monotonic_s'), tuple(state.get('bounds') or ()))
        if key == self._last_saturation:
            return
        self._last_saturation = key
        snapshot = copy.deepcopy(state)
        self.saturation_history['detected'] = True
        self.saturation_history['detection_count'] += 1
        if self.saturation_history['first'] is None:
            self.saturation_history['first'] = snapshot
        self.saturation_history['last'] = snapshot
        if emit:
            self._offer('saturation', snapshot)

    def event(self, event):
        return self._offer('event', event)

    def marker(self, action, bounds=None):
        return self._offer('marker', {'action': action, 'utc': utc_now(),
                                     'elapsed_s': time.perf_counter() - self.started,
                                     'bounds': list(bounds) if bounds is not None else None})

    def stop(self, reason='user', context=None):
        if self.stop_info is None:
            self.stop_info = {'stopped_utc': utc_now(), 'duration_s': max(0, time.perf_counter() - self.started),
                              'reason': reason, 'manual_context_at_stop': copy.deepcopy(context or {})}
        self.accepting = False
        self.stop_requested.set()

    def status(self):
        with self.lock:
            return {'accepting': self.accepting, 'done': self.done.is_set(), 'error': self.error,
                    'submitted': self.submitted, 'written': self.written, 'flushed': self.flushed,
                    'queued': self.queue.qsize(), 'queue_limit': self.queue.maxsize,
                    'rejected': self.rejected, 'path': str(self.path)}

    def _run(self):
        stream = None
        try:
            stream = self.path.open('x', encoding='utf-8', newline='\n', buffering=256 * 1024)
            stream.write(encode({'type': 'start', 'schema': SCHEMA, 'software_version': '1.9',
                                 'started_utc': self.started_utc, 'start_host_monotonic_s': self.started,
                                 'queue_limit': self.queue.maxsize, 'flush_interval_s': FLUSH_SECONDS,
                                 'scope': 'GUI-delivered ROI curve samples; not every hardware frame',
                                 'calibration': {'status': 'uncalibrated', 'physical_value': None},
                                 'saturation_interval_s': 2.0}))
            stream.flush()
            os.fsync(stream.fileno())
            previous = None
            state_id = 0
            last_event_id = 0
            last_sync = time.monotonic()
            while not self.stop_requested.is_set() or not self.queue.empty():
                try:
                    batch = [self.queue.get(timeout=0.05)]
                except queue.Empty:
                    batch = []
                for unused in range(63):
                    try:
                        batch.append(self.queue.get_nowait())
                    except queue.Empty:
                        break
                lines = []
                batch_samples = 0
                for kind, item in batch:
                    events = item.get('metadata', {}).get('parameter_events', []) if kind == 'sample' else ([item] if kind == 'event' else [])
                    for event in events:
                        event_id = event.get('id', 0)
                        if event.get('host_monotonic_s', self.started) < self.started or event_id <= last_event_id:
                            continue
                        lines.append(encode({'type': 'parameter_event', 'event': event}))
                        last_event_id = event_id
                    if kind == 'event':
                        continue
                    if kind != 'sample':
                        lines.append(encode({'type': kind, 'value': item}))
                        continue
                    row = self.written + batch_samples + 1
                    params = parameter_log.parameter_state(item)
                    # Chunk values are already in each sample's frame metadata.
                    params.pop('frame_parameters', None)
                    if previous is None or params != previous:
                        state_id += 1
                        if previous is None:
                            lines.append(encode({'type': 'settings', 'id': state_id, 'first_data_row': row, 'values': params}))
                        else:
                            changed = {key: value for key, value in params.items() if key != 'nodes' and value != previous.get(key)}
                            nodes = params.get('nodes', {})
                            old_nodes = previous.get('nodes', {})
                            updates = {key: value for key, value in nodes.items() if value != old_nodes.get(key)}
                            lines.append(encode({'type': 'settings_change', 'id': state_id, 'base_id': state_id - 1,
                                                 'first_data_row': row, 'changed': changed, 'node_updates': updates,
                                                 'removed_nodes': [key for key in old_nodes if key not in nodes]}))
                        previous = params
                    meta = {key: value for key, value in item['metadata'].items()
                            if key not in ('nodes', 'conversion', 'chunk_configuration', 'parameter_events',
                                           'effective_parameters', 'node_read_times')}
                    lines.append(encode({'type': 'sample', 'data_row': row, 'csv_line': row + 1,
                                         'elapsed_s': item['elapsed_s'], 'sum_dn': int(item['sum_dn']),
                                         'bounds': list(item['bounds']), 'settings_id': state_id,
                                         'received_utc': item.get('received_utc'),
                                         'host_monotonic_s': item['host_monotonic_s'], 'frame_metadata': meta,
                                         'roi_saturation': item.get('roi_saturation', {'checked': False, 'status': 'not_checked'})}))
                    batch_samples += 1
                if lines:
                    stream.write(''.join(lines))
                    with self.lock:
                        self.written += batch_samples
                if time.monotonic() - last_sync >= FLUSH_SECONDS:
                    stream.flush()
                    os.fsync(stream.fileno())
                    with self.lock:
                        self.flushed = self.written
                    last_sync = time.monotonic()
            stream.write(encode({'type': 'end', 'sample_count': self.written, 'submitted_count': self.submitted,
                                 'rejected_samples': self.rejected, 'error': self.error,
                                 'stop': self.stop_info, 'saturation_history': self.saturation_history}))
            stream.flush()
            os.fsync(stream.fileno())
            with self.lock:
                self.flushed = self.written
        except Exception as exc:
            self.error = '记录写入失败：' + str(exc)
            self.accepting = False
            self.stop_requested.set()
        finally:
            if stream is not None:
                try:
                    stream.close()
                except OSError as exc:
                    self.error = self.error or str(exc)
            # Release every queued record/reference, even after a disk failure.
            while True:
                try:
                    self.queue.get_nowait()
                except queue.Empty:
                    break
            self.done.set()


def journal_items(path):
    """Yield only complete lines. A torn final line is recoverable, middle corruption is not."""
    with Path(path).open('r', encoding='utf-8') as stream:
        while True:
            line = stream.readline(16 * 1024 * 1024)
            if not line:
                break
            if len(line) >= 16 * 1024 * 1024 and not line.endswith('\n'):
                raise ValueError('缓存单行异常过大，已停止读取。')
            if not line.endswith('\n'):
                return
            item = json.loads(line)
            if not isinstance(item, dict):
                raise ValueError('记录缓存格式错误。')
            yield item


def inspect_journal(path):
    signature = file_signature(path)
    report = {'sample_count': 0, 'complete': False, 'saturation_history': empty_history(),
              'start': None, 'end': None}
    previous_time = -1.0
    last_saturation = None
    for item in journal_items(path):
        kind = item.get('type')
        if report['start'] is None:
            if kind != 'start' or item.get('schema') != SCHEMA:
                raise ValueError('不是支持的连续记录缓存。')
            report['start'] = item
            continue
        if report['complete']:
            raise ValueError('缓存结束标记后存在多余数据。')
        state = None
        if kind == 'sample':
            report['sample_count'] += 1
            if item['data_row'] != report['sample_count'] or item['elapsed_s'] < previous_time:
                raise ValueError('缓存数据行或时间顺序不一致。')
            previous_time = item['elapsed_s']
            state = item.get('roi_saturation', {})
        elif kind == 'saturation':
            state = item['value']
        elif kind == 'end':
            if item['sample_count'] != report['sample_count']:
                raise ValueError('缓存结束计数不一致。')
            report.update(complete=True, end=item)
            report['saturation_history'] = item.get('saturation_history', report['saturation_history'])
        if state and state.get('saturated'):
            key = (state.get('checked_host_monotonic_s'), tuple(state.get('bounds') or ()))
            if key != last_saturation:
                last_saturation = key
                history = report['saturation_history']
                history['detected'] = True
                history['detection_count'] += 1
                history['first'] = history['first'] or state
                history['last'] = state
    if report['start'] is None:
        raise ValueError('缓存未写入有效记录头。')
    if signature != file_signature(path):
        raise ValueError('缓存仍在变化，请停止对应记录后重试。')
    report['source_signature'] = signature
    return report


def file_signature(path):
    stat = Path(path).stat()
    return (stat.st_size, stat.st_mtime_ns)


TXT_INTRO = '''FLIR 连续记录 1.9：精简参数TXT
CSV时间从点击开始记录计时，包含暂停采集的间隔；不受60秒显示窗口影响。
改变选区/清空曲线不删除连续记录，每行保存当时坐标和大小。
TXT只记录初始参数、变化和参数操作；不含光强数据、逐点帧信息或像素。
initial_parameters.values为初始值；parameter_change.changed覆盖变化键，removed删除失效键。
csv_line为含表头的CSV文件行；变化从该行起适用；参数不变不重复写入。
camera.*为节点读回，frame.*为随帧Chunk参数，image.*为格式/尺寸。自动曝光/增益变化仍逐次保留。
完整节点每2秒或操作后读回，可能复用；节点读回不能证明在传感器曝光时刻生效。
software_roi单位px，硬件ROI/binning/decimation另列；selector参数只代表当前selector。
conversion记录统计链路缩放；value=null为未知，replay_simulation为回放。未标定，DN不直接等于物理功率。
过曝只保留发生标记及检查次数；2秒或操作触发的抽检不能保证发现每个短暂峰值。
恢复仅导出完整落盘的行；不保证包含每个相机帧。source_complete/recording_error标记异常停止。
参数采用逐行JSON，光强数据仅在配对CSV中；csv_sha256用于校验。
--- BEGIN JSONL ---
'''


def _temp_path(directory):
    fd, name = tempfile.mkstemp(prefix='.flir_record_', suffix='.tmp', dir=str(directory))
    os.close(fd)
    return Path(name)


def export_journal(source, destination, expected_signature=None):
    """Background streaming export; rollback ordinary pair-commit failures."""
    source, destination = Path(source), Path(destination)
    if destination.suffix.lower() != '.csv':
        destination = destination.with_suffix('.csv')
    txt = destination.with_suffix('.txt')
    if source.resolve() in (destination.resolve(), txt.resolve()):
        raise ValueError('导出路径不能覆盖缓存。')
    report = inspect_journal(source)
    if expected_signature is not None and tuple(expected_signature) != report['source_signature']:
        raise ValueError('缓存已改变，需要重新核对并确认后保存。')
    if report['sample_count'] == 0:
        raise ValueError('此次记录没有有效选区数据。缓存仍保留。')
    staged, backups, committed = {}, {}, []
    try:
        staged[destination] = _temp_path(destination.parent)
        staged[txt] = _temp_path(txt.parent)
        digest = hashlib.sha256()
        with staged[destination].open('wb') as out:
            class Sink:
                def write(self, text):
                    raw = text.encode('utf-8')
                    digest.update(raw)
                    out.write(raw)
            out.write(b'\xef\xbb\xbf')
            digest.update(b'\xef\xbb\xbf')
            writer = csv.writer(Sink())
            writer.writerow(parameter_log.CSV_HEADER)
            for item in journal_items(source):
                if item.get('type') == 'sample':
                    x0, y0, x1, y1 = item['bounds']
                    writer.writerow((item['elapsed_s'], item['sum_dn'], x0, y0, x1-x0, y1-y0))
            out.flush()
            os.fsync(out.fileno())
        summary = {'type': 'export_summary', 'schema_version': 2, 'software_version': '1.9', 'exported_utc': utc_now(),
                   'csv_name': destination.name, 'csv_sha256': digest.hexdigest(),
                   'sample_count': report['sample_count'], 'source_complete': report['complete'],
                   'recording_error': (report.get('end') or {}).get('error'),
                   'started_utc': report['start'].get('started_utc'),
                   'stop': (report.get('end') or {}).get('stop'),
                   'calibration': {'status': 'uncalibrated', 'physical_value': None},
                   'saturation_history': compact_parameters.saturation_summary(report['saturation_history'])}
        with staged[txt].open('w', encoding='utf-8-sig', newline='\n', buffering=256 * 1024) as out:
            out.write(TXT_INTRO)
            out.write(encode(summary))
            for item in compact_parameters.journal_parameters(journal_items(source)):
                out.write(encode(item))
            out.write('--- END JSONL ---\n')
            out.flush()
            os.fsync(out.fileno())
        if file_signature(source) != report['source_signature']:
            raise ValueError('缓存仍在变化，已取消导出以避免CSV/TXT不一致。')
        for target in (destination, txt):
            if target.exists():
                if not target.is_file():
                    raise IsADirectoryError(str(target))
                backup = _temp_path(target.parent)
                backups[target] = backup
                shutil.copyfile(str(target), str(backup))
        for target in (destination, txt):
            os.replace(str(staged[target]), str(target))
            del staged[target]
            committed.append(target)
    except BaseException as error:
        recovery_errors = []
        for target in reversed(committed):
            try:
                if target in backups:
                    os.replace(str(backups[target]), str(target))
                    del backups[target]
                else:
                    target.unlink(missing_ok=True)
            except OSError as recovery:
                recovery_errors.append(str(recovery))
        if recovery_errors:
            paths = list(map(str, backups.values()))
            backups.clear()
            raise OSError('恢复原文件失败，备份保留于：' + repr(paths)) from error
        raise
    finally:
        for temporary in list(staged.values()) + list(backups.values()):
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    return {'csv': str(destination), 'txt': str(txt), **summary}


def remove_saved_cache(source):
    # Only delete this named journal after successful paired export; no recursive delete.
    source = Path(source)
    source.unlink(missing_ok=True)
    try:
        source.parent.rmdir()
    except OSError:
        pass


class BackgroundJob:
    def __init__(self, function, *args):
        self.done = threading.Event()
        self.result = None
        self.error = None
        def run():
            try:
                self.result = function(*args)
            except Exception as exc:
                self.error = str(exc)
            finally:
                self.done.set()
        self.thread = threading.Thread(target=run, name='FLIR-record-export', daemon=True)
        self.thread.start()
