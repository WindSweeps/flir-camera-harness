"""Export regression: size follows parameter changes, never curve sample count."""
import copy
import csv
import hashlib
import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import compact_parameters as compact
import continuous_recording as recording
import parameter_log


def main():
    output = Path(tempfile.mkdtemp(prefix='compact_', dir=Path(__file__).parent))
    cases = []
    bounds = (10, 20, 110, 220)

    def record(i, exposure=20000, chunk=21000):
        return {'elapsed_s': i / 100, 'sum_dn': 999888000 + i, 'bounds': bounds,
                'received_utc': '2026-11-13T08:00:{:02d}+00:00'.format(i % 60),
                'host_monotonic_s': 5000 + i / 100, 'context': {'wavelength_nm': 532},
                'metadata': {'source': 'camera', 'session_id': 'test',
                    'nodes': {'ExposureTime': {'value': exposure, 'unit': 'us', 'status': 'ok',
                                               'read_at': {'utc': str(i)}},
                              'Gain': {'value': 0.0, 'unit': 'dB', 'status': 'ok'},
                              'BinningHorizontal': {'value': 2, 'status': 'ok'},
                              'BinningVertical': {'value': 2, 'status': 'ok'}},
                    'chunks': {'ExposureTime': {'value': chunk, 'unit': 'us', 'status': 'ok'},
                               'FrameID': {'value': i}, 'Timestamp': {'value': i * 100000}},
                    'image_info': {'FrameID': {'value': i}, 'Width': {'value': 640},
                                   'PixelFormat': {'value': 'Mono16'}},
                    'readback_cache': {'age_s': i * 0.01},
                    'conversion': {'input_dtype': 'uint16', 'scaling': {'method': 'dtype_max', 'denominator': 65535}}}}

    def export_rows(name, rows, events=()):
        path = output / (name + '.csv')
        samples = [(r['elapsed_s'], r['sum_dn']) for r in rows]
        parameter_log.write_export_bundle(path, samples, bounds, rows, events=events)
        text = path.with_suffix('.txt').read_text(encoding='utf-8-sig')
        report = json.loads(text.split('--- BEGIN JSON ---\n')[1].split('\n--- END JSON ---')[0])
        assert report['csv_sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()
        assert len(list(csv.reader(path.open(encoding='utf-8-sig')))) == len(rows) + 1
        for forbidden in ('"samples"', '"sum_dn"', '"frame_metadata"', '"FrameID"', '"Timestamp"', '"readback_cache"'):
            assert forbidden not in text, forbidden
        return path, report

    short, small = export_rows('static_50', [record(i) for i in range(50)])
    long, large = export_rows('static_5000', [record(i) for i in range(5000)])
    assert len(small['parameter_timeline']) == len(large['parameter_timeline']) == 1
    short_size, long_size = short.with_suffix('.txt').stat().st_size, long.with_suffix('.txt').stat().st_size
    assert abs(long_size - short_size) < 30 and long_size < 6000
    cases.append({'name': '5000_samples_do_not_grow_txt', 'short_txt_bytes': short_size, 'long_txt_bytes': long_size})

    rows = [record(0), record(1), record(2, 40000, 42000), record(3, 20000, 21000)]
    path, changed = export_rows('return_and_auto', rows)
    timeline = changed['parameter_timeline']
    assert [p['csv_line'] for p in timeline] == [2, 4, 5]
    assert set(timeline[1]['changed']) == {'camera.ExposureTime', 'frame.ExposureTime'}
    assert timeline[2]['changed']['frame.ExposureTime']['value'] == 21000
    cases.append({'name': 'A_B_A_and_chunk_changes_preserved'})

    events = [{'id': i, 'action': 'set_exposure', 'status': 'ok',
               'started_at': {'utc': '2026-11-13T08:01:00+00:00'},
               'request': {'value': value}, 'readback': {'nodes': record(0, value)['metadata']['nodes'],
                                                        'read_started': {'unix_ns': i}}}
              for i, value in enumerate((20000, 40000, 20000), 1)]
    _, report = export_rows('no_new_frames', [record(0)], events)
    assert len(report['parameter_events']) == 3
    assert report['parameter_events'][2]['readback']['changed'] == {'camera.ExposureTime': {'value': 20000, 'unit': 'us'}}
    cases.append({'name': 'operations_without_frames_keep_only_parameter_deltas'})

    tracker = compact.Changes()
    first = compact.from_record(record(0))
    tracker.observe(first)
    unavailable = record(1)
    unavailable['metadata']['nodes']['ExposureTime'] = {'status': 'unavailable'}
    unavailable['metadata']['chunks'].pop('ExposureTime')
    delta = tracker.observe(compact.from_record(unavailable))
    assert delta['removed'] == ['frame.ExposureTime']
    assert delta['changed']['camera.ExposureTime'] == {'value': None, 'status': 'unavailable'}
    assert 'camera.Gain' not in delta['changed']
    cases.append({'name': 'unknown_removed_and_valid_zero_parameters'})

    # Deliberately use the pre-1.8 cache schema and per-sample chunks. Recovery
    # export must also be compact, while the retained cache remains recoverable.
    journal = output / 'old_cache.jsonl'
    items = [{'type': 'start', 'schema': recording.SCHEMA, 'started_utc': '2026-11-13T00:00:00Z'}]
    current = None
    state_id = 0
    for i, chunk in enumerate((21000, 21000, 22000, 21000)):
        row = record(i, chunk=chunk)
        state = parameter_log.parameter_state(row)
        state.pop('frame_parameters')
        # read_at changes are deliberately ignored by final TXT projection.
        if current is None:
            state_id += 1
            items.append({'type': 'settings', 'id': state_id, 'values': state})
        else:
            state_id += 1
            items.append({'type': 'settings_change', 'id': state_id, 'base_id': state_id - 1,
                          'changed': {}, 'node_updates': state['nodes'], 'removed_nodes': []})
        current = state
        items.append({'type': 'sample', 'data_row': i + 1, 'settings_id': state_id,
                      **{k: row[k] for k in ('elapsed_s', 'sum_dn', 'bounds', 'received_utc')},
                      'frame_metadata': row['metadata']})
    items.append({'type': 'end', 'sample_count': 4})
    journal.write_text(''.join(recording.encode(i) for i in items), encoding='utf-8')
    saved = recording.export_journal(journal, output / 'recovered.csv')
    text = Path(saved['txt']).read_text(encoding='utf-8-sig')
    objects = [json.loads(line) for line in text.split('--- BEGIN JSONL ---\n')[1].split('--- END JSONL ---')[0].splitlines()]
    timeline = [i for i in objects if i['type'] in ('initial_parameters', 'parameter_change')]
    assert len(timeline) == 3 and [i['csv_line'] for i in timeline] == [2, 4, 5]
    assert 'sum_dn' not in text and 'frame_metadata' not in text
    assert journal.read_text(encoding='utf-8').count('"type":"sample"') == 4
    cases.append({'name': 'old_cache_recovery_preserves_chunks_without_exporting_samples'})

    corrupt = copy.deepcopy(items)
    next(i for i in corrupt if i['type'] == 'sample')['settings_id'] = 999
    journal.write_text(''.join(recording.encode(i) for i in corrupt), encoding='utf-8')
    original_csv, original_txt = Path(saved['csv']).read_bytes(), Path(saved['txt']).read_bytes()
    try:
        recording.export_journal(journal, saved['csv'])
        raise AssertionError('Missing parameter linkage must fail')
    except ValueError:
        pass
    assert Path(saved['csv']).read_bytes() == original_csv and Path(saved['txt']).read_bytes() == original_txt
    assert not list(output.glob('.flir*'))
    cases.append({'name': 'broken_state_link_aborts_without_overwriting_pair'})
    report = {'status': 'passed', 'cases': cases, 'output': str(output)}
    (output / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
