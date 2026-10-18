import os,time,json,threading,datetime,urllib.request,urllib.parse,math
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from concurrent.futures import ThreadPoolExecutor
BASE="https://external-api.kalshi.com/trade-api/v2/"
S={"status":"STARTING","error":"","locked":"ANALYZING","strength":"UNVERIFIED","history":[],"performance":{"graded":0,"wins":0,"losses":0,"skips":0,"pending":0,"accuracy":None,"last":[]}}
L=threading.RLock()
def fetch(url):
 req=urllib.request.Request(url,headers={"User-Agent":"SignalLab/21","Accept":"application/json"})
 with urllib.request.urlopen(req,timeout=8) as r:return json.load(r)
def stamp(x):
 try:return int(datetime.datetime.fromisoformat(x.replace("Z","+00:00")).timestamp())
 except:return 0
def btc(name):
 if name=="kraken":return float(next(iter(fetch("https://api.kraken.com/0/public/Ticker?pair=XBTUSD")["result"].values()))["c"][0])
 if name=="coinbase":return float(fetch("https://api.coinbase.com/v2/prices/BTC-USD/spot")["data"]["amount"])
 return float(fetch("https://www.bitstamp.net/api/v2/ticker/btcusd/")["last"])
def connect():
 import pg8000.dbapi as pg
 u=urllib.parse.urlparse(os.environ["DATABASE_URL"])
 return pg.connect(host=u.hostname,port=u.port or 5432,user=urllib.parse.unquote(u.username),password=urllib.parse.unquote(u.password or ""),database=u.path.lstrip("/"),ssl_context=True,timeout=8)
def setup():
 c=connect();q=c.cursor()
 q.execute("CREATE TABLE IF NOT EXISTS signal_lab_github_locks(ticker TEXT PRIMARY KEY, close_ts BIGINT, locked TEXT, strength TEXT, locked_at BIGINT)")
 q.execute("CREATE TABLE IF NOT EXISTS signal_lab_eval_v1(ticker TEXT PRIMARY KEY, close_ts BIGINT NOT NULL, direction TEXT NOT NULL, strength TEXT, locked_at BIGINT NOT NULL, reference_price DOUBLE PRECISION, market_yes_ask INTEGER, market_no_ask INTEGER, result TEXT, graded_at BIGINT)")
 c.commit();c.close()
def track(ticker,close,direction,strength,now,price,yes_ask,no_ask):
 try:
  c=connect();q=c.cursor()
  q.execute("INSERT INTO signal_lab_eval_v1(ticker,close_ts,direction,strength,locked_at,reference_price,market_yes_ask,market_no_ask) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(ticker) DO NOTHING",(ticker,close,direction,strength,now,price,yes_ask,no_ask))
  c.commit();c.close()
 except Exception as e:print("TRACK",repr(e),flush=True)
def verify():
 try:
  c=connect();q=c.cursor();now=int(time.time())
  q.execute("SELECT ticker FROM signal_lab_eval_v1 WHERE result IS NULL AND close_ts<%s ORDER BY close_ts LIMIT 15",(now-30,))
  pending=[x[0] for x in q.fetchall()]
  for ticker in pending:
   try:
    m=fetch(BASE+"markets/"+urllib.parse.quote(ticker,safe="")).get("market",{})
    result=str(m.get("result") or "").lower()
    if result not in ("yes","no"):continue
    q.execute("UPDATE signal_lab_eval_v1 SET result=%s,graded_at=%s WHERE ticker=%s AND result IS NULL",(result,now,ticker))
    c.commit()
   except Exception as e:print("GRADE",ticker,repr(e),flush=True)
  q.execute("SELECT direction,result FROM signal_lab_eval_v1")
  rows=q.fetchall()
  graded=[(d,r) for d,r in rows if r in ("yes","no") and d in ("UP","DOWN")]
  wins=sum((d=="UP" and r=="yes") or (d=="DOWN" and r=="no") for d,r in graded)
  skips=sum(d=="WAIT" for d,r in rows)
  q.execute("SELECT ticker,direction,result FROM signal_lab_eval_v1 ORDER BY close_ts DESC LIMIT 15")
  recent=[{"ticker":t,"pick":d,"result":r or "PENDING"} for t,d,r in q.fetchall()]
  perf={"graded":len(graded),"wins":wins,"losses":len(graded)-wins,"skips":skips,"pending":sum(r is None for d,r in rows),"accuracy":round(100*wins/len(graded),1) if graded else None,"last":recent}
  with L:S["performance"]=perf
  c.close()
 except Exception as e:print("VERIFY",repr(e),flush=True)
def saved(ticker):
 try:
  c=connect();q=c.cursor();q.execute("SELECT locked,strength,locked_at FROM signal_lab_github_locks WHERE ticker=%s",(ticker,));v=q.fetchone();c.close();return v
 except Exception as e:print("DB READ",repr(e),flush=True);return None
def lock(ticker,close,direction,strength,now):
 try:
  c=connect();q=c.cursor();q.execute("INSERT INTO signal_lab_github_locks VALUES (%s,%s,%s,%s,%s) ON CONFLICT (ticker) DO NOTHING",(ticker,close,direction,strength,now));c.commit();c.close()
 except Exception as e:print("DB WRITE",repr(e),flush=True)
def cycle():
 db_ready=False
 last_verify=0
 last_ticker=None
 record_cache=None
 try:
  setup()
  db_ready=True
 except Exception as e:print("DB SETUP",repr(e),flush=True)
 while True:
  try:
   now=int(time.time())
   ms=fetch(BASE+"markets?series_ticker=KXBTC15M&status=open&limit=100").get("markets",[])
   ms=[m for m in ms if stamp(m.get("close_time") or m.get("expiration_time") or "")>now]
   ms.sort(key=lambda m:stamp(m.get("close_time") or m.get("expiration_time") or ""))
   if not ms:raise RuntimeError("No open BTC 15-minute market")
   m=ms[0];ticker=m["ticker"];close=stamp(m.get("close_time") or m.get("expiration_time"));left=max(0,close-now)
   target=m.get("floor_strike")
   if target is None:target=m.get("strike_price")
   if target is None:raise RuntimeError("Kalshi target missing")
   target=float(target)
   with ThreadPoolExecutor(max_workers=3) as pool:
    fs={n:pool.submit(btc,n) for n in ("kraken","coinbase","bitstamp")}
    prices={}
    for n,f in fs.items():
     try:prices[n]=f.result(timeout=9)
     except Exception as e:print("SPOT",n,str(e),flush=True)
   if len(prices)<2:raise RuntimeError("Fewer than two exchange feeds")
   spot=prices.get("kraken",next(iter(prices.values())))
   with L:
    history=list(S.get("history",[])) if S.get("ticker")==ticker else []
   if not history or now-history[-1][0]>=1:history=(history+[[now,spot]])[-900:]
   def past(seconds):
    candidates=[p[1] for p in history if p[0]<=now-seconds]
    return candidates[-1] if candidates else None
   p30,p60,p120=past(30),past(60),past(120)
   spread=max(prices.values())-min(prices.values())
   ready=p30 is not None and p60 is not None and len(history)>=15
   score=0.0
   if ready:
    v30=spot-p30;v60=spot-p60
    score=0.65*v30+0.35*v60
    if p120 is not None:score=0.45*v30+0.35*v60+0.20*(spot-p120)
   threshold=max(8.0,spread*1.5,spot*0.00008)
   if ticker!=last_ticker:
    record_cache=saved(ticker) if db_ready else None
    last_ticker=ticker
   record=record_cache
   if record:direction,strength,locked_at=record
   elif left<=720 and ready:
    if abs(score)<threshold:direction,strength="WAIT","NO CLEAR MOMENTUM"
    else:
     direction="UP" if score>0 else "DOWN"
     strength="MOMENTUM LEAN / NOT VERIFIED"
    locked_at=now
    if db_ready:
     lock(ticker,close,direction,strength,now)
     track(ticker,close,direction,strength,now,spot,m.get("yes_ask"),m.get("no_ask"))
    record_cache=(direction,strength,now)
   elif left<=720:direction,strength,locked_at="ANALYZING","COLLECTING 60 SECONDS OF HISTORY",None
   else:direction,strength,locked_at="ANALYZING","WAITING FOR 3-MINUTE LOCK",None
   if db_ready and now-last_verify>=60:
    last_verify=now
    verify()
   conflict=direction in ("UP","DOWN") and (spot>=target)!=(direction=="UP")
   with L:
    h=history
    S.update(status="LIVE",error="",ticker=ticker,btc=spot,target=target,difference=spot-target,remaining=left,locked=direction,strength=strength,locked_at=locked_at,tracking_online=db_ready,conflict=conflict,quotes=prices,history=h,updated=now,up_ask=m.get("yes_ask"),down_ask=m.get("no_ask"))
  except Exception as e:
   print("COLLECTOR",repr(e),flush=True)
   with L:S.update(status="FEED ERROR",error=str(e))
  time.sleep(1)
HTML='''<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#090e18"><title>Signal Lab</title><style>
*{box-sizing:border-box}body{margin:0;background:#090e18;color:#eef5ff;font:16px system-ui,sans-serif}.wrap{max-width:760px;margin:auto;padding:22px 18px 70px}.head{display:flex;justify-content:space-between;align-items:center;margin-bottom:26px}.logo{font-size:25px;font-weight:900}.pill{background:#243750;border-radius:99px;padding:9px 12px;font-size:12px}.card{background:linear-gradient(140deg,#152641,#101c30);border:1px solid #2b4261;border-radius:24px;padding:23px;margin:16px 0}.label{font-size:12px;font-weight:800;letter-spacing:2px;color:#a4b7d0}.signal{font-size:clamp(65px,17vw,110px);font-weight:900;letter-spacing:-5px;margin:10px 0}.up{color:#2ee0aa}.down{color:#ff6886}.warn{color:#ffc26b}.muted{color:#9fb3cd}.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}.price{font-size:clamp(24px,6vw,40px);font-weight:850;letter-spacing:-1px;margin:8px 0}.between{display:flex;align-items:center;justify-content:space-between;gap:8px}.timer{font-size:29px;font-weight:850}.bar{height:9px;border-radius:9px;background:#293d56;overflow:hidden;margin:20px 0}.fill{height:100%;width:0;background:linear-gradient(90deg,#42a7f7,#2edcad)}.diff{font-size:22px;font-weight:800;margin:24px 0}.notice{padding:14px;border:1px solid #91673e;border-radius:13px;background:#403020;margin-top:17px}svg{display:block;width:100%;height:170px}details{background:#121f34;border:1px solid #2b4261;border-radius:16px;padding:20px;margin:14px 0}summary{font-weight:750;cursor:pointer}.small{font-size:13px;line-height:1.7}
</style></head><body><main class="wrap"><header class="head"><span class="logo">ϟ SIGNAL LAB</span><span class="pill" id="status">CONNECTING</span></header><section class="card"><div class="between"><span class="label">LOCKED AT 3 MINUTES</span><span class="pill" id="phase">ANALYZING</span></div><div id="signal" class="signal warn">WAIT</div><div id="strength" class="muted">Waiting for exchange data</div><div id="notice" class="notice" hidden></div></section><section class="card"><div class="between"><span class="label">CURRENT ROUND</span><span class="timer" id="timer">--:--</span></div><div class="bar"><div class="fill" id="progress"></div></div><div class="grid"><div><div class="label">KALSHI TARGET</div><div class="price" id="target">—</div><div class="muted small">Fixed at round start</div></div><div><div class="label">NOW <span id="arrow"></span></div><div class="price" id="now">—</div><div class="muted small">Updates ~1 second</div></div></div><div class="diff" id="diff">—</div><div class="label">BTC LIVE MOVEMENT</div><svg viewBox="0 0 600 170" preserveAspectRatio="none"><path id="reference" fill="none" stroke="#8c9fb7" stroke-dasharray="6 6"/><path id="chart" fill="none" stroke="#32ddb0" stroke-width="3" stroke-linejoin="round"/></svg><div class="between muted small"><span id="up">UP buy —</span><span id="down">DOWN buy —</span></div></section><section class="card"><div class="label">VERIFIED PERFORMANCE · OFFICIAL SETTLEMENT</div><div class="grid" style="margin-top:15px"><div><div class="label">GRADED SIGNALS</div><div class="price" id="graded">0</div></div><div><div class="label">ACCURACY</div><div class="price" id="accuracy">—</div></div></div><p class="muted small" id="scorecard">No verified results yet</p><div id="recent" class="muted small"></div><p class="muted small">Accuracy excludes WAIT rounds and pending settlements. No results are invented. Experimental model; historical performance does not guarantee future outcomes.</p></section><details><summary>Exchange checks</summary><p class="muted small" id="quotes">Waiting for data</p></details><details><summary>Diagnostics</summary><p class="muted small" id="ticker"></p><p class="muted small">Experimental forecast, not a calibrated probability. Kalshi's settlement index may differ from exchange spot prices.</p></details></main><script>
const e=id=>document.getElementById(id),money=n=>'$'+Number(n).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});let last=null;
function chart(a,t){if(!a||a.length<2)return;let v=a.map(p=>p[1]),low=Math.min(t,...v),high=Math.max(t,...v),pad=Math.max((high-low)*.15,2);low-=pad;high+=pad;let y=z=>160-(z-low)/(high-low)*150,x=i=>i*600/(v.length-1);e('chart').setAttribute('d',v.map((z,i)=>(i?'L':'M')+x(i).toFixed(1)+' '+y(z).toFixed(1)).join(' '));e('reference').setAttribute('d','M0 '+y(t)+' L600 '+y(t))}
async function refresh(){try{let r=await fetch('/api?'+Date.now(),{cache:'no-store'}),d=await r.json();e('status').textContent=d.status;if(d.btc==null||d.target==null)return;e('target').textContent=money(d.target);e('now').textContent=money(d.btc);let dif=d.btc-d.target;e('diff').textContent=(dif>=0?'▲ +$':'▼ -$')+Math.abs(dif).toFixed(2)+(dif>=0?' above target':' below target');e('diff').className='diff '+(dif>=0?'up':'down');if(last!==null){let z=d.btc-last;e('arrow').textContent=z>0?'▲':z<0?'▼':'•';e('arrow').className=z>0?'up':z<0?'down':'muted'}last=d.btc;let s=d.remaining;e('timer').textContent=Math.floor(s/60)+':'+String(s%60).padStart(2,'0');e('progress').style.width=(100-s/9)+'%';e('signal').textContent=d.locked;e('signal').className='signal '+(d.locked==='UP'?'up':d.locked==='DOWN'?'down':'warn');e('strength').textContent=d.strength;e('phase').textContent=d.locked==='ANALYZING'?'ANALYZING':'LOCKED';e('up').textContent='UP buy '+(d.up_ask??'—')+'¢';e('down').textContent='DOWN buy '+(d.down_ask??'—')+'¢';e('quotes').textContent=Object.entries(d.quotes||{}).map(([k,v])=>k+': '+money(v)).join(' · ');e('ticker').textContent=d.ticker;let p=d.performance||{};e('graded').textContent=p.graded??0;e('accuracy').textContent=p.accuracy==null?'—':p.accuracy+'%';e('scorecard').textContent=(d.tracking_online?'':'⚠ Database tracking offline · ')+(p.wins??0)+' wins · '+(p.losses??0)+' losses · '+(p.skips??0)+' skips · '+(p.pending??0)+' pending';e('recent').textContent=(p.last||[]).slice(0,5).map(x=>x.pick+' → '+x.result).join(' | ');e('notice').hidden=!d.conflict;e('notice').textContent=d.conflict?'⚠ Price has crossed to the opposite side of the locked signal. Reliability reduced.':'';chart(d.history,d.target)}catch(err){e('status').textContent='RECONNECTING'}}
refresh();setInterval(refresh,1000)
</script></body></html>'''
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  if self.path.startswith("/api"):
   with L:data=json.dumps(S).encode()
   typ="application/json"
  elif self.path.split("?")[0]=="/":data=HTML.encode();typ="text/html; charset=utf-8"
  else:self.send_error(404);return
  self.send_response(200);self.send_header("Content-Type",typ);self.send_header("Cache-Control","no-store");self.send_header("Content-Length",str(len(data)));self.end_headers();self.wfile.write(data)
 def log_message(self,*args):pass
threading.Thread(target=cycle,daemon=True).start()
ThreadingHTTPServer(("0.0.0.0",int(os.getenv("PORT","10000"))),Handler).serve_forever()
