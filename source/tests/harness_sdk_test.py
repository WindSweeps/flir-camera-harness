"""Official MCP Python SDK interoperability (test dependency only, not shipped)."""
import argparse
import hashlib
import importlib.metadata
import json
import site
import time
from pathlib import Path


def run(executable, output):
    import anyio
    import jsonschema
    from mcp import Client
    from mcp.client.stdio import StdioServerParameters
    cases=[]
    output.mkdir(parents=True,exist_ok=True)
    async def check(mode):
        params=StdioServerParameters(command=str(executable),args=['--mcp','--replay','--output-dir',str(output/mode)],env={'QT_QPA_PLATFORM':'windows'})
        async with Client(params,mode=mode) as client:
            assert client.protocol_version=='2025-11-25'
            assert client.server_info.name=='flir-camera-harness'
            listed=await client.list_tools()
            assert len(listed.tools)==31
            for tool in listed.tools:
                jsonschema.Draft202012Validator.check_schema(tool.input_schema)
            async def tool(name,**args):
                result=await client.call_tool('flir_'+name,args)
                assert not result.is_error,result
                return result.structured_content
            state=await tool('status')
            assert state['source']=='replay_simulation' and not state['opened']
            camera=(await tool('list_cameras'))['cameras'][0]['camera_id']
            await tool('open_camera',camera_id=camera)
            await tool('start_capture')
            deadline=time.monotonic()+5
            while not (await tool('status'))['frame_shape']:
                assert time.monotonic()<deadline
                await anyio.sleep(.05)
            await tool('set_roi',x=0,y=0,width=40,height=40)
            await anyio.sleep(.15)
            preview=await client.call_tool('flir_preview',{'max_width':240})
            assert not preview.is_error and any(block.type=='image' for block in preview.content)
            error=await client.call_tool('flir_set_trigger',{'mode':'external'})
            assert error.is_error and error.structured_content['code']=='UNSUPPORTED'
            task=await tool('export_curve',path='sdk_curve.csv',allow_saturated=True)
            deadline=time.monotonic()+5
            while True:
                job=await tool('job_status',job_id=task['job_id'])
                if job['done']: break
                assert time.monotonic()<deadline
                await anyio.sleep(.05)
            assert job['status']=='succeeded',job
            assert Path(job['result']['csv']).exists()
            await tool('stop_capture')
            await tool('close_camera')
            cases.append({'mode':mode,'protocol':client.protocol_version,'tools':len(listed.tools),
                          'checks':['automatic legacy negotiation','schema validation','structured results','image content','tool errors','acquisition and export']})
    async def main():
        await check('auto')
        await check('legacy')
    anyio.run(main)
    report={'status':'passed','hardware_access':False,'mcp_sdk':importlib.metadata.version('mcp'),
            'exe_sha256':hashlib.sha256(executable.read_bytes()).hexdigest(),'cases':cases}
    (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--exe',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--dependencies',type=Path)
    args=parser.parse_args()
    if args.dependencies: site.addsitedir(str(args.dependencies.resolve()))
    run(args.exe.resolve(),args.output.resolve())
