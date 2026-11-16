"""Build an additive export extension without altering the supplied main/PYZ.

CArchive format reference: PyInstaller v6.16.0 archive/readers.py and writers.py.
Only the small exec(source, {}) launcher needs Python 3.10 bytecode; the actual
extension is plain auditable source, compiled by the application's own CPython.
"""
import argparse
import hashlib
import json
import struct
import zipfile
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MAGIC = b'MEI\x0c\x0b\x0a\x0b\x0e'
EXPECTED = 'E44F94B88C6AC97E1C18B29C4CA597631E1B6D62E6C4292F077721FB90171580'


def marshal310_launcher(source):
    # CPython 3.10 marshal layout, no reference sharing needed.
    def integer(value):
        return struct.pack('<i', value)

    def obj(value):
        if value is None:
            return b'N'
        if isinstance(value, str):
            data = value.encode('utf-8')
            return b'u' + integer(len(data)) + data
        if isinstance(value, bytes):
            return b's' + integer(len(value)) + value
        if isinstance(value, tuple):
            return b'(' + integer(len(value)) + b''.join(map(obj, value))
        raise TypeError(type(value))

    code = bytes([101, 0, 100, 0, 105, 0, 131, 2, 1, 0, 100, 1, 83, 0])
    return (b'c' + struct.pack('<6i', 0, 0, 0, 0, 3, 0)
            + obj(code) + obj((source, None)) + obj(('exec',))
            + obj(()) + obj(()) + obj(())
            + obj('roi_curve_export.py') + obj('<module>') + integer(1) + obj(b''))


def parse_archive(exe):
    cookie_at = exe.rfind(MAGIC)
    if cookie_at < 0 or cookie_at + 88 != len(exe):
        raise ValueError('Expected an unsigned PyInstaller archive ending at EOF')
    cookie = struct.unpack('!8sIIII64s', exe[cookie_at:])
    _, length, toc_offset, toc_length, pyver, pylib = cookie
    if pyver != 310 or pylib.rstrip(b'\0') != b'python310.dll':
        raise ValueError('Only the supplied Python 3.10 build is supported')
    start = len(exe) - length
    position = start + toc_offset
    end = position + toc_length
    entries = []
    while position < end:
        size, offset, packed, unpacked, compressed, kind = struct.unpack_from('!IIIIBc', exe, position)
        if size < 18 or position + size > end:
            raise ValueError('Invalid archive TOC')
        name = exe[position + 18:position + size].rstrip(b'\0').decode('utf-8')
        data = exe[start + offset:start + offset + packed]
        if len(data) != packed:
            raise ValueError('Truncated archive member')
        entries.append((name, data, unpacked, compressed, kind))
        position += size
    return exe[:start], entries, pyver, pylib


def add_extension(exe, source):
    prefix, entries, pyver, pylib = parse_archive(exe)
    if sum(name == 'main' for name, *_ in entries) != 1:
        raise ValueError('Expected exactly one main script')
    raw = marshal310_launcher(source)
    extension = ('roi_curve_export', zlib.compress(raw, 9), len(raw), 1, b's')
    updated = []
    for entry in entries:
        if entry[0] == 'main':
            updated.append(extension)
        updated.append(entry)
    payload, toc = bytearray(), bytearray()
    for name, data, unpacked, compressed, kind in updated:
        encoded = name.encode('utf-8') + b'\0'
        size = (18 + len(encoded) + 15) // 16 * 16
        toc += struct.pack('!IIIIBc', size, len(payload), len(data), unpacked, compressed, kind)
        toc += encoded + b'\0' * (size - 18 - len(encoded))
        payload += data
    cookie = struct.pack('!8sIIII64s', MAGIC, len(payload) + len(toc) + 88,
                         len(payload), len(toc), pyver, pylib)
    result = prefix + payload + toc + cookie
    # Exact preservation is required for every existing archive entry and bootloader.
    new_prefix, reread, _, _ = parse_archive(result)
    assert new_prefix == prefix
    assert [e for e in reread if e[0] != 'roi_curve_export'] == entries
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--patch', type=Path, default=Path('C:/data/zzr/FLIR_ROI_Camera_Patch_1.1.zip'))
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--test', action='store_true')
    parser.add_argument('--parameter-test', action='store_true')
    parser.add_argument('--metadata-test', action='store_true')
    parser.add_argument('--saturation-test', action='store_true')
    parser.add_argument('--performance-test', action='store_true')
    parser.add_argument('--capture-performance-test', action='store_true')
    parser.add_argument('--recording-test', action='store_true')
    parser.add_argument('--recording-ui-test', action='store_true')
    parser.add_argument('--mode-test', action='store_true')
    parser.add_argument('--workflow-test', action='store_true')
    parser.add_argument('--photo-ui-test', action='store_true')
    parser.add_argument('--monitor-test', action='store_true')
    parser.add_argument('--monitor-demo', action='store_true')
    args = parser.parse_args()
    if hashlib.sha256(args.patch.read_bytes()).hexdigest().upper() != EXPECTED:
        raise ValueError('Source 1.1 patch hash differs from the reviewed package')
    with zipfile.ZipFile(args.patch) as package:
        exe = package.read('FLIR_ROI_Camera_Patch_1.1/payload/FLIR_ROI_Camera.exe')
    source = 'import sys, types\n'
    for module_name in ('saturation', 'photo_storage', 'capture_metadata', 'camera_modes', 'saturation_ui', 'experiment_context', 'compact_parameters', 'parameter_log',
                        'continuous_recording', 'recording_ui', 'photo_ui', 'qrcodegen',
                        'monitor_web', 'monitor_server', 'monitor_ui', 'workflow_ui',
                        'harness_core', 'harness_camera', 'harness_runtime'):
        module_source = (ROOT / ('src/' + module_name + '.py')).read_text(encoding='utf-8')
        source += ('_module = types.ModuleType(' + repr(module_name) + ')\n'
                   'sys.modules[' + repr(module_name) + '] = _module\n'
                   'exec(compile(' + repr(module_source) + ', ' + repr(module_name + '.py') + ', "exec"), _module.__dict__)\n')
    source += '\n' + (ROOT / 'src/roi_curve_export.py').read_text(encoding='utf-8')
    entry = next(e for e in parse_archive(exe)[1] if e[0] == 'main')
    main_code = zlib.decompress(entry[1]) if entry[3] else entry[1]
    source += '\nif "--write-mcp-config" in sys.argv:\n    import harness_runtime\n    raise SystemExit(harness_runtime.write_config())\n'
    source += '\nif "--mcp" in sys.argv:\n    import harness_runtime\n    raise SystemExit(harness_runtime.run(' + repr(main_code) + '))\n'
    test_prelude = '\n_original_main_code = ' + repr(main_code) + '\n'
    test_helper = (ROOT / 'tests/camera_modes_test.py').read_text(encoding='utf-8')
    test_prelude += '\n_mock_module = types.ModuleType("camera_modes_test")\nsys.modules["camera_modes_test"] = _mock_module\n'
    test_prelude += 'exec(compile(' + repr(test_helper) + ', "camera_modes_test.py", "exec"), _mock_module.__dict__)\n'
    # The old self-test requires the image alone to use >75% of window height.
    # Route the official self-test entry to functional checks of the new layout.
    # Test-only mocks are compiled ONLY when explicitly running --self-test.
    smoke = test_prelude + (ROOT / 'tests/workflow_selftest.py').read_text(encoding='utf-8')
    smoke += '\n' + (ROOT / 'tests/photo_ui_selftest.py').read_text(encoding='utf-8')
    smoke += '\n' + (ROOT / 'tests/monitor_selftest.py').read_text(encoding='utf-8')
    smoke += '''
from pathlib import Path
_out = Path.cwd() / '_test_output'
_out.mkdir(exist_ok=True)
(_out / 'verification.txt').write_text('RUNNING: FLIR ROI 1.9 verification.\\n', encoding='utf-8')
try:
    import PySpin
    print('PySpin native runtime loaded successfully.')
    run_workflow_tests(globals())
    run_photo_ui_tests(globals())
    run_monitor_tests(globals())
    (_out / 'verification.txt').write_text('PASS: FLIR ROI 1.9 native Qt replay, controls, ROI, recording, trigger mocks and responsive layouts. Hardware not accessed.\\n', encoding='utf-8')
except BaseException:
    import traceback
    (_out / 'verification.txt').write_text('FAIL: FLIR ROI 1.9 verification.\\n' + traceback.format_exc(), encoding='utf-8')
    traceback.print_exc()
    raise SystemExit(1)
raise SystemExit(0)
'''
    source += '\nif "--self-test" in sys.argv:\n    exec(' + repr(smoke) + ', globals())\n'
    harness_smoke = test_prelude + (ROOT / 'tests/harness_native_selftest.py').read_text(encoding='utf-8')
    harness_smoke += '\ntry:\n    run_harness_tests(globals())\nexcept BaseException:\n    import traceback\n    traceback.print_exc()\n    raise SystemExit(1)\nraise SystemExit(0)\n'
    source += '\nif "--harness-self-test" in sys.argv:\n    exec(' + repr(harness_smoke) + ', globals())\n'
    if args.workflow_test or args.photo_ui_test or args.monitor_test or args.monitor_demo:
        source += test_prelude
    if args.monitor_demo:
        source += '\n' + (ROOT / 'tests/monitor_selftest.py').read_text(encoding='utf-8')
        source += '\nrun_monitor_tests(globals(), demo=True)\nraise SystemExit(0)\n'
    if args.metadata_test:
        test_source = (ROOT / 'tests/metadata_mock_test.py').read_text(encoding='utf-8')
        source += ('\n_test_module = types.ModuleType("metadata_mock_test")\n'
                   'exec(compile(' + repr(test_source) + ', "metadata_mock_test.py", "exec"), _test_module.__dict__)\n'
                   'try:\n    import json\n    print(json.dumps(_test_module.run_metadata_tests(capture_metadata)))\n'
                   'except BaseException:\n    import traceback\n    traceback.print_exc()\n'
                   '    raise SystemExit(1)\nraise SystemExit(0)\n')
    if args.parameter_test:
        source += '\n' + (ROOT / 'tests/parameter_selftest.py').read_text(encoding='utf-8')
        source += ('\ntry:\n    run_parameter_tests(globals())\n'
                   'except BaseException:\n    import traceback\n    traceback.print_exc()\n'
                   '    raise SystemExit(1)\nraise SystemExit(0)\n')
    if args.test:
        source += '\n' + (ROOT / 'tests/export_selftest.py').read_text(encoding='utf-8')
        source += ('\ntry:\n    run_export_tests(globals())\n'
                   'except BaseException:\n    import traceback\n    traceback.print_exc()\n'
                   '    raise SystemExit(1)\nraise SystemExit(0)\n')
    if args.saturation_test:
        source += '\n' + (ROOT / 'tests/performance_selftest.py').read_text(encoding='utf-8')
        source += ('\ntry:\n    run_performance_tests(globals())\n'
                   'except BaseException:\n    import traceback\n    traceback.print_exc()\n'
                   '    raise SystemExit(1)\nraise SystemExit(0)\n')
    for enabled, filename, function in (
        (args.performance_test, 'performance_selftest.py', 'run_performance_tests(globals())'),
        (args.capture_performance_test, 'capture_performance_test.py', 'print(__import__("json").dumps(run_capture_performance_tests()))'),
        (args.recording_test, 'recording_selftest.py', 'run_recording_tests()'),
        (args.recording_ui_test, 'recording_ui_selftest.py', 'run_recording_ui_tests(globals())'),
        (args.mode_test, 'camera_modes_test.py', 'run_camera_mode_tests()'),
        (args.workflow_test, 'workflow_selftest.py', 'run_workflow_tests(globals())'),
        (args.photo_ui_test, 'photo_ui_selftest.py', 'run_photo_ui_tests(globals())'),
        (args.monitor_test, 'monitor_selftest.py', 'run_monitor_tests(globals())'),
    ):
        if enabled:
            test_source = (ROOT / ('tests/' + filename)).read_text(encoding='utf-8')
            source += '\n' + test_source
            source += ('\ntry:\n    ' + function + '\n'
                       'except BaseException:\n    import traceback\n    traceback.print_exc()\n'
                       '    raise SystemExit(1)\nraise SystemExit(0)\n')
    result = add_extension(exe, source)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(result)
    report = {'base_version': '1.1', 'extension_version': '1.9', 'harness_version': '1.0.0', 'python': '3.10',
              'test_build': args.test or args.parameter_test or args.metadata_test or args.saturation_test or args.performance_test or args.capture_performance_test or args.recording_test or args.recording_ui_test or args.mode_test or args.workflow_test or args.photo_ui_test or args.monitor_test or args.monitor_demo, 'original_archive_members_unchanged': True,
              'original_bootloader_unchanged': True,
              'output_sha256': hashlib.sha256(result).hexdigest(),
              'extension_source_sha256': hashlib.sha256(source.encode()).hexdigest()}
    args.output.with_suffix('.build.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
