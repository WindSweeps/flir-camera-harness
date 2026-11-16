"""Native Qt adapter checks with synthetic SDK nodes, never physical devices."""


def run_harness_tests(namespace):
    import json
    import marshal
    import sys
    import time
    import types
    import urllib.request
    import socket
    from pathlib import Path
    import numpy as np
    from PyQt5 import QtWidgets
    from camera_modes_test import MockBackend, snapshot
    from harness_camera import CameraHarness
    from harness_core import Protocol, ToolError
    import camera_modes
    import capture_metadata
    output = Path.cwd() / '_harness_test_output'
    output.mkdir(exist_ok=True)
    original = types.ModuleType('_harness_test_original')
    original.__file__ = str(Path(sys.executable).parent / 'main.py')
    exec(marshal.loads(namespace['_original_main_code']), original.__dict__)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    app.setQuitOnLastWindowClosed(False)
    window = original.RoiCameraWindow(backend_factory=MockBackend, camera_provider=lambda: [('MOCK','Mock SDK nodes')])
    app.processEvents()
    adapter = CameraHarness(window, lambda: [('MOCK','Mock SDK nodes')], output)
    cases=[]
    def pump(seconds=.05):
        end=time.monotonic()+seconds
        while time.monotonic()<end:
            app.processEvents()
            adapter.poll_jobs()
            time.sleep(.005)
    def call(tool_name, **args): return adapter.call(tool_name,args)
    def job(result):
        end=time.monotonic()+10
        while time.monotonic()<end:
            pump()
            result2=call('job_status',job_id=result['job_id'])
            if result2['done']: return result2
        raise AssertionError('Job timed out')
    try:
        call('open_camera',camera_id='MOCK')
        backend=window.backend
        assert not backend.streaming
        call('photo_configure',enabled=True,name='MCP_触发',directory='photos')
        readback=call('set_trigger',mode='external',delay_us=9)['readback']
        assert readback['TriggerSource']=='Line0' and readback['TriggerActivation']=='RisingEdge'
        assert readback['StreamBufferHandlingMode']=='OldestFirst'
        assert readback['LineMode']=='Input' and readback['LineInverter'] is False
        assert readback['TriggerOverlap']=='Off'
        call('start_capture')
        assert backend.streaming and window.acquisition_timer.isActive()
        call('set_auto_brightness',enabled=True,gain_upper_db=14)
        assert camera_modes.read(backend,'ExposureAuto')==camera_modes.read(backend,'GainAuto')=='Continuous'
        assert camera_modes.read(backend,'AutoExposureGainUpperLimit')==14
        assert not window.gain_spin.isEnabled() and not window.exposure_spin.isEnabled()
        call('set_gain',gain_db=3)
        assert camera_modes.read(backend,'ExposureAuto')==camera_modes.read(backend,'GainAuto')=='Off'
        assert backend.streaming
        assert window.gain_spin.isEnabled() and window.exposure_spin.isEnabled()
        cases.append('real adapter: external Line0 rising/input/FIFO and auto exposure+gain readback')
        call('set_trigger',mode='continuous')
        assert camera_modes.read(backend,'TriggerMode')=='Off' and backend.streaming
        backend.camera.nodes['TriggerActivation'].value='FallingEdge'
        backend.camera.nodes['TriggerActivation'].fail='RisingEdge'
        before=snapshot(backend)
        try: call('set_trigger',mode='external')
        except camera_modes.ConfigurationError: pass
        else: raise AssertionError('Failed SDK write must surface')
        assert snapshot(backend)==before and backend.streaming
        assert window._workflow.mode.currentData()=='continuous'
        backend.camera.nodes['TriggerActivation'].fail=None
        assert not any(n in ('LineSource','UserOutputValue','V3_3Enable') for n,v in backend.camera.trace)
        cases.append('SDK failure rolls trigger settings back; no electrical output writes')
        # Parameter polling must not repeatedly scan SDK nodes.
        counter=[0]
        old=capture_metadata.read_parameter_snapshot
        def counted(b):
            counter[0]+=1
            return old(b)
        capture_metadata.read_parameter_snapshot=counted
        try:
            call('parameters',refresh=True)
            count=counter[0]
            for _ in range(100): call('parameters',refresh=True)
            assert counter[0]==count
        finally: capture_metadata.read_parameter_snapshot=old
        cases.append('100 parameter polls use the 2-second cache')
        call('stop_capture')
        # Feed a known display image. Mock node backend never grabs any frame.
        adapter.replay=True
        panel=window.roi_intensity_panel
        pixels=np.full((96,128),80,dtype=np.uint8)
        pixels[10,10]=255
        window.current_frame=pixels
        panel.consume_frame(pixels,time.perf_counter())
        call('set_roi',x=0,y=0,width=40,height=40)
        # For synthetic device metadata, explicitly mark identity uint8 conversion.
        panel._saturation_metadata={'nodes':{'PixelFormat':{'status':'ok','value':'Mono8'}},
                                    'conversion':{'input_dtype':'uint8','scaling':{'method':'none'},'selected_channel':None}}
        panel._saturation_manual_key=None
        import saturation_ui
        saturation_ui.manual_check(panel)
        panel.consume_frame(pixels,time.perf_counter()+.1)
        assert panel._saturation_history['detected'], panel._saturation_state
        try: call('export_curve',path='blocked.csv')
        except ToolError as exc: assert exc.code=='SATURATION_WARNING'
        else: raise AssertionError('Saturated save should require agreement')
        assert not (output/'blocked.csv').exists()
        saved=job(call('export_curve',path='allowed.csv',overwrite=True,allow_saturated=True))
        assert saved['status']=='succeeded',saved
        assert Path(saved['result']['txt']).exists()
        cases.append('known saturated pixel blocks export, explicit allow_saturated saves compact pair')
        # Loopback only: no LAN exposure, no firewall modifications in this test.
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0))
            port=sock.getsockname()[1]
        result=call('monitor_start',host='127.0.0.1',port=port)
        assert '#key=' in result['access_link']
        service=window._workflow.monitor.service
        request=urllib.request.Request(service.url+'api/state?client=harnesstest01',headers={'Authorization':'Bearer '+service.token})
        with urllib.request.urlopen(request,timeout=3) as response:
            state=json.load(response)
            assert isinstance(state,dict)
        assert call('monitor_start',host='127.0.0.1',port=port)['access_link']==result['access_link']
        assert 'access_link' not in call('status')['monitor']
        call('monitor_stop')
        assert not call('status')['monitor']['enabled']
        cases.append('read-only monitor tool: local token authentication/start idempotency/stop')
        window.show()
        window.close()
        assert adapter.closing and not window.isVisible()
        cases.append('closing the local window stops and drains the MCP-owned session')
        report={'status':'passed','hardware_access':False,'cases':cases}
        (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(report,ensure_ascii=False,indent=2))
    finally:
        adapter.begin_shutdown()
        end=time.monotonic()+10
        while not adapter.shutdown_ready() and time.monotonic()<end: pump()
        assert adapter.shutdown_ready(), 'Native writes failed to drain'
        adapter.finish_shutdown()
        window.close()
        pump()
