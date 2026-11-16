"""Black-box MCP client: tests the actual bundled Windows executable via pipes.

Never enumerates/opens hardware. Uses bundled image replay or an explicit replay
fixture; an EOF test checks that a live recording ends with a durable end marker.
"""
import argparse
import base64
import csv
import hashlib
import io
import json
import os
import queue
import subprocess
import struct
import threading
import time
import zlib
from pathlib import Path


class Client:
    def __init__(self, exe, output, replay='bundled'):
        args = [str(exe), '--mcp', '--replay', '--output-dir', str(output)]
        if replay != 'bundled': args.insert(3, str(replay))
        env = dict(os.environ, QT_QPA_PLATFORM='windows')
        self.process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=env, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.responses, self.logs, self.seq = queue.Queue(), [], 0
        def read():
            for line in self.process.stdout:
                try: self.responses.put(json.loads(line.decode('utf-8')))
                except Exception: self.responses.put({'invalid_stdout':repr(line)})
        def errors():
            for line in self.process.stderr: self.logs.append(line.decode('utf-8',errors='replace'))
        threading.Thread(target=read,daemon=True).start()
        threading.Thread(target=errors,daemon=True).start()
    def send(self, obj):
        self.process.stdin.write((json.dumps(obj, ensure_ascii=True)+'\n').encode())
        self.process.stdin.flush()
    def rpc(self, method, params=None):
        self.seq += 1
        self.send({'jsonrpc':'2.0','id':self.seq,'method':method,'params':params or {}})
        try: response = self.responses.get(timeout=20)
        except queue.Empty: raise AssertionError('MCP timeout; exit={} stderr={}'.format(self.process.poll(),''.join(self.logs)))
        assert response.get('id') == self.seq, response
        return response
    def init(self, version='2025-11-25'):
        r = self.rpc('initialize', {'protocolVersion':version,'capabilities':{},'clientInfo':{'name':'FLIR-wire-test','version':'1'}})
        assert r['result']['protocolVersion'] == version, r
        self.send({'jsonrpc':'2.0','method':'notifications/initialized'})
        return r
    def tool(self, tool_name, expected=None, **args):
        r = self.rpc('tools/call',{'name':'flir_'+tool_name,'arguments':args})
        assert 'result' in r, r
        result = r['result']
        data = json.loads(result['content'][0]['text'])
        if expected:
            assert result['isError'] and data['code'] == expected, data
        else:
            assert not result.get('isError'), data
        return data, result
    def data(self, tool_name, **args): return self.tool(tool_name, **args)[0]
    def poll(self, fn, predicate, timeout=12):
        deadline = time.monotonic()+timeout
        while time.monotonic()<deadline:
            value = fn()
            if predicate(value): return value
            time.sleep(.07)
        raise AssertionError('Polling timeout: '+repr(value))
    def job(self, result, expected=None):
        job = self.poll(lambda:self.data('job_status',job_id=result['job_id']),lambda j:j['done'])
        if expected: assert job['status']=='failed' and job['error']['code']==expected,job
        else: assert job['status']=='succeeded',job
        return job
    def close(self):
        if self.process.poll() is None:
            self.process.stdin.close()
            try: self.process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.process.kill()
                raise AssertionError('Server failed to drain on EOF: '+''.join(self.logs))
        assert self.process.returncode == 0, ''.join(self.logs)


def run(exe, output):
    output.mkdir(parents=True,exist_ok=True)
    cases=[]
    c=Client(exe,output)
    try:
        assert c.rpc('server/discover')['error']['code']==-32601
        c.init()
        tools=c.rpc('tools/list')['result']['tools']
        assert len(tools)>=30
        status=c.data('status')
        assert status['source']=='replay_simulation' and not status['opened'] and not status['monitor']['enabled']
        c.tool('set_gain',expected='NO_CAMERA',gain_db=2)
        cameras=c.data('list_cameras')['cameras']
        assert len(cameras)>=1
        c.data('open_camera',camera_id=cameras[0]['camera_id'])
        assert not c.data('status')['streaming']
        c.data('start_capture')
        state=c.poll(lambda:c.data('status'),lambda s:s['frame_shape'] is not None)
        cases.append('initialize/list/open without capture/start replay; no hardware access')
        h,w=state['frame_shape'][:2]
        c.data('set_roi',x=0,y=0,width=min(160,w),height=min(120,h))
        c.poll(lambda:c.data('get_curve'),lambda d:d['available_points']>1)
        c.tool('set_roi',expected='OUT_OF_RANGE',x=w,y=0,width=2,height=2)
        preview,response=c.tool('preview',max_width=360)
        images=[x for x in response['content'] if x['type']=='image']
        assert len(images)==1 and base64.b64decode(images[0]['data']).startswith(b'\xff\xd8')
        assert preview['preview_width']<=360 and preview['image_width']==w
        c.data('set_exposure',exposure_us=8000)
        c.data('set_gain',gain_db=3)
        assert c.data('parameters')['snapshot']['Gain_db']==3
        c.data('set_auto_brightness',enabled=True)
        c.data('set_gain',gain_db=4)
        assert not c.data('parameters')['snapshot']['automatic']
        c.tool('set_trigger',expected='UNSUPPORTED',mode='external')
        c.tool('set_auto_brightness',expected='UNSUPPORTED',enabled=True,gain_upper_db=10)
        cases.append('ROI bounds/full preview/manual and auto parameters/replay limitations')
        ctx=c.job(c.data('context_set',values={'wavelength_nm':532,'nd_transmission_fraction':.1,'magnification':2}))['result']
        assert ctx['calibration_status']=='uncalibrated' and not ctx['dark_correction_applied']
        c.tool('context_set',expected='INVALID_ARGUMENT',values={'nd_transmission_fraction':2})
        path=output/'curve.csv'
        job=c.data('export_curve',path=str(path),allow_saturated=True)
        c.data('set_roi',x=10,y=10,width=40,height=30)
        exported=c.job(job)['result']
        assert exported['sample_count']>0 and digest(path)==exported['csv_sha256']
        with path.open(encoding='utf-8-sig',newline='') as f:
            rows=list(csv.reader(f))
        assert len(rows)==exported['sample_count']+1 and int(rows[1][4])==min(160,w)
        txt=path.with_suffix('.txt').read_text(encoding='utf-8-sig')
        assert '"sum_dn"' not in txt and '"samples"' not in txt and digest(path) in txt
        c.poll(lambda:c.data('get_curve'),lambda d:d['available_points']>1)
        c.job(c.data('export_curve',path=str(path),allow_saturated=True),expected='FILE_EXISTS')
        cases.append('immutable paired export/CSV hash/compact TXT/no implicit overwrite')
        record=c.data('record_start')['record_id']
        c.tool('record_start',expected='ALREADY_RECORDING')
        c.poll(lambda:c.data('record_status',record_id=record),lambda d:d['submitted']>=3)
        c.data('set_exposure',exposure_us=9000)
        c.data('set_roi',x=20,y=20,width=20,height=20)
        c.data('clear_curve')
        c.poll(lambda:c.data('record_status',record_id=record),lambda d:d['submitted']>=6)
        c.tool('record_export',expected='RECORDING_ACTIVE',record_id=record,path='active.csv')
        c.data('record_stop',record_id=record)
        stopped=c.poll(lambda:c.data('record_status',record_id=record),lambda d:d['export_ready'])
        result=c.job(c.data('record_export',record_id=record,path='long_record.csv',allow_saturated=True))['result']
        assert result['sample_count']==stopped['written'] and result['sample_count']>=6
        assert not Path(stopped['cache_path']).exists() and result['cache_removed']
        record_txt=Path(result['txt']).read_text(encoding='utf-8-sig')
        assert '"sum_dn"' not in record_txt and '9000' in record_txt
        cases.append('continuous disk recording/ROI and parameter changes/async drain/export/cache removal')
        c.data('stop_capture')
        c.data('photo_configure',enabled=True,name='实验_甲',directory='photos')
        assert c.data('photo_status')['name']=='实验_甲'
        c.tool('photo_configure',expected='INVALID_ARGUMENT',enabled=True,name='bad/name',directory='photos')
        c.data('clear_peak')
        c.data('show_window',visible=True)
        c.data('show_window',visible=False)
        cases.append('photo configuration/name validation and existing GUI show/hide')
        c.data('start_capture')
        c.poll(lambda:c.data('status'),lambda s:s['frame_shape'] is not None)
        c.data('set_roi',x=0,y=0,width=w,height=h)
        c.poll(lambda:c.data('get_curve'),lambda d:d['available_points']>=2)
        if c.data('status')['saturation_history']['detected']:
            c.tool('export_curve',expected='SATURATION_WARNING',path='blocked_overexposure.csv')
            assert not (output/'blocked_overexposure.csv').exists()
            cases.append('sampled overexposure blocks save pending explicit agreement')
        # EOF must stop/drain a live record, preserving the recovery journal.
        record=c.data('record_start')['record_id']
        c.poll(lambda:c.data('record_status',record_id=record),lambda d:d['submitted']>=3)
        status=c.data('record_status',record_id=record)
        cache=Path(status['cache_path'])
        c.close()
        items=[json.loads(line) for line in cache.read_text(encoding='utf-8').splitlines()]
        assert items[-1]['type']=='end' and items[-1]['sample_count']>=3
        assert items[-1]['sample_count']==sum(i['type']=='sample' for i in items)
        cases.append('client EOF stops acquisition and durably drains active recording')
    finally:
        if c.process.poll() is None: c.close()
        (output/'server_stderr.txt').write_text(''.join(c.logs),encoding='utf-8')
    c=Client(exe,output)
    try:
        c.init('2024-11-05')
        _, raw=c.tool('status')
        assert 'structuredContent' not in raw
        job=c.job(c.data('record_recover',cache_path=str(cache)))
        rid=job['result']['record_id']
        result=c.job(c.data('record_export',record_id=rid,path='recovered.csv',allow_saturated=True))['result']
        assert cache.exists() and not result['cache_removed']
        assert result['sample_count']==items[-1]['sample_count']
        cases.append('2024 MCP text results and cross-process crash-cache recovery')
    finally: c.close()
    # A generated test target has one exact clipped pixel; no user image is edited.
    fixture=output/'saturation_fixture.png'
    width,height=128,96
    raw=bytearray()
    for y in range(height):
        row=bytearray([80]*width)
        if y==10: row[10]=255
        raw.extend(b'\0'+row)
    def chunk(kind,data): return struct.pack('!I',len(data))+kind+data+struct.pack('!I',zlib.crc32(kind+data)&0xffffffff)
    fixture.write_bytes(b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('!IIBBBBB',width,height,8,0,0,0,0))+chunk(b'IDAT',zlib.compress(raw))+chunk(b'IEND',b''))
    clipping=output/'clipping'
    c=Client(exe,clipping,replay=fixture)
    try:
        c.init()
        camera=c.data('list_cameras')['cameras'][0]['camera_id']
        c.data('open_camera',camera_id=camera)
        c.data('start_capture')
        c.poll(lambda:c.data('status'),lambda s:s['frame_shape'] is not None)
        c.data('set_roi',x=0,y=0,width=40,height=40)
        c.poll(lambda:c.data('get_curve'),lambda s:s['available_points']>=2)
        state=c.data('status')
        assert state['saturation_history']['detected']
        c.tool('export_curve',expected='SATURATION_WARNING',path='refused.csv')
        assert not (clipping/'refused.csv').exists()
        c.job(c.data('export_curve',path='allowed.csv',allow_saturated=True))
        rid=c.data('record_start')['record_id']
        c.poll(lambda:c.data('record_status',record_id=rid),lambda s:s['submitted']>=3)
        c.data('set_roi',x=70,y=60,width=20,height=20)
        assert not c.data('status')['saturation_history']['detected']
        assert c.data('record_status',record_id=rid)['saturation_history']['detected']
        c.data('record_stop',record_id=rid)
        record=c.poll(lambda:c.data('record_status',record_id=rid),lambda s:s['export_ready'])
        c.tool('record_export',expected='SATURATION_WARNING',record_id=rid,path='refused_record.csv')
        torn=clipping/'interrupted'/'session.jsonl'
        torn.parent.mkdir(parents=True,exist_ok=True)
        lines=Path(record['cache_path']).read_bytes().splitlines(keepends=True)
        torn.write_bytes(b''.join(lines[:-1])+b'{"type":"en')
        recovered=c.job(c.data('record_recover',cache_path=str(torn)))['result']
        assert not recovered['complete']
        r=c.data('record_export',record_id=recovered['record_id'],path='incomplete.csv',allow_saturated=True)
        c.job(r,expected='INCOMPLETE_RECORDING')
        assert torn.exists() and not (clipping/'incomplete.csv').exists()
        c.job(c.data('record_export',record_id=recovered['record_id'],path='recovered_incomplete.csv',allow_saturated=True,allow_incomplete=True))
        assert torn.exists()
        cases.append('actual MCP saturated-pixel warning persists across ROI resets in full records; incomplete-cache approval enforced')
        # Only these owned tools execute; a client notification is not a command.
        c.send({'jsonrpc':'2.0','method':'tools/call','params':{'name':'flir_stop_capture'}})
        assert c.data('status')['streaming']
        c.process.stdin.write(b'not-json\n')
        c.process.stdin.flush()
        assert c.responses.get(timeout=3)['error']['code']==-32700
        assert c.data('status')['streaming']
        started=time.monotonic()
        for _ in range(30): c.data('status')
        assert time.monotonic()-started<3
        cases.append('malformed input recovery/notifications never mutate/bounded status polling')
    finally: c.close()
    report={'status':'passed','hardware_access':False,'exe_sha256':digest(exe),'tool_count':len(tools),'cases':cases}
    (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))


def digest(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--exe',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    run(args.exe.resolve(),args.output.resolve())
