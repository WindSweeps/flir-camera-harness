"""Offline mobile monitor assets; no CDN, third-party requests or write actions."""
HTML = '''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>FLIR 手机监控</title><link rel="icon" href="data:,"><link rel="stylesheet" href="/app.css"><script src="/app.js" defer></script></head>
<body><header class="bar"><h1>FLIR · 手机监控</h1><span class="tag">只读</span><button id="pause" type="button">暂停刷新</button></header>
<main><div id="connection" class="notice" role="status" aria-live="polite">正在连接电脑…</div>
<form id="login" hidden><label for="key">访问密钥</label><input id="key" type="password" autocomplete="off" maxlength="128" required>
<button type="submit">连接监控</button><p>请用手机扫描电脑“手机监控”窗口中的二维码，或粘贴该窗口的访问密钥。</p></form>
<div id="dashboard" class="dashboard" hidden>
<section class="camera"><div class="heading"><h2 id="cameraName">相机图像</h2><span id="mode">—</span></div>
<div class="picture"><canvas id="preview" width="960" height="720" aria-label="完整相机预览，绿框为统计选区"></canvas><p id="empty">等待相机图像</p></div>
<div class="image-note"><span id="frameTime">尚未收到图像</span><span>完整画面 · 最高 2 帧/秒</span></div>
<div id="saturation" class="notice">过曝检测：等待统计选区</div>
<p id="roi">尚未框选统计区域</p>
</section>
<section class="data" aria-label="光强和采集状态"><div class="readings"><div><h2>当前选区光强</h2><p class="value" id="intensity">— <span>DN</span></p></div><div><h2>240 s 最高</h2><p class="value" id="peak">—</p></div></div>
<div class="heading"><h2>最近 60 s 曲线</h2><span class="muted">预览抽样</span></div><canvas id="curve" aria-label="最近60秒的选区光强时间曲线"></canvas>
<p id="curveEmpty" class="muted">先在电脑上框选统计区域</p>
<dl><div><dt>曝光时间</dt><dd id="exposure">—</dd></div><div><dt>增益</dt><dd id="gain">—</dd></div><div><dt>自动控制</dt><dd id="automatic">—</dd></div><div><dt>Binning</dt><dd id="binning">—</dd></div><div><dt>像素格式</dt><dd id="format">—</dd></div></dl>
<div class="status-row"><h2>曲线记录</h2><p id="recording">未开始</p></div><div class="status-row"><h2>触发照片</h2><p id="photos">未开始</p></div>
<p class="muted footnote">手机仅查看状态；相机参数、记录和保存操作请在电脑上完成。DN 尚未标定为物理光强。</p></section></div>
</main></body></html>'''

CSS = '''
:root{color-scheme:light;--text:#183047;--muted:#526879;--blue:#1768a6;--line:#d5dde6;--small:12px;--body:18px;--value:24px}
*{box-sizing:border-box}body{margin:0;background:#f3f5f8;color:var(--text);font:var(--body)/1.5 system-ui,"Microsoft YaHei",sans-serif}
button,input{font:inherit}button{min-height:44px;padding:8px 14px;border:1px solid #aabac8;border-radius:6px;background:white;color:var(--text);cursor:pointer}
button:hover{background:#edf4fa}button:focus-visible,input:focus-visible{outline:3px solid var(--blue);outline-offset:2px}button:disabled{opacity:.55;cursor:default}
h1,h2,p{margin:0}h1,h2{font-size:var(--body);font-weight:600}h2{line-height:1.5}.bar{display:flex;align-items:center;gap:10px;max-width:1440px;margin:auto;padding:12px 16px}.bar button{margin-left:auto}.tag{font-size:var(--small);border:1px solid var(--line);border-radius:4px;padding:2px 6px;white-space:nowrap}
main{max-width:1440px;margin:0 auto;padding:0 16px 24px}.notice{padding:10px 12px;border:1px solid var(--line);border-radius:6px;background:white;overflow-wrap:anywhere}.notice.good{background:#f0fbf4;border-color:#98bda4;color:#245d38}.notice.warn{background:#fff6e7;border-color:#d6ad68;color:#784700}.notice.bad{background:#fff0ef;border-color:#d59591;color:#922c25}
#connection{margin-bottom:12px}.dashboard{display:grid;grid-template-columns:minmax(0,1fr) 360px;gap:20px}.camera,.data{min-width:0}.heading{display:flex;align-items:baseline;justify-content:space-between;gap:12px;padding:4px 0 10px;flex-wrap:wrap}.heading span{color:var(--muted)}
.picture{position:relative;background:#080b0e;aspect-ratio:4/3;max-height:72vh;width:100%;display:flex;align-items:center;justify-content:center;border-radius:6px;overflow:hidden}.picture canvas{width:100%;height:100%;object-fit:contain}.picture p{position:absolute;color:#dbe5ed;background:#080b0e;padding:16px;text-align:center}.image-note{display:flex;justify-content:space-between;flex-wrap:wrap;gap:4px 16px;color:var(--muted);font-size:var(--small);padding:8px 0 12px}#roi{margin-top:8px;overflow-wrap:anywhere}
.data{background:white;border:1px solid var(--line);border-radius:8px;padding:16px}.readings{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:20px}.readings h2{font-size:var(--body);font-weight:400}.value{font-size:var(--value);font-weight:600;color:#174f7d;overflow-wrap:anywhere;font-variant-numeric:tabular-nums}.value span{font-size:var(--body);font-weight:400}#curve{display:block;width:100%;height:180px}#curveEmpty{margin-top:8px}dl{margin:20px 0}dl div{display:flex;justify-content:space-between;gap:12px;padding:8px 0;border-bottom:1px solid #edf0f3}dt{color:var(--muted)}dd{margin:0;text-align:right;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}.status-row{padding:12px 0;border-top:1px solid var(--line)}.status-row h2{font-weight:400;color:var(--muted)}.status-row p{overflow-wrap:anywhere}.muted{color:var(--muted)}.footnote{font-size:var(--small);margin-top:16px}#login{max-width:560px;margin:24px auto;padding:20px;background:white;border:1px solid var(--line);border-radius:8px}#login label,#login input{display:block;width:100%}#login input{min-height:48px;border:1px solid #aabac8;border-radius:6px;padding:8px;margin:8px 0 16px}#login button{background:var(--blue);color:white}#login p{margin-top:16px;color:var(--muted)}[hidden]{display:none!important}
@media(max-width:1000px){.dashboard{grid-template-columns:1fr}.data{padding:16px}.picture{max-height:64vh}.readings{margin-bottom:16px}dl{display:grid;grid-template-columns:1fr 1fr;column-gap:24px}}
@media(max-width:480px){.bar,main{padding-left:12px;padding-right:12px}.bar{flex-wrap:wrap}.bar h1{flex:1}.bar button{margin-left:0}.picture{max-height:62vh}.data{padding:12px}dl{display:block}.heading{gap:4px}.readings{grid-template-columns:minmax(0,1fr) minmax(0,1fr)}.tag{display:none}}
'''

JS = r'''
'use strict';
const $ = id => document.getElementById(id);
const storageKey='flir-monitor-key';
let key=new URLSearchParams(location.hash.slice(1)).get('key') || '';
try{if(key){sessionStorage.setItem(storageKey,key);}else{key=sessionStorage.getItem(storageKey)||'';}}catch(e){}
history.replaceState(null,'',location.pathname);
const client=Array.from(crypto.getRandomValues(new Uint32Array(3)),v=>v.toString(36)).join('');
let timer=null,controller=null,paused=false,version=-1,last=null,lastImage=null,failures=0;
const fmt = n => typeof n==='number' && Number.isFinite(n) ? n.toLocaleString('zh-CN',{maximumFractionDigits:3}) : '—';
function notice(text,kind=''){ $('connection').textContent=text;$('connection').className='notice '+kind; }
function authorize(message){key='';try{sessionStorage.removeItem(storageKey);}catch(e){}$('login').hidden=false;notice(message,'warn');$('pause').disabled=true;}
function schedule(delay=500){clearTimeout(timer);if(key&&!paused&&!document.hidden)timer=setTimeout(poll,delay);}
function drawPreview(){const c=$('preview'),ctx=c.getContext('2d');ctx.clearRect(0,0,c.width,c.height);if(!lastImage)return;ctx.drawImage(lastImage,0,0,c.width,c.height);if(last&&last.roi&&last.frame){const r=last.roi,w=last.frame.original_width,h=last.frame.original_height;ctx.strokeStyle='#39eb77';ctx.lineWidth=3;ctx.strokeRect(r[0]*c.width/w,r[1]*c.height/h,(r[2]-r[0])*c.width/w,(r[3]-r[1])*c.height/h);}}
function drawCurve(){const c=$('curve'),rect=c.getBoundingClientRect();if(!rect.width)return;const ratio=window.devicePixelRatio||1;c.width=Math.round(rect.width*ratio);c.height=Math.round(180*ratio);const ctx=c.getContext('2d');ctx.scale(ratio,ratio);const font=parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--small'))||12;const w=rect.width,h=180,l=font*5,r=12,t=font+4,b=font+16;ctx.font=font+'px system-ui';ctx.fillStyle='#526879';const data=last?.curve||[];$('curveEmpty').hidden=data.length>0;ctx.strokeStyle='#d5dde6';ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(l,t);ctx.lineTo(l,h-b);ctx.lineTo(w-r,h-b);ctx.stroke();if(!data.length)return;let lo=Math.min(...data.map(p=>p[1])),hi=Math.max(...data.map(p=>p[1]));if(lo===hi){const pad=Math.max(1,Math.abs(lo)*.005);lo-=pad;hi+=pad;}const end=data[data.length-1][0];ctx.fillText(hi.toPrecision(4),0,t+4);ctx.fillText(lo.toPrecision(4),0,h-b);ctx.fillText('-60 s',l,h-6);ctx.textAlign='right';ctx.fillText('现在',w-r,h-6);ctx.textAlign='left';ctx.strokeStyle='#1768a6';ctx.lineWidth=2;ctx.beginPath();data.forEach((p,i)=>{const x=l+(p[0]-end+60)/60*(w-r-l),y=h-b-(p[1]-lo)/(hi-lo)*(h-b-t);if(i)ctx.lineTo(x,y);else ctx.moveTo(x,y);});ctx.stroke();}
function render(s){last=s;$('dashboard').hidden=false;$('login').hidden=true;$('pause').disabled=false;
 $('cameraName').textContent=s.camera||'相机图像';$('mode').textContent=s.mode||'未连接';
 const stale=s.snapshot_age_s>3;
 notice(stale?'电脑界面暂未更新，以下是最后状态':s.acquiring?'已连接 · 相机采集中':'已连接 · 相机已停止',stale?'warn':'good');
 $('intensity').replaceChildren(document.createTextNode(fmt(s.intensity)+' '),Object.assign(document.createElement('span'),{textContent:'DN'}));$('peak').replaceChildren(document.createTextNode((s.peak||'—').replace(/\s*DN$/,'')+' '),Object.assign(document.createElement('span'),{textContent:'DN'}));
 $('roi').textContent=s.roi?`选区 ${s.roi[2]-s.roi[0]} × ${s.roi[3]-s.roi[1]} px · X ${s.roi[0]} · Y ${s.roi[1]}`:'尚未框选统计区域';
 $('saturation').textContent=s.saturation?.text||'过曝检测：等待统计选区';$('saturation').className='notice '+(s.saturation?.saturated===true?'bad':s.saturation?.ever?'warn':s.saturation?.saturated===false?'good':'');
 const auto=v=>({true:'开启',false:'关闭',Continuous:'连续自动',Off:'关闭',Once:'单次'})[v]??'未知';const p=s.parameters||{};$('exposure').textContent=fmt(p.ExposureTime)+' μs';$('gain').textContent=fmt(p.Gain)+' dB';$('automatic').textContent=`曝光 ${auto(p.ExposureAuto)} / 增益 ${auto(p.GainAuto)}`;$('binning').textContent=`${p.BinningHorizontal??'—'} × ${p.BinningVertical??'—'}`;$('format').textContent=p.PixelFormat||'—';
 $('recording').textContent=s.recording||'未开始';$('photos').textContent=s.photos||'未开始';
 if(s.frame){$('empty').hidden=true;$('frameTime').textContent=`最近图像 ${s.frame.received_local||''} · ${fmt(s.frame.age_s)} 秒前`;if(s.frame.data_url&&s.frame.version!==version){const requested=s.frame.version;const img=new Image();img.onload=()=>{if(last?.frame?.version!==requested)return;lastImage=img;version=requested;$('preview').width=img.naturalWidth;$('preview').height=img.naturalHeight;drawPreview();};img.src=s.frame.data_url;}else drawPreview();}
 else{version=-1;lastImage=null;$('empty').hidden=false;$('frameTime').textContent='尚未收到图像';drawPreview();}
 drawCurve();
}
async function poll(){if(!key||paused||document.hidden)return;controller=new AbortController();const deadline=setTimeout(()=>controller?.abort(),3000);try{const response=await fetch(`/api/state?since=${version}&client=${client}`,{headers:{Authorization:'Bearer '+key},signal:controller.signal,cache:'no-store',credentials:'omit',redirect:'error'});if(response.status===401){authorize('访问链接已失效，请在电脑端重新扫码');return;}if(!response.ok)throw Error('HTTP '+response.status);const s=await response.json();failures=0;render(s);}catch(e){if(!paused&&!document.hidden){failures++;notice('连接中断，正在重试 · 画面和数据停留在最后一次更新','bad');}}finally{clearTimeout(deadline);controller=null;schedule(failures?1500:500);}}
$('login').addEventListener('submit',e=>{e.preventDefault();key=$('key').value.trim();if(!key)return;try{sessionStorage.setItem(storageKey,key);}catch(e){}$('key').value='';failures=0;version=-1;poll();});
$('pause').addEventListener('click',()=>{paused=!paused;$('pause').textContent=paused?'继续刷新':'暂停刷新';clearTimeout(timer);controller?.abort();if(paused)notice('手机刷新已暂停 · 电脑采集和记录继续','warn');else{notice('正在重新连接…');schedule(0);}});
document.addEventListener('visibilitychange',()=>{clearTimeout(timer);if(document.hidden){controller?.abort();}else if(!paused){schedule(0);}});
new ResizeObserver(()=>drawCurve()).observe($('curve'));
if(key)poll();else authorize('请扫描电脑上的二维码，连接局域网监控');
'''

ASSETS = {'/': ('text/html; charset=utf-8', HTML.encode('utf-8')),
          '/app.css': ('text/css; charset=utf-8', CSS.encode('utf-8')),
          '/app.js': ('text/javascript; charset=utf-8', JS.encode('utf-8'))}
