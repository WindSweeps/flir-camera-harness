"""Bounded stdio transport; the original application runs on Qt's main thread."""
import argparse
import json
import marshal
import os
import queue
import sys
import threading
import traceback
import types
from pathlib import Path

from PyQt5 import QtCore, QtWidgets
from harness_core import Protocol
from harness_camera import CameraHarness

MAX_LINE = 256 * 1024


def write_config():
    """Generate local client snippets only; never change a client's global config."""
    parser = argparse.ArgumentParser(description='Generate portable FLIR MCP client configurations')
    parser.add_argument('--write-mcp-config', action='store_true')
    parser.add_argument('--plugin-dir', type=Path, default=Path(sys.executable).parent.parent)
    args = parser.parse_args()
    root = args.plugin_dir.expanduser().resolve()
    executable = root / 'runtime' / 'FLIR_ROI_Camera.exe'
    if not executable.is_file():
        raise ValueError('Missing runtime/FLIR_ROI_Camera.exe in plugin directory')
    (root / 'configs').mkdir(exist_ok=True)
    common = ['--mcp', '--output-dir', str(root / 'data')]
    for name, replay in (('hardware', False), ('replay', True)):
        config = {'mcpServers': {'flir-camera': {'command': str(executable),
                    'args': common + (['--replay'] if replay else [])}}}
        text = json.dumps(config, ensure_ascii=False, indent=2) + '\n'
        (root / 'configs' / (name + '.mcp.json')).write_text(text, encoding='utf-8')
        if not replay:
            (root / '.mcp.json').write_text(text, encoding='utf-8')
    print('Generated .mcp.json, configs/hardware.mcp.json and configs/replay.mcp.json in ' + str(root))
    return 0


def run(main_code):
    parser = argparse.ArgumentParser(description='FLIR camera MCP stdio server')
    parser.add_argument('--mcp', action='store_true')
    parser.add_argument('--replay', nargs='?', const='bundled')
    parser.add_argument('--show-window', action='store_true')
    parser.add_argument('--output-dir', type=Path, default=Path.home() / 'Documents' / 'FLIR_Camera_Harness')
    args = parser.parse_args()
    protocol_out, protocol_in = sys.stdout, sys.stdin
    if protocol_in is None or protocol_out is None:
        raise RuntimeError('MCP requires stdin/stdout pipes from an AI client.')
    # Only protocol JSON goes to stdout. Qt/SDK/Python diagnostics go to stderr.
    sys.stdout = sys.stderr
    original = types.ModuleType('_flir_harness_original')
    original.__file__ = str(Path(sys.executable).parent / 'main.py')
    sys.modules[original.__name__] = original
    exec(marshal.loads(main_code), original.__dict__)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    app.setQuitOnLastWindowClosed(False)
    if args.replay:
        if args.replay == 'bundled':
            folder = Path(sys._MEIPASS) / 'sample'
            files = [folder / 'camera_frame.png', folder / 'camera_frame_2.png']
            files = [p for p in files if p.is_file()]
        else:
            files = [Path(args.replay).expanduser().resolve()]
        if not files or not all(p.is_file() for p in files):
            raise ValueError('Replay image does not exist')
        provider = lambda: [(str(p), '图片回放 · ' + p.name) for p in files]
        factory = lambda serial: original.ImageReplayBackend(Path(serial))
    else:
        provider = original.FlirCameraBackend.list_cameras
        factory = original.FlirCameraBackend
    # No SDK enumeration or acquisition until a client explicitly requests it.
    window = original.RoiCameraWindow(backend_factory=factory, camera_provider=lambda: [])
    app.processEvents()  # Existing Workflow is attached by a single-shot timer.
    window.camera_provider = provider
    window.setWindowTitle('FLIR 相机 · MCP Harness 1.0 / Camera 1.9')
    adapter = CameraHarness(window, provider, args.output_dir, replay=bool(args.replay))
    if args.show_window:
        window.show()
    protocol = Protocol(adapter)
    incoming, outgoing = queue.Queue(16), queue.Queue(32)
    eof, pipe_failed = threading.Event(), threading.Event()
    errors = []
    def log(message):
        if sys.stderr:
            sys.stderr.write(str(message) + '\n')
            sys.stderr.flush()
    def parse_error(message):
        return {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': message}}
    def reject_constant(value):
        raise ValueError('Non-finite JSON number: ' + value)
    def reader():
        stream = getattr(protocol_in, 'buffer', protocol_in)
        try:
            while not pipe_failed.is_set():
                raw = stream.readline(MAX_LINE + 1)
                if not raw:
                    break
                if len(raw) > MAX_LINE:
                    while raw and not raw.endswith(b'\n' if isinstance(raw, bytes) else '\n'):
                        raw = stream.readline(MAX_LINE + 1)
                    item = ('response', parse_error('Request exceeds 256 KiB'))
                else:
                    try:
                        if isinstance(raw, bytes):
                            raw = raw.decode('utf-8')
                        item = ('request', json.loads(raw, parse_constant=reject_constant))
                    except (ValueError, UnicodeError, RecursionError) as exc:
                        item = ('response', parse_error(str(exc)[:256]))
                while not pipe_failed.is_set():
                    try:
                        incoming.put(item, timeout=.1)
                        break
                    except queue.Full:
                        pass
        except Exception as exc:
            log('MCP stdin: ' + str(exc))
        finally:
            eof.set()
    def writer():
        try:
            while True:
                message = outgoing.get()
                if message is None:
                    return
                # ASCII JSON is also valid UTF-8 even under a Chinese Windows codepage.
                protocol_out.write(json.dumps(message, ensure_ascii=True, allow_nan=False, separators=(',', ':')) + '\n')
                protocol_out.flush()
        except Exception as exc:
            log('MCP stdout disconnected: ' + str(exc))
            pipe_failed.set()
    def exception_hook(kind, value, tb):
        message = ''.join(traceback.format_exception(kind, value, tb))
        adapter.events.append({'kind': 'qt_error', 'message': message[-4096:]})
        log(message)
    sys.excepthook = exception_hook
    read_thread = threading.Thread(target=reader, name='FLIR-MCP-reader', daemon=True)
    write_thread = threading.Thread(target=writer, name='FLIR-MCP-writer', daemon=True)
    write_thread.start()
    read_thread.start()
    shutdown_reported = [False]
    def tick():
        try:
            adapter.poll_jobs()
            if eof.is_set() or pipe_failed.is_set() or adapter.closing:
                if not shutdown_reported[0]:
                    shutdown_reported[0] = True
                    log('MCP shutdown requested; stopping acquisition and draining writes.')
                    adapter.begin_shutdown()
                if adapter.shutdown_ready():
                    try:
                        adapter.finish_shutdown()
                    finally:
                        timer.stop()
                        app.quit()
                return
            try:
                kind, item = incoming.get_nowait()
            except queue.Empty:
                return
            result = item if kind == 'response' else protocol.handle(item)
            if result is not None:
                try:
                    outgoing.put_nowait(result)
                except queue.Full:
                    log('MCP client is not consuming responses; draining and closing.')
                    pipe_failed.set()
        except Exception as exc:
            errors.append(str(exc))
            log(traceback.format_exc())
            pipe_failed.set()
    timer = QtCore.QTimer()
    timer.setInterval(10)
    timer.timeout.connect(tick)
    timer.start()
    log('FLIR MCP ready; mode=' + ('replay' if args.replay else 'hardware') + '; no acquisition started.')
    exit_code = app.exec_()
    try:
        outgoing.put_nowait(None)
    except queue.Full:
        pass
    write_thread.join(timeout=1)
    return 1 if errors else exit_code
