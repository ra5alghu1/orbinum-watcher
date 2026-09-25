#!/usr/bin/env python3
import html,json,mimetypes,os,sqlite3,time
from datetime import datetime,timezone,timedelta
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

DB=os.getenv('ORBINUM_DB','/var/lib/orbinum-monitor/uptime.db')
HOST=os.getenv('ORBINUM_WEB_HOST','127.0.0.1'); PORT=int(os.getenv('ORBINUM_WEB_PORT','8787'))
VALIDATOR=os.getenv('ORBINUM_VALIDATOR_NAME','robotek8-orbinum')
PUBLIC=os.getenv('ORBINUM_PUBLIC_URL','https://orbinum-watcher.xyz').rstrip('/')
STATIC=Path(os.getenv('ORBINUM_STATIC_DIR','/app/static'))
TG=os.getenv('ORBINUM_TELEGRAM_URL','https://t.me/Ras_a1_Ghu1')
GH=os.getenv('ORBINUM_GITHUB_URL','https://github.com/ra5alghu1/orbinum-watcher')
KZ=timezone(timedelta(hours=5)); POLL=15; CHECK=60; STALE=180

def db(): return sqlite3.connect(f'file:{DB}?mode=ro',uri=True,timeout=5)
def fmt(ts): return datetime.fromtimestamp(ts,KZ).strftime('%d.%m.%Y %H:%M UTC+5') if ts else '—'
def dur(s):
    s=max(0,int(s or 0))
    if s<60:return f'{s}s'
    if s<3600:return f'{s//60}m'
    if s<86400:return f'{s//3600}h {(s%3600)//60}m'
    return f'{s//86400}d {(s%86400)//3600}h'

def latest():
    c=db(); r=c.execute('SELECT ts,ok,status,peers,best,finalized,latency_ms,error FROM samples ORDER BY ts DESC LIMIT 1').fetchone(); c.close(); return r

def first():
    c=db(); r=c.execute('SELECT MIN(ts) FROM samples').fetchone(); c.close(); return r[0] if r and r[0] else None

def stat(sec=None):
    c=db(); rows=c.execute('SELECT ts,ok FROM samples '+('WHERE ts>=? ' if sec else '')+'ORDER BY ts',((int(time.time())-sec,) if sec else ())).fetchall(); c.close()
    if not rows:return {'uptime':0.0,'samples':0,'incidents':0,'progress':0}
    inc=0; prev=True
    for _,ok in rows:
        if not ok and prev:inc+=1
        prev=bool(ok)
    return {'uptime':sum(1 for _,ok in rows if ok)/len(rows)*100,'samples':len(rows),'incidents':inc}

def incident_rows():
    cutoff=int(time.time())-2592000; c=db(); rows=c.execute('SELECT ts,ok FROM samples WHERE ts>=? ORDER BY ts',(cutoff,)).fetchall(); c.close()
    out=[]; start=None; now=int(time.time())
    for ts,ok in rows:
        if not ok and start is None:start=ts
        elif ok and start is not None:out.append({'started':fmt(start),'recovered':fmt(ts),'duration':dur(ts-start),'status':'Recovered','active':False}); start=None
    if start is not None:out.append({'started':fmt(start),'recovered':'Now','duration':dur(now-start),'status':'Open','active':True})
    return out[-10:][::-1]

def timeline():
    now=int(time.time()); start=now-86400; c=db(); rows=c.execute('SELECT ts,ok,status,error FROM samples WHERE ts>=? ORDER BY ts',(start,)).fetchall(); c.close(); b=[[] for _ in range(96)]
    for r in rows:
        i=int((r[0]-start)/900)
        if 0<=i<96:b[i].append(r)
    out=[]
    for x in b:
        if not x:out.append('none')
        elif any(not bool(r[1]) for r in x):out.append('down')
        elif any(str(r[2] or '').lower()=='degraded' for r in x):out.append('degraded')
        else:out.append('up')
    return out

def snapshot():
    now=int(time.time()); r=latest(); f=first(); age=max(0,now-f) if f else 0
    if r:
        ts,ok,status,peers,best,finalized,lat,error=r; sample=max(0,now-ts); state='degraded' if str(status or '').lower()=='degraded' else ('online' if ok else 'offline'); ui='stale' if sample>STALE else ('down' if state=='offline' else state)
    else: ts=peers=best=finalized=lat=None; error='No monitoring data'; sample=None; state='offline'; ui='stale'
    def win(sec):
        s=stat(sec); return {**s,'ready':age>=sec,'progress':min(100,age/sec*100)}
    a=stat()
    return {'validator':VALIDATOR,'state':state,'ui_state':ui,'peers':peers,'best':best,'finalized':finalized,'latency_ms':lat,'sample_age':sample,'last_sample':fmt(ts),'monitoring_since':fmt(f),'monitor_age':age,'poll_seconds':POLL,'collector_seconds':CHECK,'windows':{'24h':win(86400),'7d':win(604800),'30d':win(2592000),'all':{**a,'ready':True,'progress':100}},'timeline':timeline(),'incidents':incident_rows()}

PAGE=r'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Orbinum Watcher — __V__</title><meta name="description" content="External uptime monitoring for an Orbinum validator with live chain health and Telegram incident alerts."><meta property="og:title" content="Orbinum Watcher — __V__"><meta property="og:description" content="Live validator uptime, peers, block progress, latency and incidents."><meta property="og:type" content="website"><meta property="og:url" content="__P__/"><meta property="og:image" content="__P__/og-image.png?v=1"><meta name="twitter:card" content="summary_large_image"><meta name="twitter:image" content="__P__/og-image.png?v=1"><link rel="icon" href="/favicon.ico"><link rel="apple-touch-icon" href="/apple-touch-icon.png"><style>
:root{--bg:#0d0721;--panel:#160d31;--line:#39285f;--txt:#f3efff;--dim:#9b8dca;--faint:#675b96;--mint:#42eeb7;--cyan:#42d9f5;--amber:#f8c74e;--pink:#ff67a1}*{box-sizing:border-box}body{margin:0;color:var(--txt);font-family:Inter,system-ui;background:radial-gradient(circle at 10% 0,#25105b 0,transparent 34%),linear-gradient(135deg,#130827,#080716);min-height:100vh}body:before{content:"";position:fixed;inset:0;pointer-events:none;background-image:linear-gradient(#ffffff08 1px,transparent 1px),linear-gradient(90deg,#ffffff08 1px,transparent 1px);background-size:32px 32px}.wrap{width:min(1200px,calc(100% - 36px));margin:auto;padding:18px 0 44px}.top{display:flex;align-items:center;gap:12px;margin-bottom:52px}.top img{width:46px;height:46px;border-radius:10px}.brand{font-weight:700}.identity{display:flex;align-items:flex-start;gap:20px;width:100%;margin-bottom:28px}.name{font:700 clamp(40px,6vw,70px)/1 ui-monospace,SFMono-Regular,Menlo,monospace;letter-spacing:-.04em;min-width:0}.status{margin-left:auto;white-space:nowrap;border:1px solid #2c9d86;border-radius:999px;padding:11px 16px;background:#10233a;color:#c6fff0;font:600 14px ui-monospace,monospace}.status:before{content:"●";color:var(--mint);margin-right:8px}.status.degraded{border-color:#9a7930}.status.down,.status.stale{border-color:#914255}.labelrow{display:flex;justify-content:space-between;color:var(--dim);font-size:13px;margin-bottom:12px}.legend{display:flex;gap:14px;font:12px ui-monospace,monospace}.legend i{font-style:normal}.ticks{display:grid;grid-template-columns:repeat(96,1fr);gap:3px;padding:12px;border:1px solid var(--line);border-radius:14px;background:#120b2c;overflow:hidden}.tick{height:32px;border-radius:4px;background:#2b1f4f}.tick.up{background:var(--mint)}.tick.degraded{background:var(--amber)}.tick.down{background:var(--pink)}.axis{display:flex;justify-content:space-between;color:var(--faint);font:12px ui-monospace,monospace;margin-top:9px}.section{margin-top:34px}.section h2{font-size:15px;font-weight:500;color:#b3a7de;margin:0 0 16px}.windows,.metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}.card{position:relative;overflow:hidden;min-height:145px;padding:22px;border:1px solid var(--line);border-radius:22px;background:#160e31}.card .k{color:#a99bd2;font-size:13px}.card .v{margin-top:30px;font:600 34px ui-monospace,monospace}.card .s{margin-top:6px;color:#776ba6;font:12px ui-monospace,monospace}.cov{position:absolute;left:0;bottom:0;height:3px;background:linear-gradient(90deg,var(--cyan),var(--mint));width:0;max-width:100%;border-radius:0 0 22px 22px}.metric{min-height:118px}.metric .v{margin-top:20px}.inc{border:1px solid var(--line);border-radius:20px;background:#140c2d;overflow:hidden}.empty{padding:32px;color:#786ca7}.itable{width:100%;border-collapse:collapse}.itable th,.itable td{padding:14px;border-bottom:1px solid #2a1d4c;text-align:left;font-size:13px}.itable th{color:#8f82bc}.tag{font:12px ui-monospace,monospace}.open{color:var(--pink)}.resolved{color:var(--mint)}.foot{display:flex;justify-content:space-between;gap:16px;align-items:center;margin-top:28px;padding-top:22px;border-top:1px solid #2a1d4c;color:#766aa5;font-size:12px}.buttons{display:flex;gap:10px}.btn{color:#dcd4ff;text-decoration:none;border:1px solid var(--line);border-radius:999px;padding:9px 13px;background:#140c2d}.btn:hover{border-color:#5fcdb8}@media(max-width:760px){.identity{flex-wrap:wrap}.status{margin-left:auto}.windows,.metrics{grid-template-columns:repeat(2,1fr)}.ticks{grid-template-columns:repeat(48,1fr)}.tick:nth-child(odd){display:none}.foot{align-items:flex-start;flex-direction:column}}@media(max-width:480px){.windows,.metrics{grid-template-columns:1fr}.name{font-size:36px}}
</style></head><body><main class="wrap"><div class="top"><img src="/static/icon-192.png" alt=""><div class="brand">Orbinum Watcher</div></div><div class="identity"><div class="name" id="validator">__V__</div><div id="status" class="status stale"><span id="stext">Loading</span> <span style="color:#705f9d">/</span> <span id="sage">waiting</span></div></div><div class="labelrow"><span>Last 24 hours, <span id="bucket">15-minute</span> buckets</span><span class="legend"><i style="color:var(--mint)">■ up</i><i style="color:var(--amber)">■ degraded</i><i style="color:var(--pink)">■ down</i><i style="color:#3a285f">■ no data</i></span></div><div id="ticks" class="ticks"></div><div class="axis"><span>24h ago</span><span>now</span></div><section class="section"><h2>Validator uptime</h2><div class="windows"><div class="card"><div class="k">24 hours</div><div id="u24" class="v">—</div><div id="s24" class="s">—</div><i id="c24" class="cov"></i></div><div class="card"><div class="k">7 days</div><div id="u7" class="v">—</div><div id="s7" class="s">—</div><i id="c7" class="cov"></i></div><div class="card"><div class="k">30 days</div><div id="u30" class="v">—</div><div id="s30" class="s">—</div><i id="c30" class="cov"></i></div><div class="card"><div class="k">All time</div><div id="uall" class="v">—</div><div id="sall" class="s">—</div><i class="cov" style="width:100%"></i></div></div></section><section class="section"><h2>Live chain health</h2><div class="metrics"><div class="card metric"><div class="k">Peers</div><div id="peers" class="v">—</div></div><div class="card metric"><div class="k">Best block</div><div id="best" class="v">—</div></div><div class="card metric"><div class="k">Finalized</div><div id="fin" class="v">—</div></div><div class="card metric"><div class="k">Latency</div><div id="lat" class="v">—</div></div></div></section><section class="section"><h2>Incident history</h2><div id="inc" class="inc"><div class="empty">Loading…</div></div></section><footer class="foot"><div>Monitoring since <span id="since">—</span> · external checks every 60s · <span id="last">—</span></div><div class="buttons"><a class="btn" href="__TG__" target="_blank" rel="noopener">Telegram</a><a class="btn" href="__GH__" target="_blank" rel="noopener">GitHub</a></div></footer></main><script>
let D=null,at=0;const $=id=>document.getElementById(id),txt=(id,v)=>$(id).textContent=v;function age(s){s=Math.max(0,Math.floor(s||0));return s<60?s+'s':Math.floor(s/60)+'m'}function status(){if(!D)return;let a=D.sample_age==null?null:D.sample_age+Math.floor((Date.now()-at)/1000),u=a>180?'stale':D.ui_state;let n=u==='down'?'Down':u[0].toUpperCase()+u.slice(1);$('status').className='status '+u;txt('stext',n);txt('sage',a==null?'no sample':(u==='stale'?'last sample '+age(a)+' ago':'checked '+age(a)+' ago'))}function win(k,u,s,c){let w=D.windows[k],p=Math.max(0,Math.min(100,w.progress||0));$(c).style.width=p+'%';if(w.ready){txt(u,Number(w.uptime).toFixed(3)+'%');txt(s,w.samples+' samples · '+w.incidents+' incidents')}else{txt(u,'Collecting');txt(s,Math.floor(D.monitor_age/3600)+'h '+Math.floor((D.monitor_age%3600)/60)+'m observed · '+p.toFixed(1)+'%')}}function ticks(){let el=$('ticks');el.replaceChildren();for(let x of D.timeline){let i=document.createElement('i');i.className='tick '+x;el.appendChild(i)}}function incidents(){let e=$('inc'),a=D.incidents||[];if(!a.length){e.innerHTML='<div class="empty">No incidents since monitoring began.</div>';return}let h='<table class="itable"><thead><tr><th>Started</th><th>Recovered</th><th>Duration</th><th>Status</th></tr></thead><tbody>';for(let x of a)h+=`<tr><td>${x.started}</td><td>${x.recovered}</td><td>${x.duration}</td><td><span class="tag ${x.active?'open':'resolved'}">${x.status}</span></td></tr>`;e.innerHTML=h+'</tbody></table>'}function render(d){D=d;at=Date.now();txt('validator',d.validator);txt('peers',d.peers??'—');txt('best',d.best==null?'—':'#'+d.best);txt('fin',d.finalized==null?'—':'#'+d.finalized);txt('lat',d.latency_ms==null?'—':d.latency_ms+' ms');win('24h','u24','s24','c24');win('7d','u7','s7','c7');win('30d','u30','s30','c30');txt('uall',Number(d.windows.all.uptime).toFixed(3)+'%');txt('sall',d.windows.all.samples+' samples · '+d.windows.all.incidents+' incidents');txt('since',d.monitoring_since);txt('last','last sample '+d.last_sample);ticks();incidents();status()}async function go(){try{let r=await fetch('/api/status',{cache:'no-store'});if(!r.ok)throw Error(r.status);render(await r.json())}catch(e){console.error(e)}}setInterval(status,1000);setInterval(()=>{if(!document.hidden)go()},15000);document.addEventListener('visibilitychange',()=>{if(!document.hidden)go()});window.addEventListener('focus',go);go();
</script></body></html>'''

def page():
    r=PAGE.replace('__V__',html.escape(VALIDATOR)).replace('__P__',html.escape(PUBLIC,quote=True)).replace('__TG__',html.escape(TG,quote=True)).replace('__GH__',html.escape(GH,quote=True)); return r

ALIASES={'/favicon.ico':'favicon.ico','/favicon.svg':'favicon.svg','/apple-touch-icon.png':'apple-touch-icon.png','/og-image.png':'og-image.png'}
class Handler(BaseHTTPRequestHandler):
    def out(self,code,typ,b,cache='no-store',head=False):
        self.send_response(code); self.send_header('Content-Type',typ); self.send_header('Content-Length',str(len(b))); self.send_header('Cache-Control',cache); self.end_headers();
        if not head:self.wfile.write(b)
    def static(self,name,head=False):
        if not name or '/' in name or '\\' in name or '..' in name:return self.send_error(404)
        p=STATIC/name
        if not p.is_file():return self.send_error(404)
        b=p.read_bytes(); self.out(200,mimetypes.guess_type(p.name)[0] or 'application/octet-stream',b,'public,max-age=3600',head)
    def req(self,head=False):
        p=urlparse(self.path).path
        if p=='/health':return self.out(200,'text/plain',b'ok\n',head=head)
        if p=='/api/status':
            try:b=json.dumps(snapshot(),ensure_ascii=False).encode(); return self.out(200,'application/json; charset=utf-8',b,head=head)
            except Exception as e:b=json.dumps({'error':str(e)}).encode(); return self.out(500,'application/json',b,head=head)
        if p in ALIASES:return self.static(ALIASES[p],head)
        if p.startswith('/static/'):return self.static(p[8:],head)
        if p not in ('/','/index.html'):return self.send_error(404)
        return self.out(200,'text/html; charset=utf-8',page().encode(),head=head)
    def do_GET(self):self.req(False)
    def do_HEAD(self):self.req(True)
    def log_message(self,*a):pass

if __name__=='__main__':
    print(f'Orbinum Watcher listening on http://{HOST}:{PORT}',flush=True); ThreadingHTTPServer((HOST,PORT),Handler).serve_forever()
