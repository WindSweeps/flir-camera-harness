"""No-hardware, bounded-memory recording/storage tests."""


def run_recording_tests():
    import csv
    import hashlib
    import json
    import os
    import threading
    import time
    import traceback
    from pathlib import Path
    import continuous_recording as storage

    output = Path.cwd() / '_recording_test_output'
    output.mkdir(exist_ok=True)
    result = {'status': 'running', 'hardware_access': False, 'cases': []}
    live = []

    def passed(name, **details):
        result['cases'].append({'name': name, 'status': 'passed', **details})

    def make_record(i, start, exposure=20000, bounds=(1, 2, 4, 6)):
        return {'elapsed_s': i % 60, 'sum_dn': 2**40 + i, 'bounds': bounds,
                'host_monotonic_s': start + i + .01, 'received_utc': '2026-11-12T00:00:00+00:00',
                'context': {'calibration_status': 'uncalibrated'},
                'roi_saturation': {'checked': False, 'status': 'not_checked', 'saturated': None},
                'metadata': {'source': 'camera', 'session_id': 'test',
                             'nodes': {'ExposureTime': {'status': 'ok', 'value': exposure, 'unit': 'us'},
                                       'Gain': {'status': 'ok', 'value': 0.0, 'unit': 'dB'}},
                             'conversion': {'input_dtype': 'uint8'}, 'chunks': {}}}

    def finish(rec):
        rec.stop()
        assert rec.done.wait(10), 'writer did not finish'
        assert not rec.error, rec.error
        return storage.inspect_journal(rec.path)

    def recorder(**kwargs):
        obj = storage.Recorder(root=output / 'cache', **kwargs)
        live.append(obj)
        return obj

    original_encode, original_replace = storage.encode, storage.os.replace
    try:
        thread_ids = set()
        producer = threading.get_ident()
        def tracked_encode(value):
            thread_ids.add(threading.get_ident())
            return original_encode(value)
        storage.encode = tracked_encode
        rec = recorder(queue_limit=512)
        for i in range(360):
            exposure = 20000 if i < 120 or i >= 240 else 40000
            assert rec.append(make_record(i, rec.started, exposure, (1, 2, 4, 6) if i < 180 else (0, 0, 2, 2)))
        assert rec.queue.maxsize == 512
        report = finish(rec)
        assert report['sample_count'] == 360 and rec.written == rec.flushed == 360
        assert producer not in thread_ids, 'JSON serialization ran on producer thread'
        items = list(storage.journal_items(rec.path))
        samples = [item for item in items if item['type'] == 'sample']
        assert samples[-1]['elapsed_s'] > 359 and samples[-1]['elapsed_s'] > samples[0]['elapsed_s']
        assert samples[180]['bounds'] == [0, 0, 2, 2]
        settings = [item for item in items if item['type'].startswith('settings')]
        assert len(settings) == 4, 'unchanged full settings were duplicated for every sample'
        assert any(item.get('node_updates', {}).get('ExposureTime', {}).get('value') == 40000 for item in settings)
        assert settings[-1]['node_updates']['ExposureTime']['value'] == 20000
        storage.encode = original_encode
        passed('six_minute_history_and_roi_changes_survive_60s_limit_with_worker_serialization', settings_records=len(settings))

        target = output / 'long_record.csv'
        exported = storage.export_journal(rec.path, target, report['source_signature'])
        rows = list(csv.reader(target.open(encoding='utf-8-sig', newline='')))
        assert len(rows) == 361 and int(rows[-1][1]) == 2**40 + 359
        assert float(rows[-1][0]) > 359 and rows[181][2:] == ['0', '0', '2', '2']
        text = target.with_suffix('.txt').read_text(encoding='utf-8-sig')
        lines = text.split('--- BEGIN JSONL ---\n')[1].split('--- END JSONL ---')[0].splitlines()
        parsed = [json.loads(line) for line in lines]
        assert parsed[0]['csv_sha256'] == hashlib.sha256(target.read_bytes()).hexdigest() == exported['csv_sha256']
        assert parsed[0]['sample_count'] == 360 and parsed[0]['source_complete']
        assert not any('mask' in item or 'pixel_data' in item for item in parsed)
        assert not any(item.get('type') == 'sample' for item in parsed)
        assert 'sum_dn' not in text and 'frame_metadata' not in text
        parameter_items = [item for item in parsed if item.get('type') in ('initial_parameters', 'parameter_change')]
        assert len(parameter_items) == 4
        assert [item['csv_line'] for item in parameter_items] == [2, 122, 182, 242]
        assert parameter_items[-1]['changed'] == {'camera.ExposureTime': {'value': 20000, 'unit': 'us'}}
        passed('streaming_csv_txt_pair_matches_all_rows_and_hash_without_pixels')

        rec2 = recorder()
        hot = {'checked': True, 'saturated': True, 'checked_host_monotonic_s': rec2.started + .1,
               'bounds': [0, 0, 2, 2], 'count': 1, 'total': 4, 'fraction': .25, 'threshold': 255}
        rec2.note_saturation(hot)
        row = make_record(1, rec2.started)
        row['roi_saturation'] = hot
        rec2.append(row)
        rec2.marker('curve_reset_or_roi_change', (0, 0, 2, 2))
        event = {'id': 100, 'host_monotonic_s': rec2.started + .5, 'action': 'set_gain_db', 'request': {'value': 6}}
        rec2.event(event)
        rec2.event(event)
        report2 = finish(rec2)
        assert report2['saturation_history']['detected'] and report2['saturation_history']['detection_count'] == 1
        assert len([item for item in storage.journal_items(rec2.path) if item['type'] == 'parameter_event']) == 1
        assert rec2.append(make_record(2, rec2.started)) is False
        passed('recording_saturation_survives_curve_reset_and_parameter_events_are_deduplicated')

        # Deliberately stall serialization to prove enqueue never waits on disk.
        entered, release = threading.Event(), threading.Event()
        def stalled(value):
            if value.get('type') == 'start':
                entered.set()
                assert release.wait(5)
            return original_encode(value)
        storage.encode = stalled
        blocked = recorder(queue_limit=2)
        assert entered.wait(3)
        began = time.perf_counter()
        assert blocked.append(make_record(0, blocked.started))
        assert blocked.append(make_record(1, blocked.started))
        assert not blocked.append(make_record(2, blocked.started))
        elapsed = time.perf_counter() - began
        assert elapsed < .25 and not blocked.accepting and blocked.error
        release.set()
        assert blocked.done.wait(5)
        storage.encode = original_encode
        overload = storage.inspect_journal(blocked.path)
        assert overload['sample_count'] == 2 and overload['end']['rejected_samples'] == 1
        assert blocked.queue.empty()
        passed('bounded_queue_overload_stops_visibly_without_blocking_or_silent_drop', enqueue_seconds=elapsed)

        # Simulated serialization/disk-path failure leaves a recoverable prefix.
        def failed_write(value):
            if value.get('type') == 'sample':
                raise OSError('simulated disk failure')
            return original_encode(value)
        storage.encode = failed_write
        broken = recorder()
        broken.append(make_record(0, broken.started))
        broken.stop()
        assert broken.done.wait(5) and broken.error and not broken.accepting
        assert broken.path.exists() and broken.queue.empty()
        storage.encode = original_encode
        passed('writer_failure_retains_journal_and_releases_pending_references')

        torn = output / 'torn.jsonl'
        raw_lines = rec.path.read_bytes().splitlines(keepends=True)
        torn.write_bytes(b''.join(raw_lines[:-1]) + b'{"type":"sample","data_row":')
        recovered = storage.inspect_journal(torn)
        assert recovered['sample_count'] == 360 and not recovered['complete']
        restored = storage.export_journal(torn, output / 'recovered.csv')
        assert restored['sample_count'] == 360 and not restored['source_complete']
        corrupt = output / 'corrupt.jsonl'
        corrupt.write_bytes(raw_lines[0] + b'bad json\n' + raw_lines[-1])
        try:
            storage.inspect_journal(corrupt)
            raise AssertionError('middle corruption silently skipped')
        except json.JSONDecodeError:
            pass
        passed('crash_recovery_uses_complete_lines_and_rejects_middle_corruption')

        stale_signature = storage.file_signature(torn)
        with torn.open('ab') as out:
            out.write(b' ')
        try:
            storage.export_journal(torn, output / 'stale.csv', stale_signature)
            raise AssertionError('changed snapshot exported after consent')
        except ValueError:
            pass
        assert not (output / 'stale.csv').exists()
        passed('changed_cache_requires_new_review_before_export')

        rollback = output / 'rollback.csv'
        rollback_txt = rollback.with_suffix('.txt')
        rollback.write_bytes(b'original csv')
        rollback_txt.write_bytes(b'original txt')
        failed = [False]
        def fail_second(src, dst):
            if Path(dst) == rollback_txt and not failed[0]:
                failed[0] = True
                raise OSError('second commit failed')
            return original_replace(src, dst)
        storage.os.replace = fail_second
        try:
            storage.export_journal(rec.path, rollback)
            raise AssertionError('commit error hidden')
        except OSError:
            pass
        finally:
            storage.os.replace = original_replace
        assert rollback.read_bytes() == b'original csv' and rollback_txt.read_bytes() == b'original txt'
        assert rec.path.exists() and not list(output.glob('.flir_record_*.tmp'))
        passed('second_file_commit_failure_rolls_back_pair_and_keeps_cache')
        result['status'] = 'passed'
    except Exception:
        result['status'] = 'failed'
        result['traceback'] = traceback.format_exc()
        raise
    finally:
        storage.encode, storage.os.replace = original_encode, original_replace
        for rec in live:
            rec.stop()
            rec.done.wait(5)
        (output / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return result
