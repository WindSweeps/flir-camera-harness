"""Dependency-free MCP stdio protocol and the camera tool contract.

Implements the initialize-based MCP revisions listed in VERSIONS. It deliberately
does not advertise the stateless 2026 protocol or unsupported MCP capabilities.
"""
import json
import math

VERSION = '1.0.0'
VERSIONS = ('2025-11-25', '2025-06-18', '2025-03-26', '2024-11-05')
INSTRUCTIONS = (
    'FLIR camera harness owns one camera in this process. Start with flir_status '
    'and flir_list_cameras; opening and acquisition are separate actions. Replay '
    'is explicitly labelled simulation. Exposure is us, gain dB, ROI full displayed '
    'image pixels, curve sum display DN (not calibrated power). External trigger '
    'is Line0/RisingEdge; no tool emits electrical triggers. Stop recording then '
    'poll until drained before export. Export tools return jobs: poll flir_job_status '
    'to completion. For SATURATION_WARNING, explain the warning and obtain the '
    'user\'s agreement before retrying allow_saturated=true. Parameter TXT contains '
    'parameters and changes only. Never infer hardware success from requested '
    'values: use readback, saved counts, error fields and paths. Phone monitor is '
    'read-only and enabled only on request. EOF drains pending writes.'
)


def field(kind, description='', **extra):
    return {'type': kind, 'description': description, **extra}


S = lambda desc='', **kw: field('string', desc, **kw)
B = lambda desc='': field('boolean', desc)
N = lambda desc='', **kw: field('number', desc, **kw)
I = lambda desc='', **kw: field('integer', desc, **kw)
PATH = S('Absolute Windows path, or relative to the configured output directory.', minLength=1, maxLength=2048)
RECORD = S('record_id returned by flir_record_start, flir_record_recover or status.', minLength=1, maxLength=100)
EXPORT = {'path': PATH, 'overwrite': B('Explicitly replace an existing CSV/TXT pair; default false.'),
          'allow_saturated': B('Set true only after user agrees to save detected overexposure; default false.')}
CONTEXT = {
    k: field(['number', 'null'], 'User supplied; null means unknown.', exclusiveMinimum=0)
    for k in ('wavelength_nm', 'sensor_pixel_pitch_um_x', 'sensor_pixel_pitch_um_y',
              'magnification', 'effective_output_pixel_size_um_x', 'effective_output_pixel_size_um_y',
              'nd_transmission_fraction')}
CONTEXT['nd_transmission_fraction']['maximum'] = 1
CONTEXT.update({k: field(['string', 'null'], 'Provenance only; no correction is applied.', maxLength=4096)
                for k in ('reference_plane', 'optical_path_description', 'calibration_source',
                          'dark_reference_path', 'flat_reference_path', 'notes')})


def tool(name, description, properties=None, required=(), read=False, destructive=False, idempotent=False):
    return {'name': 'flir_' + name, 'description': description,
            'inputSchema': {'type': 'object', 'properties': properties or {},
                            'required': list(required), 'additionalProperties': False},
            'annotations': {'readOnlyHint': read, 'destructiveHint': destructive,
                            'idempotentHint': read or idempotent, 'openWorldHint': False}}


TOOLS = [
    tool('status', 'Cached camera, ROI, recording, photo, saturation and recent error state. Does not read SDK nodes.', read=True),
    tool('list_cameras', 'Enumerate FLIR cameras, or bundled replay images in replay mode. Does not start acquisition.', read=True),
    tool('open_camera', 'Open one listed camera without starting acquisition; select by camera_id. Stop/close the previous camera first.',
         {'camera_id': S('Exact camera_id from flir_list_cameras.', minLength=1, maxLength=2048)}, ('camera_id',), idempotent=True),
    tool('close_camera', 'Stop acquisition, release the device and clear the display. Stop recording first; photo writes drain.', idempotent=True),
    tool('start_capture', 'Start the opened device in its configured continuous/external mode. External mode waits for Line0 rising edges.', idempotent=True),
    tool('stop_capture', 'Stop acquisition; pending photos drain. A continuous scalar recording remains open until record_stop.', idempotent=True),
    tool('parameters', 'Read global camera settings/capabilities including binning, formats and ROI. Default returns cached state; refresh reads once, never each poll.',
         {'refresh': B('Request a fresh global node read, limited to once per 2 seconds.')}, read=True),
    tool('set_exposure', 'Set manual exposure in microseconds, disabling automatic exposure and gain. Returns actual readback.',
         {'exposure_us': N(minimum=0.001, maximum=1e9)}, ('exposure_us',), idempotent=True),
    tool('set_gain', 'Set manual gain in dB, disabling automatic exposure and gain. Returns actual readback.',
         {'gain_db': N(minimum=-100, maximum=200)}, ('gain_db',), idempotent=True),
    tool('set_auto_brightness', 'Enable/disable device automatic exposure AND gain. Optional gain upper limit must be supported by hardware; replay is simulation.',
         {'enabled': B(), 'gain_upper_db': N(minimum=-100, maximum=200)}, ('enabled',), idempotent=True),
    tool('set_trigger', 'Switch continuous or external FrameStart/Line0/RisingEdge/Input/non-inverted/OverlapOff. Transactional readback and rollback; no pulse generation.',
         {'mode': S(enum=['continuous', 'external']), 'delay_us': N(minimum=0, maximum=1e9)}, ('mode',), idempotent=True),
    tool('set_roi', 'Set software measurement ROI in full image pixels. Clears the rolling curve and its warning history; running continuous records retain all earlier samples/warnings.',
         {k: I(minimum=0 if k in ('x', 'y') else 1, maximum=100000) for k in ('x','y','width','height')},
         ('x','y','width','height'), destructive=True),
    tool('clear_curve', 'Clear only rolling curve/history; continuous disk records are preserved.', destructive=True),
    tool('clear_peak', 'Reset peak-hold measurement only; does not delete recorded data.', destructive=True),
    tool('get_curve', 'Return bounded points from the recent 60-second displayed curve; reports downsampling. Values are uncalibrated display DN sums.',
         {'max_points': I(minimum=1, maximum=2000)}, read=True),
    tool('preview', 'Return one bounded JPEG of the complete latest image as MCP image content, with age and ROI metadata. No additional camera frame grab.',
         {'max_width': I(minimum=120, maximum=1280)}, read=True),
    tool('export_curve', 'Freeze and export current curve to paired CSV and compact parameter TXT in a background job. Refuses detected overexposure by default.', EXPORT, ('path',), destructive=True),
    tool('record_start', 'Begin continuous scalar recording into a bounded queue and disk cache. Requires acquisition and valid ROI. Returns record_id; no 60-second limit.'),
    tool('record_stop', 'Stop accepting samples for record_id and asynchronously drain to cache. Poll record_status until done=true.', {'record_id': RECORD}, ('record_id',), idempotent=True),
    tool('record_status', 'Read counts, drain status, warning history and cache location for a record.', {'record_id': RECORD}, ('record_id',), read=True),
    tool('record_export', 'Export a stopped/drained recording by streaming its cache. Retains cache on failure. Refuses overexposure/incomplete recording unless explicitly allowed.',
         {**EXPORT, 'record_id': RECORD, 'allow_incomplete': B('User accepts missing data after interrupted/failed recording; default false.')}, ('path','record_id'), destructive=True),
    tool('record_recover', 'Inspect an existing stopped session.jsonl cache in a background job, without modifying it. Successful job returns a record_id for export.',
         {'cache_path': PATH}, ('cache_path',)),
    tool('job_status', 'Poll a background job. done=true and status=succeeded are required before reporting saved files. Failed jobs return code/details; caches remain.',
         {'job_id': S(minLength=1, maxLength=100)}, ('job_id',), read=True),
    tool('photo_configure', 'Configure external-trigger TIFF+parameter TXT automatic saving. Stop acquisition and wait for photo drain first. Filename is host time_name_sequence.',
         {'enabled': B(), 'name': S(minLength=1, maxLength=60), 'directory': PATH}, ('enabled','name','directory'), idempotent=True),
    tool('photo_status', 'Read configured photo destination, last batch path, accepted/written/failed counts and pending bytes.', read=True),
    tool('context_get', 'Read experiment/calibration provenance. Physical conversion stays uncalibrated until a future verified calibration implementation.', read=True),
    tool('context_set', 'Merge experiment parameters. Reference files are hashed in a worker, not applied as dark/flat correction. Job completion marks when context takes effect.',
         {'values': field('object', properties=CONTEXT, additionalProperties=False)}, ('values',)),
    tool('monitor_addresses', 'List local IPv4 addresses eligible for the read-only phone monitor.', read=True),
    tool('monitor_start', 'Enable the existing read-only LAN phone monitor on an explicit local IPv4 address. Returns a private token-bearing access link; share only with intended users.',
         {'host': S(minLength=7, maxLength=45), 'port': I(minimum=1024, maximum=65535)}, ('host',), idempotent=True),
    tool('monitor_stop', 'Disable the phone monitor and invalidate its access link.', idempotent=True),
    tool('show_window', 'Show or hide the full existing camera GUI for local inspection.', {'visible': B()}, ('visible',), idempotent=True),
]
BY_NAME = {t['name']: t for t in TOOLS}


class ToolError(Exception):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code, self.details = code, details

    def value(self):
        return {'ok': False, 'code': self.code, 'message': str(self), 'details': self.details}


def validate(value, schema, path='arguments'):
    """Validate the closed JSON-schema subset used by our own, fixed schemas."""
    types = schema.get('type', [])
    types = [types] if isinstance(types, str) else types
    actual = ('null' if value is None else 'boolean' if isinstance(value, bool) else
              'integer' if isinstance(value, int) else 'number' if isinstance(value, float) else
              'string' if isinstance(value, str) else 'object' if isinstance(value, dict) else 'array')
    if actual not in types and not (actual == 'integer' and 'number' in types):
        raise ToolError('INVALID_ARGUMENT', path + ': expected ' + '/'.join(types))
    if 'enum' in schema and value not in schema['enum']:
        raise ToolError('INVALID_ARGUMENT', path + ': unsupported value')
    if actual in ('integer', 'number'):
        if not math.isfinite(value):
            raise ToolError('INVALID_ARGUMENT', path + ': number must be finite')
        for key, test in (('minimum', lambda a,b:a>=b), ('maximum', lambda a,b:a<=b), ('exclusiveMinimum', lambda a,b:a>b)):
            if key in schema and not test(value, schema[key]):
                raise ToolError('INVALID_ARGUMENT', path + ': outside allowed range')
    if actual == 'string':
        if len(value) < schema.get('minLength', 0) or len(value) > schema.get('maxLength', 100000):
            raise ToolError('INVALID_ARGUMENT', path + ': invalid length')
        if '\x00' in value:
            raise ToolError('INVALID_ARGUMENT', path + ': NUL is not allowed')
    if actual == 'object':
        for k in schema.get('required', []):
            if k not in value:
                raise ToolError('INVALID_ARGUMENT', path + ': missing ' + k)
        props = schema.get('properties', {})
        for k, v in value.items():
            if k not in props:
                raise ToolError('INVALID_ARGUMENT', path + ': unknown field ' + k)
            validate(v, props[k], path + '.' + k)


class Protocol:
    def __init__(self, adapter):
        self.adapter, self.version, self.initialized = adapter, None, False

    def handle(self, req):
        rid = req.get('id') if isinstance(req, dict) else None
        def error(code, message):
            valid_id = rid if isinstance(rid, (str,int)) and not isinstance(rid, bool) else None
            return {'jsonrpc': '2.0', 'id': valid_id, 'error': {'code': code, 'message': message}}
        if not isinstance(req, dict) or req.get('jsonrpc') != '2.0' or not isinstance(req.get('method'), str):
            return error(-32600, 'Invalid JSON-RPC request')
        if 'id' in req and (isinstance(rid, bool) or not isinstance(rid, (str, int))):
            return error(-32600, 'Request id must be a string or integer')
        params = req.get('params', {})
        if not isinstance(params, dict):
            return None if 'id' not in req else error(-32602, 'params must be an object')
        method = req['method']
        if 'id' not in req:
            if method == 'notifications/initialized' and self.version:
                self.initialized = True
            return None  # A notification must never cause camera mutations.
        if method == 'initialize':
            if self.version:
                return error(-32600, 'Already initialized')
            if not isinstance(params.get('protocolVersion'), str) or not isinstance(params.get('capabilities'), dict) or not isinstance(params.get('clientInfo'), dict):
                return error(-32602, 'initialize requires protocolVersion, capabilities and clientInfo')
            requested = params['protocolVersion']
            self.version = requested if requested in VERSIONS else VERSIONS[0]
            result = {'protocolVersion': self.version, 'capabilities': {'tools': {'listChanged': False}},
                      'serverInfo': {'name': 'flir-camera-harness', 'version': VERSION}, 'instructions': INSTRUCTIONS}
        elif method == 'ping':
            result = {}
        elif method not in ('tools/list', 'tools/call'):
            return error(-32601, 'Method not found')
        elif not self.initialized:
            return error(-32600, 'Initialize and send notifications/initialized first')
        elif method == 'tools/list':
            if params.get('cursor'):
                return error(-32602, 'Invalid cursor; all tools fit in one page')
            result = {'tools': TOOLS}
        else:
            name = params.get('name')
            if not isinstance(name, str) or name not in BY_NAME:
                return error(-32602, 'Unknown tool')
            try:
                arguments = params.get('arguments', {})
                validate(arguments, BY_NAME[name]['inputSchema'])
                data = self.adapter.call(name[5:], arguments)
                if not isinstance(data, dict):
                    raise RuntimeError('Adapter must return an object')
                contents = data.pop('_content', [])
                data = {'ok': True, **data}
                result = {'isError': False, 'content': [{'type': 'text', 'text': json.dumps(data, ensure_ascii=False, allow_nan=False)}] + contents}
            except ToolError as exc:
                data = exc.value()
                result = {'isError': True, 'content': [{'type': 'text', 'text': json.dumps(data, ensure_ascii=False)}]}
            except Exception as exc:
                data = {'ok': False, 'code': 'OPERATION_FAILED', 'message': str(exc)}
                result = {'isError': True, 'content': [{'type': 'text', 'text': json.dumps(data, ensure_ascii=False)}]}
            if self.version in ('2025-11-25', '2025-06-18'):
                result['structuredContent'] = data
        return {'jsonrpc': '2.0', 'id': rid, 'result': result}
