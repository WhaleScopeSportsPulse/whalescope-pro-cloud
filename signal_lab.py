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
def historical_analysis():
 # Kraken OHLC includes historical candles; the final candle may still be forming.
 raw=fetch("https://api.kraken.com/0/public/OHLC?pair=XBTUSD&interval=5")
 if raw.get("error"):raise RuntimeError(str(raw["error"]))
 key=next(k for k in raw["result"] if k!="last")
 candles=raw["result"][key][:-1]
 if len(candles)<60:raise RuntimeError("Insufficient historical candles")
 close=[float(x[4]) for x in candles[-160:]]
 high=[float(x[2]) for x in candles[-160:]]
 low=[float(x[3]) for x in candles[-160:]]
 def ema(values,n):
  v=values[0];a=2/(n+1)
  for z in values[1:]:v=a*z+(1-a)*v
  return v
 def rsi(values,n=14):
  changes=[values[i]-values[i-1] for i in range(1,len(values))]
  gains=sum(max(x,0) for x in changes[-n:])/n
  losses=sum(max(-x,0) for x in changes[-n:])/n
  return 100 if losses==0 else 100-100/(1+gains/losses)
 e9,e21=ema(close[-60:],9),ema(close[-60:],21)
 r=rsi(close)
 ranges=[max(high[i]-low[i],abs(high[i]-close[i-1]),abs(low[i]-close[i-1])) for i in range(-14,0)]
 atr=sum(ranges)/len(ranges)
 resistance=max(high[-13:-1]);support=min(low[-13:-1])
 last=close[-1]
 trend=1 if e9>e21 else -1
 slope=(close[-1]-close[-4])/max(atr,1)
 votes=trend+(1 if slope>0.3 else -1 if slope< -0.3 else 0)+(1 if r>55 else -1 if r<45 else 0)
 return dict(trend=trend,rsi=round(r,1),ema9=round(e9,2),ema21=round(e21,2),atr=round(atr,2),support=round(support,2),resistance=round(resistance,2),votes=votes,candles=len(close),candle_time=int(candles[-1][0]),last_close=last)
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
 historical=None
 historical_checked=0
 try:
  setup()
  db_ready=True
  print("DB SETUP OK",flush=True)
 except Exception as e:print("DB SETUP",repr(e),flush=True)
 while True:
  try:
   now=int(time.time())
   # Query open markets first; retry unfiltered when Kalshi status indexing lags.
   ms=fetch(BASE+"markets?series_ticker=KXBTC15M&status=open&limit=100").get("markets",[])
   if not ms:
    ms=fetch(BASE+"markets?series_ticker=KXBTC15M&limit=200").get("markets",[])
   # Kalshi may omit close_time in list responses; inspect the event/market detail before discarding.
   if not ms:
    events=fetch(BASE+"events?series_ticker=KXBTC15M&with_nested_markets=true&limit=100").get("events",[])
    ms=[m for ev in events for m in ev.get("markets",[])]
   candidates=[]
   for item in ms:
    if item.get("status") in ("closed","settled","finalized"):continue
    if not item.get("close_time") and not item.get("expiration_time"):
     try:item=fetch(BASE+"markets/"+urllib.parse.quote(item["ticker"],safe="")).get("market",item)
     except Exception as ex:print("MARKET DETAIL",repr(ex),flush=True)
    close_at=stamp(item.get("close_time") or item.get("expiration_time") or "")
    if close_at>now:candidates.append(item)
   ms=candidates
   ms.sort(key=lambda m:stamp(m.get("close_time") or m.get("expiration_time") or ""))
   if not ms:
    print("MARKET DIAGNOSTIC empty candidates; checking API series and event listing",flush=True)
    for endpoint in ("series/KXBTC15M","events?series_ticker=KXBTC15M&limit=3","markets?series_ticker=KXBTC15M&limit=3"):
     try:
      data=fetch(BASE+endpoint)
      print("MARKET DIAGNOSTIC",endpoint,json.dumps(data,default=str)[:1800],flush=True)
     except Exception as ex:print("MARKET DIAGNOSTIC ERROR",endpoint,repr(ex),flush=True)
    raise RuntimeError("No open BTC 15-minute market")
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
   if now-historical_checked>=300 or historical is None:
    historical_checked=now
    try:
     historical=historical_analysis()
     print('HISTORICAL ANALYSIS',historical,flush=True)
    except Exception as ex:
     historical=None
     print('HISTORICAL ERROR',repr(ex),flush=True)
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
   elif left<=420 and ready and historical:
    # Compare live short-term momentum with historical candle trend, RSI, and volatility.
    # No fabricated probability: disagreement is explicitly flagged.
    momentum=1 if score>threshold else -1 if score< -threshold else 0
    trend=historical["votes"]
    target_side=1 if spot>=target else -1
    weighted=trend+momentum+target_side
    direction="UP" if weighted>0 else "DOWN" if weighted<0 else ("UP" if trend>0 else "DOWN")
    agreement=(momentum==target_side and momentum!=0 and (trend>0)==(momentum>0))
    strength=("MULTI-FACTOR AGREEMENT / UNCALIBRATED" if agreement else
              "MIXED SIGNALS / LOW CONFIDENCE")
    locked_at=now
    if db_ready:
     lock(ticker,close,direction,strength,now)
     track(ticker,close,direction,strength,now,spot,m.get("yes_ask"),m.get("no_ask"))
    record_cache=(direction,strength,now)
   elif left>420:direction,strength,locked_at="ANALYZING","STUDYING FIRST 8 MINUTES",None
   else:direction,strength,locked_at="ANALYZING","LOADING HISTORICAL CANDLES / MOMENTUM",None
   if db_ready and now-last_verify>=60:
    last_verify=now
    verify()
   conflict=direction in ("UP","DOWN") and (spot>=target)!=(direction=="UP")
   with L:
    h=history
    S.update(status="LIVE",error="",ticker=ticker,btc=spot,target=target,difference=spot-target,remaining=left,locked=direction,strength=strength,locked_at=locked_at,tracking_online=db_ready,historical=historical,conflict=conflict,quotes=prices,history=h,updated=now,up_ask=m.get("yes_ask"),down_ask=m.get("no_ask"))
  except Exception as e:
   print("COLLECTOR",repr(e),flush=True)
   with L:S.update(status="FEED ERROR",error=str(e))
  time.sleep(1)
HTML='''<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#090e18"><title>Signal Lab</title><style>
*{box-sizing:border-box}body{margin:0;background:#090e18;color:#eef5ff;font:16px system-ui,sans-serif}.wrap{max-width:760px;margin:auto;padding:22px 18px 70px}.head{display:flex;justify-content:space-between;align-items:center;margin-bottom:26px}.logo{font-size:25px;font-weight:900}.pill{background:#243750;border-radius:99px;padding:9px 12px;font-size:12px}.card{background:linear-gradient(140deg,#152641,#101c30);border:1px solid #2b4261;border-radius:24px;padding:23px;margin:16px 0}.label{font-size:12px;font-weight:800;letter-spacing:2px;color:#a4b7d0}.signal{font-size:clamp(65px,17vw,110px);font-weight:900;letter-spacing:-5px;margin:10px 0}.up{color:#2ee0aa}.down{color:#ff6886}.warn{color:#ffc26b}.muted{color:#9fb3cd}.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}.price{font-size:clamp(24px,6vw,40px);font-weight:850;letter-spacing:-1px;margin:8px 0}.between{display:flex;align-items:center;justify-content:space-between;gap:8px}.timer{font-size:29px;font-weight:850}.bar{height:9px;border-radius:9px;background:#293d56;overflow:hidden;margin:20px 0}.fill{height:100%;width:0;background:linear-gradient(90deg,#42a7f7,#2edcad)}.diff{font-size:22px;font-weight:800;margin:24px 0}.notice{padding:14px;border:1px solid #91673e;border-radius:13px;background:#403020;margin-top:17px}svg{display:block;width:100%;height:170px}details{background:#121f34;border:1px solid #2b4261;border-radius:16px;padding:20px;margin:14px 0}summary{font-weight:750;cursor:pointer}.small{font-size:13px;line-height:1.7}
body{background:#0b1020!important;color:#e9eefb!important}.wrap{max-width:1180px!important}.card{background:linear-gradient(145deg,#1b2540,#141c30)!important;border:1px solid #303957!important;box-shadow:0 8px 25px #05081444!important}.logo{color:#b49bff!important}.pill{background:#303253!important;color:#a9efcf!important}.label{color:#b5b1da!important}.fill{background:linear-gradient(90deg,#ad8bff,#6ce8bc)!important}.signal.up,.up{color:#6ce8bc!important}.signal.down,.down{color:#ff7e9f!important}.price{color:#f0edff!important}svg{background:linear-gradient(180deg,#23254655,#151c3000);border-radius:12px}details{background:#161e33!important;border-color:#343857!important}@media(min-width:900px){.wrap{padding-top:32px!important}.head{margin-bottom:36px!important}.card{padding:30px!important}}.wrap{max-width:1200px;display:grid;grid-template-columns:2fr 1fr;gap:16px}.head{grid-column:1/-1}.wrap>.card{margin:0}.wrap>.card:nth-of-type(1){grid-column:2;grid-row:2}.wrap>.card:nth-of-type(2){grid-column:1;grid-row:2}.wrap>.card:nth-of-type(3){grid-column:1/-1}.wrap>details{grid-column:1/-1}@media(max-width:700px){.wrap{display:flex;flex-direction:column}}.wrap{grid-template-columns:210px minmax(0,2fr) minmax(0,1fr)!important;gap:16px!important}.sidebar{grid-column:1;grid-row:1/5;background:#121a2e;border:1px solid #303957;border-radius:20px;padding:24px 15px;display:flex;flex-direction:column;gap:12px}.sidebrand{color:#b49bff;font-size:19px;font-weight:900;padding:8px 10px 26px}.sideitem{font-size:14px;color:#a8b3d2;padding:14px;border-radius:12px}.sideitem.active{background:#302957;color:#d9cbff;font-weight:800}.sidefoot{margin-top:auto;color:#7686a8;font-size:11px;line-height:2;padding:12px}.head{grid-column:2/-1!important;grid-row:1!important;margin:0!important;padding:14px 18px;border:1px solid #303957;border-radius:16px;background:#161e33}.wrap>.card:nth-of-type(1){grid-column:3!important;grid-row:2!important}.wrap>.card:nth-of-type(2){grid-column:2!important;grid-row:2!important}.wrap>.card:nth-of-type(3){grid-column:2/-1!important;grid-row:3!important}.wrap>details{grid-column:2/-1!important}.signal{font-size:clamp(35px,4vw,65px)!important;letter-spacing:-2px!important;overflow-wrap:anywhere}.card{border-radius:18px!important}@media(max-width:900px){.wrap{display:flex!important;flex-direction:column!important;gap:12px!important;padding:14px!important}.sidebar{display:flex;flex-direction:row;align-items:center;overflow-x:auto;padding:8px!important;gap:6px;border-radius:13px}.sidebrand{font-size:14px;padding:8px;white-space:nowrap}.sideitem{font-size:12px;padding:10px;white-space:nowrap}.sidefoot{display:none}.head{padding:12px 16px!important}.logo{font-size:18px!important}.card{padding:18px!important}.wrap>.card:nth-of-type(1){order:1}.wrap>.card:nth-of-type(2){order:2}.wrap>.card:nth-of-type(3){order:3}.wrap>details{order:4}.signal{font-size:clamp(36px,10vw,62px)!important;letter-spacing:-2px!important}.price{font-size:clamp(18px,5vw,32px)!important}.label{font-size:10px!important;letter-spacing:1px!important}.diff{font-size:17px!important;margin:14px 0!important}svg{height:210px!important}}</style></head><body><main class="wrap"><aside class="sidebar"><div class="sidebrand">ϟ SIGNAL LAB</div><div class="sideitem active">◈ &nbsp; Overview</div><div class="sideitem">⌁ &nbsp; Live Market</div><div class="sideitem">◎ &nbsp; Predictions</div><div class="sideitem">▥ &nbsp; Performance</div><div class="sidefoot">BTC / 15 MIN<br>EXPERIMENTAL MODEL</div></aside><header class="head"><span class="logo">ϟ SIGNAL LAB</span><span class="pill" id="status">CONNECTING</span></header><section class="card"><div class="between"><span class="label">FINAL PICK · LOCKED AT MINUTE 8</span><span class="pill" id="phase">ANALYZING</span></div><div id="signal" class="signal warn">WAIT</div><div id="strength" class="muted">Waiting for exchange data</div><div id="notice" class="notice" hidden></div></section><section class="card"><div class="between"><span class="label">CURRENT ROUND</span><span class="timer" id="timer">--:--</span></div><div class="bar"><div class="fill" id="progress"></div></div><div class="grid"><div><div class="label">KALSHI TARGET</div><div class="price" id="target">—</div><div class="muted small">Fixed at round start</div></div><div><div class="label">NOW <span id="arrow"></span></div><div class="price" id="now">—</div><div class="muted small">Updates ~1 second</div></div></div><div class="diff" id="diff">—</div><div class="label">BTC LIVE MOVEMENT</div><svg viewBox="0 0 600 170" preserveAspectRatio="none"><path id="reference" fill="none" stroke="#8c9fb7" stroke-dasharray="6 6"/><path id="chart" fill="none" stroke="#32ddb0" stroke-width="3" stroke-linejoin="round"/></svg><div class="between muted small"><span id="up">UP buy —</span><span id="down">DOWN buy —</span></div></section><section class="card"><div class="label">VERIFIED PERFORMANCE · OFFICIAL SETTLEMENT</div><div class="grid" style="margin-top:15px"><div><div class="label">GRADED SIGNALS</div><div class="price" id="graded">0</div></div><div><div class="label">ACCURACY</div><div class="price" id="accuracy">—</div></div></div><p class="muted small" id="scorecard">No verified results yet</p><div id="recent" class="muted small"></div><p class="muted small">Accuracy excludes WAIT rounds and pending settlements. No results are invented. Experimental model; historical performance does not guarantee future outcomes.</p></section><details><summary>Exchange checks</summary><p class="muted small" id="quotes">Waiting for data</p></details><details><summary>Diagnostics</summary><p class="muted small" id="ticker"></p><p class="muted small" id="indicators"></p><p class="muted small">Experimental forecast, not a calibrated probability. Kalshi's settlement index may differ from exchange spot prices.</p></details></main><script>
const e=id=>document.getElementById(id),money=n=>'$'+Number(n).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});let last=null;
function chart(a,t){if(!a||a.length<2)return;let v=a.map(p=>p[1]),low=Math.min(t,...v),high=Math.max(t,...v),pad=Math.max((high-low)*.15,2);low-=pad;high+=pad;let y=z=>160-(z-low)/(high-low)*150,x=i=>i*600/(v.length-1);e('chart').setAttribute('d',v.map((z,i)=>(i?'L':'M')+x(i).toFixed(1)+' '+y(z).toFixed(1)).join(' '));e('reference').setAttribute('d','M0 '+y(t)+' L600 '+y(t))}
async function refresh(){try{let r=await fetch('/api?'+Date.now(),{cache:'no-store'}),d=await r.json();e('status').textContent=d.status;if(d.btc==null||d.target==null)return;e('target').textContent=money(d.target);e('now').textContent=money(d.btc);let dif=d.btc-d.target;e('diff').textContent=(dif>=0?'▲ +$':'▼ -$')+Math.abs(dif).toFixed(2)+(dif>=0?' above target':' below target');e('diff').className='diff '+(dif>=0?'up':'down');if(last!==null){let z=d.btc-last;e('arrow').textContent=z>0?'▲':z<0?'▼':'•';e('arrow').className=z>0?'up':z<0?'down':'muted'}last=d.btc;let s=d.remaining;e('timer').textContent=Math.floor(s/60)+':'+String(s%60).padStart(2,'0');e('progress').style.width=(100*(900-s)/900)+'%';e('signal').textContent=d.locked;e('signal').className='signal '+(d.locked==='UP'?'up':d.locked==='DOWN'?'down':'warn');e('strength').textContent=d.strength;e('phase').textContent=d.locked==='ANALYZING'?'ANALYZING':'LOCKED';e('up').textContent='UP buy '+(d.up_ask??'—')+'¢';e('down').textContent='DOWN buy '+(d.down_ask??'—')+'¢';e('quotes').textContent=Object.entries(d.quotes||{}).map(([k,v])=>k+': '+money(v)).join(' · ');e('ticker').textContent=d.ticker;let h=d.historical;e('indicators').textContent=h?'5-minute historical candles: '+h.candles+' · RSI '+h.rsi+' · EMA9 '+h.ema9+' · EMA21 '+h.ema21+' · ATR '+h.atr+' · support '+h.support+' · resistance '+h.resistance:'Historical candle feed unavailable';let p=d.performance||{};e('graded').textContent=p.graded??0;e('accuracy').textContent=p.accuracy==null?'—':p.accuracy+'%';e('scorecard').textContent=(d.tracking_online?'':'⚠ Database tracking offline · ')+(p.wins??0)+' wins · '+(p.losses??0)+' losses · '+(p.skips??0)+' skips · '+(p.pending??0)+' pending';e('recent').textContent=(p.last||[]).slice(0,5).map(x=>x.pick+' → '+x.result).join(' | ');e('notice').hidden=!d.conflict;e('notice').textContent=d.conflict?'⚠ Price has crossed to the opposite side of the locked signal. Reliability reduced.':'';chart(d.history,d.target)}catch(err){e('status').textContent='RECONNECTING'}}
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
