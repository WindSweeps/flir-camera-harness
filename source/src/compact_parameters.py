"""Parameter-only export projection. Never serializes curve samples or frames."""


def entry(value):
    if not isinstance(value, dict):
        return {'value': value}
    result = {'value': value.get('value')}
    if value.get('unit'):
        result['unit'] = value['unit']
    status = value.get('status')
    if status and status != 'ok':
        result['status'] = status
    return result


def node_values(nodes, prefix='camera.'):
    return {prefix + key: entry(value) for key, value in nodes.items()}


def from_record(record):
    meta = record.get('metadata', {})
    values = node_values(meta.get('nodes', {}))
    values['source'] = meta.get('source', 'unknown')
    values['session_id'] = meta.get('session_id')
    bounds = record.get('bounds')
    values['software_roi'] = (dict(x=bounds[0], y=bounds[1],
                                  width=bounds[2] - bounds[0], height=bounds[3] - bounds[1])
                              if bounds is not None else None)
    values['manual_context'] = record.get('context', {})
    if meta.get('conversion') is not None:
        values['conversion'] = meta['conversion']
    if meta.get('sdk_version') is not None:
        values['sdk_version'] = entry(meta['sdk_version'])
    # Explicit whitelist: FrameID, Timestamp, cache age, extrema, intensity and
    # per-frame diagnostics are not parameters and must not enter the TXT.
    for name in ('ExposureTime', 'Gain', 'BlackLevel', 'Width', 'Height', 'OffsetX', 'OffsetY'):
        if name in meta.get('chunks', {}):
            values['frame.' + name] = entry(meta['chunks'][name])
    for name in ('PixelFormat', 'Width', 'Height', 'BitsPerPixel'):
        if name in meta.get('image_info', {}):
            values['image.' + name] = entry(meta['image_info'][name])
    return values


class Changes:
    """One initial state, then changed keys only; memory independent of duration."""
    def __init__(self):
        self.previous = None

    def observe(self, values, **when):
        if self.previous is None:
            item = {'type': 'initial_parameters', **when, 'values': values}
        else:
            changed = {key: value for key, value in values.items()
                       if key not in self.previous or value != self.previous[key]}
            removed = [key for key in self.previous if key not in values]
            if not changed and not removed:
                return None
            item = {'type': 'parameter_change', **when, 'changed': changed}
            if removed:
                item['removed'] = removed
        self.previous = values
        return item


def at_sample(record, row):
    when = {'csv_line': row + 1, 'elapsed_s': record['elapsed_s']}
    utc = record.get('received_utc')
    if utc:
        when['utc'] = utc
    return when


def event_record(event, changes):
    """Keep operations even without new frames; drop duplicated readback detail."""
    result = {'type': 'parameter_event'}
    for key in ('id', 'session_id', 'action', 'operation', 'status', 'request', 'error',
                'requested_us', 'requested_db'):
        if key in event:
            result[key] = event[key]
    result['utc'] = event.get('utc') or event.get('started_at', {}).get('utc')
    readback = event.get('readback')
    if isinstance(readback, dict):
        nodes = readback.get('nodes')
        if nodes is None:
            nodes = {key: value for key, value in readback.items()
                     if isinstance(value, dict) and ('value' in value or 'unit' in value)}
        if nodes:
            delta = changes.observe(node_values(nodes))
            if delta:
                result['readback'] = delta
        elif readback.get('error') or readback.get('status'):
            result['readback_status'] = readback.get('status')
            result['readback_error'] = readback.get('error')
    return result


def saturation_summary(history):
    history = history or {}
    return {'detected': bool(history.get('detected')),
            'detection_count': history.get('detection_count', 0),
            'inspection': 'sampled_every_2s_and_on_operations; not_checked_is_unknown'}


def journal_parameters(items):
    """Project old/new recording caches without copying sample data into TXT."""
    state = None
    state_id = None
    changes, event_changes = Changes(), Changes()
    for item in items:
        kind = item.get('type')
        if kind == 'settings':
            state, state_id = item['values'], item['id']
        elif kind == 'settings_change':
            if state is None or item['base_id'] != state_id:
                raise ValueError('参数缓存顺序无效，无法可靠关联曲线。')
            state = {**state, **item.get('changed', {})}
            nodes = {**state.get('nodes', {}), **item.get('node_updates', {})}
            for key in item.get('removed_nodes', []):
                nodes.pop(key, None)
            state['nodes'] = nodes
            state_id = item['id']
        elif kind == 'sample':
            if state is None or item['settings_id'] != state_id:
                raise ValueError('曲线缺少对应参数缓存，已取消导出。')
            metadata = {**item.get('frame_metadata', {}),
                        **{key: state[key] for key in ('nodes', 'source', 'session_id', 'conversion')
                           if key in state}}
            record = {**item, 'metadata': metadata, 'context': state.get('manual_context', {})}
            delta = changes.observe(from_record(record), **at_sample(record, item['data_row']))
            if delta:
                yield delta
        elif kind == 'parameter_event':
            yield event_record(item['event'], event_changes)
        elif kind == 'marker':
            value = item.get('value', {})
            yield {'type': 'roi_operation', **{key: value.get(key) for key in
                                             ('action', 'utc', 'elapsed_s', 'bounds')}}
