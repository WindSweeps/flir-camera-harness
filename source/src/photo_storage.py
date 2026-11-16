"""Bounded asynchronous raw grayscale TIFF + parameter TXT saving.

TIFF6: little-endian, one uncompressed strip, unsigned8/16bit grayscale.
No image library conversions, display scaling, JPEG, or retained image history.
"""
import json
import os
import queue
import re
import struct
import threading
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
import numpy as np

MAX_FRAMES = 8
MAX_BYTES = 64 * 1024 * 1024


class PhotoError(RuntimeError):
    pass


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def write_tiff(path, pixels, metadata):
    """Write a standard TIFF without narrowing raw unsigned pixel values."""
    if pixels.ndim != 2 or pixels.size == 0 or pixels.dtype.kind != 'u' or pixels.dtype.itemsize not in (1, 2):
        raise PhotoError('照片保存目前支持8/16位灰度原始图像。')
    height, width = pixels.shape
    description = json.dumps(metadata, ensure_ascii=True, separators=(',', ':'), allow_nan=False).encode('ascii') + b'\0'
    count = 16
    description_offset = 8 + 2 + 12 * count + 4
    resolution_offset = (description_offset + len(description) + 1) // 2 * 2
    pixel_offset = resolution_offset + 16
    byte_count = int(pixels.nbytes)
    if pixel_offset + byte_count >= 2**32:
        raise PhotoError('单张TIFF超过本版本支持的4GB上限。')
    entries = [(256, 4, 1, width), (257, 4, 1, height), (258, 3, 1, pixels.dtype.itemsize * 8),
               (259, 3, 1, 1), (262, 3, 1, 1), (270, 2, len(description), description_offset),
               (273, 4, 1, pixel_offset), (274, 3, 1, 1), (277, 3, 1, 1),
               (278, 4, 1, height), (279, 4, 1, byte_count),
               (282, 5, 1, resolution_offset), (283, 5, 1, resolution_offset + 8),
               (284, 3, 1, 1), (296, 3, 1, 1), (339, 3, 1, 1)]
    with Path(path).open('xb', buffering=256 * 1024) as stream:
        stream.write(struct.pack('<2sHIH', b'II', 42, 8, count))
        for entry in entries:
            stream.write(struct.pack('<HHII', *entry))
        stream.write(struct.pack('<I', 0))
        stream.write(description)
        stream.write(b'\0' * (resolution_offset - stream.tell()))
        stream.write(struct.pack('<IIII', 1, 1, 1, 1))
        stream.write(memoryview(pixels).cast('B'))
        stream.flush()
        os.fsync(stream.fileno())


def write_pair(directory, sequence, pixels, information):
    name = '{}_{}_{:06d}'.format(information['system_time_filename'], information['shoot_name'], sequence)
    image_path, text_path = directory / (name + '.tif'), directory / (name + '.txt')
    info = dict(information, sequence=sequence, image_file=image_path.name,
                stored_dtype=str(pixels.dtype), stored_shape=list(pixels.shape),
                pixel_source='SDK raw ndarray before preview conversion; not a screenshot',
                software_version='1.9', calibration={'status':'uncalibrated', 'physical_value':None})
    image_temp, text_temp = directory / (name + '.tif.part'), directory / (name + '.txt.part')
    write_tiff(image_temp, pixels, info)
    with text_temp.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(info, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    # Unique per-session folder and monotonic names. Never overwrite old photos.
    if image_path.exists() or text_path.exists():
        raise PhotoError('照片文件名冲突，已停止保存。')
    os.rename(str(image_temp), str(image_path))
    os.rename(str(text_temp), str(text_path))
    return info


class Session:
    def __init__(self, root, shoot_name='拍摄', max_frames=MAX_FRAMES, max_bytes=MAX_BYTES, writer=write_pair):
        shoot_name = str(shoot_name).strip()
        if not shoot_name or len(shoot_name) > 60 or re.search(r'[<>:"/\\|?*\x00-\x1f]', shoot_name):
            raise PhotoError('拍摄名称不能为空，最多60字，不能包含文件名禁用字符 < > : " / \\ | ? *。')
        self.shoot_name = shoot_name
        root = Path(root).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone(timedelta(hours=8))).strftime('%Y%m%d_%H%M%S')
        self.path = root / (stamp + '_' + uuid.uuid4().hex[:8])
        self.path.mkdir()
        self.lock, self.done = threading.RLock(), threading.Event()
        self.queue = queue.Queue(max_frames)
        self.max_frames, self.max_bytes, self.writer = max_frames, max_bytes, writer
        self.accepting, self.error = True, None
        self.submitted = self.written = self.rejected = self.pending = self.pending_bytes = 0
        self.last_frame_id, self.id_gaps, self.missing_frame_ids = None, [], 0
        self.started_utc, self.last_file = utc_now(), None
        self.manifest = (self.path / 'photos.jsonl').open('x', encoding='utf-8', newline='\n')
        self.manifest.write(json.dumps({'type':'start', 'version':'1.9', 'started_utc':self.started_utc,
            'max_frames':max_frames, 'max_bytes':max_bytes, 'shoot_name':shoot_name,
            'filename':'YYYYMMDD-HHMMSS-mmm_name_sequence; system frame reception time UTC+08:00',
            'format':'uncompressed unsigned grayscale TIFF',
            'scope':'received complete external-trigger frames; no guarantee of one frame per electrical pulse'}) + '\n')
        self.manifest.flush()
        os.fsync(self.manifest.fileno())
        self.thread = threading.Thread(target=self._run, name='FLIR-photo-writer', daemon=True)
        self.thread.start()

    def fail(self, message):
        with self.lock:
            if self.error is None:
                self.error = str(message)
            self.accepting = False
        return PhotoError(str(message))

    def reject(self, message):
        with self.lock:
            self.rejected += 1
        return self.fail(message)

    def offer(self, array, metadata, context=None, saturation=None):
        if array.ndim != 2 or array.size == 0 or array.dtype.kind != 'u' or array.dtype.itemsize not in (1, 2):
            with self.lock:
                self.rejected += 1
            raise self.fail('原始图像不是支持的8/16位灰度格式，未转换或丢弃后继续采集。')
        size = int(array.nbytes)
        with self.lock:
            if not self.accepting:
                self.rejected += 1
                raise PhotoError(self.error or '照片保存已停止。')
            if self.pending >= self.max_frames or self.pending_bytes + size > self.max_bytes:
                self.rejected += 1
                raise self.fail('照片写入缓存已满，写入速度跟不上采集；请降低触发频率或选择更快的磁盘。')
            self.pending += 1
            self.pending_bytes += size
            self.submitted += 1
            sequence = self.submitted
        try:
            # Exactly one extra raw copy, only while saving; SDK may release immediately.
            pixels = np.array(array, dtype='<u2' if array.dtype.itemsize == 2 else 'u1', copy=True, order='C')
            meta = dict(metadata)
            meta['conversion'] = dict(metadata.get('conversion', {}))
            timestamp = (meta.get('image_retrieved_at') or meta.get('captured_host') or {}).get('utc') or utc_now()
            captured_time = datetime.fromisoformat(timestamp.replace('Z', '+00:00')).astimezone(timezone(timedelta(hours=8)))
            time_token = captured_time.strftime('%Y%m%d-%H%M%S-') + '{:03d}'.format(captured_time.microsecond // 1000)
            frame_id = meta.get('image_info', {}).get('FrameID', {}).get('value')
            gap = None
            with self.lock:
                if isinstance(frame_id, int):
                    if self.last_frame_id is not None and frame_id != self.last_frame_id + 1:
                        gap = {'after':self.last_frame_id, 'received':frame_id, 'sequence':sequence}
                        self.id_gaps.append(gap)
                        self.id_gaps = self.id_gaps[-16:]
                    self.last_frame_id = frame_id
                else:
                    self.missing_frame_ids += 1
            info = {'received_utc':timestamp, 'system_time_filename':time_token, 'shoot_name':self.shoot_name,
                    'system_time_basis':'host GetNextImage return time; actual camera timestamp retained separately',
                    'camera':meta, 'context':context or {},
                    'roi_saturation':saturation or {'status':'not_checked'}, 'frame_id_gap':gap}
            self.queue.put_nowait((sequence, pixels, info))
        except BaseException as exc:
            with self.lock:
                self.pending -= 1
                self.pending_bytes -= size
            raise self.fail('无法缓存照片：' + str(exc)) from exc

    def stop(self):
        with self.lock:
            self.accepting = False

    def status(self):
        with self.lock:
            return dict(path=str(self.path), submitted=self.submitted, written=self.written,
                pending=self.pending, pending_bytes=self.pending_bytes, rejected=self.rejected,
                unsaved=self.submitted-self.written+self.rejected, error=self.error,
                accepting=self.accepting, done=self.done.is_set(), frame_id_gaps=list(self.id_gaps),
                missing_frame_ids=self.missing_frame_ids, last_file=self.last_file)

    def _run(self):
        failed_write = False
        try:
            while True:
                try:
                    item = self.queue.get(timeout=.05)
                except queue.Empty:
                    with self.lock:
                        if not self.accepting and self.pending == 0:
                            break
                    continue
                sequence, pixels, info = item
                try:
                    if not failed_write:
                        saved = self.writer(self.path, sequence, pixels, info)
                        with self.lock:
                            self.written += 1
                            self.last_file = saved['image_file']
                        self.manifest.write(json.dumps({'type':'photo', 'sequence':sequence,
                            'image':saved['image_file'], 'frame_id_gap':saved['frame_id_gap'],
                            'received_utc':saved['received_utc']}, ensure_ascii=False) + '\n')
                        self.manifest.flush()
                except BaseException as exc:
                    failed_write = True
                    self.fail('写入照片失败：' + str(exc))
                finally:
                    with self.lock:
                        self.pending -= 1
                        self.pending_bytes -= int(pixels.nbytes)
                    self.queue.task_done()
                    del item, pixels, info
            self.manifest.write(json.dumps(dict(type='end', ended_utc=utc_now(), **self.status()), ensure_ascii=False) + '\n')
            self.manifest.flush()
            os.fsync(self.manifest.fileno())
        except BaseException as exc:
            self.fail('照片清单写入失败：' + str(exc))
        finally:
            try:
                self.manifest.close()
            finally:
                self.done.set()
