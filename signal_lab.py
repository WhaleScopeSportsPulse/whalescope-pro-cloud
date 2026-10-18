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
 if name=="coinbase":return float(fetch("https://api.exchange.coinbase.com/products/BTC-USD/ticker")["price"])
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
 volumes=[float(x[6]) for x in candles[-160:]]
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
 # Historical 15-minute pattern study using three completed 5-minute candles.
 patterns=[]
 for i in range(6,len(close)-3):
  prev=close[i-3:i+1]
  scale=max(sum(high[j]-low[j] for j in range(i-2,i+1))/3,1)
  shape=[(prev[j]-prev[0])/scale for j in range(1,4)]
  future=1 if close[i+3]>close[i] else 0
  patterns.append((shape,future))
 current_scale=max(sum(high[-j]-low[-j] for j in (1,2,3))/3,1)
 current_shape=[(close[-4+j]-close[-4])/current_scale for j in range(1,4)]
 # Prevent near-identical overlapping historical windows from dominating the vote.
 ranked=sorted(enumerate(patterns),key=lambda ip:sum((a-b)**2 for a,b in zip(ip[1][0],current_shape)))
 chosen=[]
 for idx,p in ranked:
  if all(abs(idx-j)>=3 for j,_ in chosen):
   chosen.append((idx,p))
  if len(chosen)>=25:break
 neighbors=[p for _,p in chosen]
 pattern_up=sum(p[1] for p in neighbors)/len(neighbors) if neighbors else 0.5
 volume_ratio=volumes[-1]/max(sum(volumes[-21:-1])/20,0.000001)
 return dict(backtest=paper_backtest(candles[-160:]),pattern_up=round(pattern_up,3),pattern_samples=len(neighbors),volume_ratio=round(volume_ratio,2),trend=trend,rsi=round(r,1),ema9=round(e9,2),ema21=round(e21,2),atr=round(atr,2),support=round(support,2),resistance=round(resistance,2),votes=votes,candles=len(close),candle_time=int(candles[-1][0]),last_close=last)
def connect():
 import pg8000.dbapi as pg
 u=urllib.parse.urlparse(os.environ["DATABASE_URL"])
 return pg.connect(host=u.hostname,port=u.port or 5432,user=urllib.parse.unquote(u.username),password=urllib.parse.unquote(u.password or ""),database=u.path.lstrip("/"),ssl_context=True,timeout=8)
def setup():
 c=connect();q=c.cursor()
 q.execute("CREATE TABLE IF NOT EXISTS signal_lab_github_locks(ticker TEXT PRIMARY KEY, close_ts BIGINT, locked TEXT, strength TEXT, locked_at BIGINT)")
 q.execute("CREATE TABLE IF NOT EXISTS signal_lab_eval_v1(ticker TEXT PRIMARY KEY, close_ts BIGINT NOT NULL, direction TEXT NOT NULL, strength TEXT, locked_at BIGINT NOT NULL, reference_price DOUBLE PRECISION, market_yes_ask INTEGER, market_no_ask INTEGER, result TEXT, graded_at BIGINT)")
 q.execute("CREATE TABLE IF NOT EXISTS signal_lab_features_v2(ticker TEXT PRIMARY KEY, forecast DOUBLE PRECISION, distance DOUBLE PRECISION, momentum DOUBLE PRECISION, trend DOUBLE PRECISION, pattern DOUBLE PRECISION, result TEXT)")
 q.execute("CREATE TABLE IF NOT EXISTS signal_lab_baseline_v1(ticker TEXT PRIMARY KEY, leader TEXT NOT NULL, predicted TEXT NOT NULL, projected_move DOUBLE PRECISION, strike_distance DOUBLE PRECISION, recorded_at BIGINT NOT NULL)")
 c.commit();c.close()
def track_features(ticker,forecast,distance,momentum,trend,pattern):
 try:
  c=connect();q=c.cursor()
  q.execute("INSERT INTO signal_lab_features_v2(ticker,forecast,distance,momentum,trend,pattern) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT(ticker) DO NOTHING",(ticker,forecast,distance,momentum,trend,pattern))
  c.commit();c.close()
 except Exception as ex:print("FEATURE TRACK",repr(ex),flush=True)
def record_baseline(ticker,leader,predicted,move,distance,now):
 try:
  c=connect();q=c.cursor()
  q.execute("INSERT INTO signal_lab_baseline_v1(ticker,leader,predicted,projected_move,strike_distance,recorded_at) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT(ticker) DO NOTHING",(ticker,leader,predicted,move,distance,now))
  c.commit();c.close()
 except Exception as ex:print("BASELINE TRACK",repr(ex),flush=True)
def baseline_report():
 try:
  c=connect();q=c.cursor()
  q.execute("SELECT b.leader,b.predicted,e.result FROM signal_lab_baseline_v1 b JOIN signal_lab_eval_v1 e ON b.ticker=e.ticker WHERE e.result IN ('yes','no') AND b.recorded_at >= %s",(1792359837,))
  rows=q.fetchall();c.close()
  n=len(rows)
  if not n:return {"graded":0,"model_accuracy":None,"current_leader_accuracy":None,"different_picks":0}
  model=sum((p=="UP")==(r=="yes") for leader,p,r in rows)
  baseline=sum((leader=="UP")==(r=="yes") for leader,p,r in rows)
  return {"graded":n,"model_accuracy":round(100*model/n,1),"current_leader_accuracy":round(100*baseline/n,1),"different_picks":sum(leader!=p for leader,p,r in rows)}
 except Exception as ex:return {"error":str(ex)[:120]}
def model_audit():
 # Out-of-sample chronological evaluation of recorded live decisions.
 # Never train and evaluate on the same settled round.
 try:
  c=connect();q=c.cursor()
  q.execute("SELECT f.forecast,f.distance,f.momentum,f.trend,f.pattern,e.result FROM signal_lab_features_v2 f JOIN signal_lab_eval_v1 e ON f.ticker=e.ticker WHERE e.result IN ('yes','no') ORDER BY e.close_ts")
  rows=q.fetchall();c.close()
  if len(rows)<40:return {"status":"COLLECTING","samples":len(rows),"minimum":40}
  split=max(20,int(len(rows)*0.7));train,test=rows[:split],rows[split:]
  candidates=[(a,b,c) for a in (0.3,0.55,0.8) for b in (0.1,0.3,0.5) for c in (0,0.35,0.6)]
  def score(row,w):
   _,distance,momentum,trend,pattern,_=row
   return distance+w[0]*momentum+w[1]*trend+w[2]*(pattern-0.5)*2
  def accuracy(data,w):
   return sum((score(x,w)>=0)==(x[5]=="yes") for x in data)/len(data)
  selected=max(candidates,key=lambda w:accuracy(train,w))
  baseline=(0.55,0.3,0.35)
  return {"status":"SHADOW TEST","samples":len(rows),"holdout":len(test),"candidate_weights":selected,"candidate_accuracy":round(100*accuracy(test,selected),1),"baseline_accuracy":round(100*accuracy(test,baseline),1),"note":"Shadow evaluation only; live weights unchanged"}
 except Exception as ex:return {"status":"ERROR","error":str(ex)[:120]}
def adaptive_weights():
 # Promote weights only after chronological holdout improvement.
 baseline=(0.50,0.25,0.25)
 try:
  c=connect();q=c.cursor()
  q.execute("SELECT f.distance,f.momentum,f.trend,f.pattern,e.result FROM signal_lab_features_v2 f JOIN signal_lab_eval_v1 e ON f.ticker=e.ticker WHERE e.result IN ('yes','no') ORDER BY e.close_ts")
  rows=q.fetchall();c.close()
  if len(rows)<90:return baseline
  train=rows[:-30];test=rows[-30:]
  candidates=[(m,t,1-m-t) for m in (0.2,0.35,0.5,0.65) for t in (0.1,0.2,0.3) if m+t<0.9]
  def hit(data,w):
   return sum(((x[0]+w[0]*x[1]+w[1]*x[2]+w[2]*(2*x[3]-1))>=0)==(x[4]=='yes') for x in data)
  winner=max(candidates,key=lambda w:hit(train,w))
  if hit(test,winner)>=hit(test,baseline)+3:return winner
 except Exception as ex:print("ADAPTIVE WEIGHTS",repr(ex),flush=True)
 return baseline

def record_observation(ticker,now,spot,target,direction,market):
 try:
  c=connect();q=c.cursor()
  q.execute("INSERT INTO signal_lab_observations(ts,ticker,up_bid,down_bid,btc,reference,direction,entry_signal,up_ask,down_ask) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (ticker,ts) DO NOTHING",(now,ticker,market.get("yes_bid_dollars"),market.get("no_bid_dollars"),spot,target,direction,"OBSERVE",market.get("yes_ask_dollars"),market.get("no_ask_dollars")))
  c.commit();c.close()
 except Exception as ex:print("OBSERVATION TRACK",repr(ex),flush=True)
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
  q.execute("SELECT direction,result FROM signal_lab_eval_v1 WHERE locked_at >= %s",(1792359837,))
  rows=q.fetchall()
  graded=[(d,r) for d,r in rows if r in ("yes","no") and d in ("UP","DOWN")]
  wins=sum((d=="UP" and r=="yes") or (d=="DOWN" and r=="no") for d,r in graded)
  skips=sum(d=="WAIT" for d,r in rows)
  # Diagnose historical mistakes without modifying the live model or rewriting prior picks.
  q.execute("SELECT direction,result,strength,reference_price FROM signal_lab_eval_v1 WHERE result IN ('yes','no') ORDER BY close_ts DESC LIMIT 100")
  audit=q.fetchall()
  mistakes=[{"direction":d,"actual":r,"reason":("MIXED SIGNALS" if "MIXED" in (st or "") else "REVERSAL RISK" if "REVERSAL" in (st or "") else "OTHER / UNDETERMINED")} for d,r,st,price in audit if (d=="UP")!=(r=="yes")]
  q.execute("SELECT ticker,direction,result FROM signal_lab_eval_v1 WHERE locked_at >= %s ORDER BY close_ts DESC LIMIT 15",(1792359837,))
  recent=[{"ticker":t,"pick":d,"result":r or "PENDING"} for t,d,r in q.fetchall()]
  perf={"graded":len(graded),"wins":wins,"losses":len(graded)-wins,"skips":skips,"pending":sum(r is None for d,r in rows),"accuracy":round(100*wins/len(graded),1) if graded else None,"last":recent,"mistake_audit":{"sample":len(audit),"mistakes":len(mistakes),"recent_reasons":mistakes[:5]}}
  perf["model_audit"]=model_audit()
  perf["leader_comparison"]=baseline_report()
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
def paper_backtest(candles):
 # Walk-forward evaluation: completed 5m candles, 15m outcomes, no future leakage.
 # Proxy only: this is NOT a backtest of minute-8 Kalshi contract prices.
 closes=[float(c[4]) for c in candles]
 wins=total=0
 for i in range(30,len(closes)-3,3):
  past=closes[:i+1]
  momentum=past[-1]-past[-4]
  pick=1 if momentum>=0 else -1
  actual=1 if closes[i+3]>=closes[i] else -1
  wins+=int(pick==actual)
  total+=1
 return {"sample":total,"wins":wins,"accuracy":round(wins*100/total,1) if total else None,"method":"5m-candle momentum proxy; not minute-8 model or Kalshi P&L"}

def cashout_signal(direction,yes_ask,no_ask,conflict,yes_bid=None,no_bid=None):
 # Indicative only: executable bids, fees and order depth are not available.
 if direction not in ("UP","DOWN"):return "WAIT / NO TRADE DATA"
 ask=yes_ask if direction=="UP" else no_ask
 if conflict:return "REASSESS / PRICE CROSSED TARGET"
 if ask is None:return "NO QUOTE / DO NOT ASSUME PROFIT"
 bid=yes_bid if direction=="UP" else no_bid
 if bid is None:return "NO LIVE BID / EXIT CANNOT BE PRICED"
 return "INDICATIVE EXIT BID "+str(bid)+"¢ / COMPARE TO YOUR ENTRY + FEES"

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
   # Search the paginated market list for the currently trading 15-minute round.
   # Kalshi's first page may contain only future contracts, so do not treat it as exhaustive.
   candidates=[]
   cursor=""
   for page in range(12):
    url=BASE+"markets?series_ticker=KXBTC15M&limit=200"
    if cursor:url+="&cursor="+urllib.parse.quote(cursor,safe="")
    response=fetch(url)
    for item in response.get("markets",[]):
     if item.get("status") in ("closed","settled","finalized"):continue
     start=stamp(item.get("open_time") or "")
     end=stamp(item.get("close_time") or item.get("expiration_time") or "")
     if start<=now<end and end-now<=1200:
      candidates.append(item)
    if candidates:break
    cursor=response.get("cursor") or ""
    if not cursor:break
   ms=sorted(candidates,key=lambda m:stamp(m.get("close_time") or m.get("expiration_time") or ""))
   if not ms:raise RuntimeError("Kalshi did not return a currently trading BTC 15-minute market")
   m=ms[0];ticker=m["ticker"];close=stamp(m.get("close_time") or m.get("expiration_time"));left=max(0,close-now)
   target=m.get("floor_strike")
   if target is None:target=m.get("strike_price")
   if target is None:
    # List responses may omit strike fields. Fetch the canonical market object.
    detail=fetch(BASE+"markets/"+urllib.parse.quote(ticker,safe="")).get("market",{})
    m={**m,**{k:v for k,v in detail.items() if v is not None}}
    target=m.get("floor_strike")
    if target is None:target=m.get("strike_price")
   if target is None:
    # Preserve the feed and log actionable diagnostics; never fabricate a strike.
    print("TARGET MISSING",ticker,"fields",list(m.keys()),"rules",str(m.get("rules_primary") or "")[:300],flush=True)
    # An exchange OHLC minute is only a reference estimate, not CF BRTI.
    raw=fetch("https://api.kraken.com/0/public/OHLC?pair=XBTUSD&interval=1")
    key=next(k for k in raw["result"] if k!="last")
    minute=close-900
    bars=[bar for bar in raw["result"][key] if int(bar[0])==minute]
    if not bars:raise RuntimeError("Estimated opening reference unavailable")
    target=float(bars[0][5]) # Kraken minute VWAP; experimental, NOT CF BRTI
    m["estimated_reference"]=True
    print("ESTIMATED REFERENCE",ticker,target,flush=True)
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
   elif left<=780 and ready and historical:
    # Estimate FUTURE movement first, then compare the projected settlement to strike.
    # The current winning side is NOT a directional vote.
    remaining_minutes=max(0.5,left/60)
    expected_move=max(spot*0.00012,historical["atr"]*math.sqrt(remaining_minutes/5))
    distance=(spot-target)/expected_move
    momentum_signal=max(-1.0,min(1.0,score/max(threshold,1)))
    trend_signal=max(-1.0,min(1.0,historical["votes"]/3))
    pattern_signal=max(-1.0,min(1.0,2*(historical.get("pattern_up",0.5)-0.5)))
    reversal=(p120 is not None and (spot-p30)*(spot-p120)<0 and
              abs(spot-p30)>threshold*0.5)
    # Reduce reliance on short-term momentum if it has started reversing.
    momentum_weight=0.22 if reversal else 0.50
    # Future movement estimate is independent of the current strike distance.
    weights=adaptive_weights() if db_ready else (0.50,0.25,0.25)
    momentum_weight=weights[0]*(0.44 if reversal else 1.0)
    future_move=expected_move*(momentum_weight*momentum_signal+
                               weights[1]*trend_signal+weights[2]*pattern_signal)
    projected_finish=spot+future_move
    forecast=(projected_finish-target)/expected_move
    direction="UP" if forecast>=0 else "DOWN"
    # Record whether the model actually predicts a reversal of the current leader.
    current_leader="UP" if spot>=target else "DOWN"
    predicts_flip=(direction!=current_leader)
    agreement=(momentum_signal*trend_signal>0 and
               momentum_signal*pattern_signal>0 and not reversal)
    strength=("PROJECTED REVERSAL / UNCALIBRATED" if predicts_flip else
              "REVERSAL RISK / UNCALIBRATED" if reversal else
              "MULTI-FACTOR AGREEMENT / UNCALIBRATED" if agreement else
              "MIXED SIGNALS / UNCALIBRATED")
    locked_at=now
    if db_ready:
     lock(ticker,close,direction,strength,now)
     track(ticker,close,direction,strength,now,spot,m.get("yes_ask"),m.get("no_ask"))
     track_features(ticker,forecast,distance,momentum_signal,trend_signal,historical.get("pattern_up",0.5))
     record_baseline(ticker,current_leader,direction,future_move,spot-target,now)
    record_cache=(direction,strength,now)
   elif left>780:direction,strength,locked_at="ANALYZING","STUDYING FIRST 2 MINUTES",None
   else:direction,strength,locked_at="ANALYZING","LOADING HISTORICAL CANDLES / MOMENTUM",None
   if db_ready and now-last_verify>=60:
    last_verify=now
    verify()
   if db_ready and now%10==0:record_observation(ticker,now,spot,target,direction,m)
   if m.get("estimated_reference"):strength="ESTIMATED KRAKEN REFERENCE / NOT OFFICIAL BRTI"
   conflict=direction in ("UP","DOWN") and (spot>=target)!=(direction=="UP")
   with L:
    h=history
    S.update(status="LIVE",error="",ticker=ticker,btc=spot,target=target,difference=spot-target,remaining=left,locked=direction,strength=strength,locked_at=locked_at,tracking_online=db_ready,historical=historical,reference_is_estimated=bool(m.get("estimated_reference")),conflict=conflict,cashout=cashout_signal(direction,m.get("yes_ask"),m.get("no_ask"),conflict,m.get("yes_bid"),m.get("no_bid")),quotes=prices,history=h,updated=now,up_ask=m.get("yes_ask"),down_ask=m.get("no_ask"),up_bid=m.get("yes_bid"),down_bid=m.get("no_bid"))
  except Exception as e:
   print("COLLECTOR",repr(e),flush=True)
   try:
    with ThreadPoolExecutor(max_workers=3) as pool:
     fs={n:pool.submit(btc,n) for n in ("kraken","coinbase","bitstamp")}
     prices={}
     for n,f in fs.items():
      try:prices[n]=f.result(timeout=9)
      except Exception:pass
    with L:
     S.update(status="MARKET UNAVAILABLE",error=str(e),btc=prices.get("kraken",next(iter(prices.values())) if prices else None),quotes=prices,updated=int(time.time()),locked="ANALYZING",strength="Kalshi market feed unavailable; no prediction",target=None,remaining=None)
   except Exception as spot_ex:
    with L:S.update(status="FEED ERROR",error=str(e)+"; spot: "+str(spot_ex))
   time.sleep(14)
  time.sleep(1)
HTML='''<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#090e18"><title>Signal Lab</title><style>
*{box-sizing:border-box}body{margin:0;background:#090e18;color:#eef5ff;font:16px system-ui,sans-serif}.wrap{max-width:760px;margin:auto;padding:22px 18px 70px}.head{display:flex;justify-content:space-between;align-items:center;margin-bottom:26px}.logo{font-size:25px;font-weight:900}.pill{background:#243750;border-radius:99px;padding:9px 12px;font-size:12px}.card{background:linear-gradient(140deg,#152641,#101c30);border:1px solid #2b4261;border-radius:24px;padding:23px;margin:16px 0}.label{font-size:12px;font-weight:800;letter-spacing:2px;color:#a4b7d0}.signal{font-size:clamp(65px,17vw,110px);font-weight:900;letter-spacing:-5px;margin:10px 0}.up{color:#2ee0aa}.down{color:#ff6886}.warn{color:#ffc26b}.muted{color:#9fb3cd}.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}.price{font-size:clamp(24px,6vw,40px);font-weight:850;letter-spacing:-1px;margin:8px 0}.between{display:flex;align-items:center;justify-content:space-between;gap:8px}.timer{font-size:29px;font-weight:850}.bar{height:9px;border-radius:9px;background:#293d56;overflow:hidden;margin:20px 0}.fill{height:100%;width:0;background:linear-gradient(90deg,#42a7f7,#2edcad)}.diff{font-size:22px;font-weight:800;margin:24px 0}.notice{padding:14px;border:1px solid #91673e;border-radius:13px;background:#403020;margin-top:17px}svg{display:block;width:100%;height:170px}details{background:#121f34;border:1px solid #2b4261;border-radius:16px;padding:20px;margin:14px 0}summary{font-weight:750;cursor:pointer}.small{font-size:13px;line-height:1.7}
body{background:#0b1020!important;color:#e9eefb!important}.wrap{max-width:1180px!important}.card{background:linear-gradient(145deg,#1b2540,#141c30)!important;border:1px solid #303957!important;box-shadow:0 8px 25px #05081444!important}.logo{color:#b49bff!important}.pill{background:#303253!important;color:#a9efcf!important}.label{color:#b5b1da!important}.fill{background:linear-gradient(90deg,#ad8bff,#6ce8bc)!important}.signal.up,.up{color:#6ce8bc!important}.signal.down,.down{color:#ff7e9f!important}.price{color:#f0edff!important}svg{background:linear-gradient(180deg,#23254655,#151c3000);border-radius:12px}details{background:#161e33!important;border-color:#343857!important}@media(min-width:900px){.wrap{padding-top:32px!important}.head{margin-bottom:36px!important}.card{padding:30px!important}}.wrap{max-width:1200px;display:grid;grid-template-columns:2fr 1fr;gap:16px}.head{grid-column:1/-1}.wrap>.card{margin:0}.wrap>.card:nth-of-type(1){grid-column:2;grid-row:2}.wrap>.card:nth-of-type(2){grid-column:1;grid-row:2}.wrap>.card:nth-of-type(3){grid-column:1/-1}.wrap>details{grid-column:1/-1}@media(max-width:700px){.wrap{display:flex;flex-direction:column}}.wrap{grid-template-columns:210px minmax(0,2fr) minmax(0,1fr)!important;gap:16px!important}.sidebar{grid-column:1;grid-row:1/5;background:#121a2e;border:1px solid #303957;border-radius:20px;padding:24px 15px;display:flex;flex-direction:column;gap:12px}.sidebrand{color:#b49bff;font-size:19px;font-weight:900;padding:8px 10px 26px}.sideitem{font-size:14px;color:#a8b3d2;padding:14px;border-radius:12px}.sideitem.active{background:#302957;color:#d9cbff;font-weight:800}.sidefoot{margin-top:auto;color:#7686a8;font-size:11px;line-height:2;padding:12px}.head{grid-column:2/-1!important;grid-row:1!important;margin:0!important;padding:14px 18px;border:1px solid #303957;border-radius:16px;background:#161e33}.wrap>.card:nth-of-type(1){grid-column:3!important;grid-row:2!important}.wrap>.card:nth-of-type(2){grid-column:2!important;grid-row:2!important}.wrap>.card:nth-of-type(3){grid-column:2/-1!important;grid-row:3!important}.wrap>details{grid-column:2/-1!important}.signal{font-size:clamp(35px,4vw,65px)!important;letter-spacing:-2px!important;overflow-wrap:anywhere}.card{border-radius:18px!important}@media(max-width:900px){.wrap{display:flex!important;flex-direction:column!important;gap:12px!important;padding:14px!important}.sidebar{display:flex;flex-direction:row;align-items:center;overflow-x:auto;padding:8px!important;gap:6px;border-radius:13px}.sidebrand{font-size:14px;padding:8px;white-space:nowrap}.sideitem{font-size:12px;padding:10px;white-space:nowrap}.sidefoot{display:none}.head{padding:12px 16px!important}.logo{font-size:18px!important}.card{padding:18px!important}.wrap>.card:nth-of-type(1){order:1}.wrap>.card:nth-of-type(2){order:2}.wrap>.card:nth-of-type(3){order:3}.wrap>details{order:4}.signal{font-size:clamp(36px,10vw,62px)!important;letter-spacing:-2px!important}.price{font-size:clamp(18px,5vw,32px)!important}.label{font-size:10px!important;letter-spacing:1px!important}.diff{font-size:17px!important;margin:14px 0!important}svg{height:210px!important}}.heading{display:flex;flex-direction:column;gap:4px}.eyebrow{font-size:10px;letter-spacing:1.5px;color:#8e9cb9}.subheading{font-size:12px;color:#8999b8}.metrics{grid-column:2/-1;display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}.metric{min-width:0;background:linear-gradient(125deg,#1d2943,#171e33);border:1px solid #303957;border-radius:17px;padding:18px;display:flex;flex-direction:column;gap:10px}.metric span{font-size:10px;letter-spacing:1px;color:#9aa6c3}.metric strong{font-size:clamp(16px,2vw,26px);white-space:nowrap;letter-spacing:-.8px}.metric small{font-size:11px;color:#7e90b0}.wrap>.card:nth-of-type(1){grid-row:3!important}.wrap>.card:nth-of-type(2){grid-row:3!important}.wrap>.card:nth-of-type(3){grid-row:4!important}.sidebar{grid-row:1/6!important}.wrap>details{grid-column:2/-1!important}@media(max-width:900px){.metrics{order:0;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:9px}.metric{padding:13px;border-radius:12px}.metric strong{font-size:clamp(16px,4.3vw,23px)}.metric span{font-size:9px}.metric small{font-size:10px}.head{order:0}.sidebar{order:0}.wrap>.card:nth-of-type(1){order:1}.wrap>.card:nth-of-type(2){order:2}.wrap>.card:nth-of-type(3){order:3}}body{background:#0a0915!important}.sidebar{background:#100e22!important;border-color:#27213e!important}.sideitem.active{background:#6334b7!important;color:white!important}.sidebrand,.logo{color:#d0aaff!important}.head{background:#110f24!important;border-color:#27213e!important}.card,.metric{background:linear-gradient(135deg,#1c1537,#100f23)!important;border:1px solid #312449!important;box-shadow:0 8px 32px #05030e66!important}.metric:nth-child(2){background:linear-gradient(130deg,#17152f,#111a27)!important}.metric:nth-child(3){background:linear-gradient(130deg,#1e1631,#141020)!important}.metric:nth-child(4){background:linear-gradient(130deg,#20142c,#141020)!important}.metric strong{color:#f1e9ff!important}.fill{background:linear-gradient(90deg,#8d4de5,#d1a0ff)!important}.signal.warn{color:#ffbc69}.pill{background:#30214d!important;color:#dfc8ff!important}.donutrow{display:flex;align-items:center;justify-content:center;gap:24px;margin:22px 0}.donut{--pct:0;width:170px;height:170px;flex:none;border-radius:50%;background:conic-gradient(#a576ff calc(var(--pct)*1%),#ffb660 0);display:grid;place-items:center;box-shadow:0 0 30px #6731ac30}.donutcore{width:118px;height:118px;border-radius:50%;background:#151128;display:grid;place-items:center;color:#fff;font-size:31px;font-weight:850}.donutlegend{display:grid;gap:13px;color:#aab0cb;font-size:12px}.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:7px}.dot.good{background:#a576ff}.dot.bad{background:#ffb660}.dot.neutral{background:#6c6687}.wrap>.card:nth-of-type(3){grid-column:3!important;grid-row:4!important}.wrap>.card:nth-of-type(2){grid-row:3/5!important}#chart{stroke:#ae82ff!important;stroke-width:3.5!important}svg{height:270px!important;background:linear-gradient(180deg,#6c3cae17,#0c0b1a00)!important}@media(max-width:900px){.donutrow{gap:12px}.donut{width:130px;height:130px}.donutcore{width:90px;height:90px;font-size:25px}.wrap>.card:nth-of-type(2){order:2}.wrap>.card:nth-of-type(3){order:3}svg{height:190px!important}}
/* Signal Lab 2026 visual refresh */
:root{color-scheme:dark;--line:#342b55;--violet:#a783ff;--mint:#70edce}
body{background:radial-gradient(ellipse 80% 45% at 65% -15%,#34205d 0%,transparent 75%),#090b17!important;font-family:Inter,ui-sans-serif,system-ui,-apple-system,'Segoe UI',sans-serif!important}
.wrap{gap:18px!important}.sidebar{background:linear-gradient(170deg,#18142b,#100f1e)!important;box-shadow:0 16px 50px #05040c55;position:relative}.sidebrand{letter-spacing:1px}.sideitem{transition:background .2s,color .2s}.sideitem:not(.active):hover{background:#29203f;color:#fff}
.head{background:linear-gradient(115deg,#19162c,#141425)!important;box-shadow:0 10px 36px #0003}.card,.metric{transition:transform .2s,border-color .2s,box-shadow .2s;border-color:#392d58!important;backdrop-filter:blur(14px)}.card:hover,.metric:hover{border-color:#7051a2!important;box-shadow:0 12px 38px #090416aa!important}.metric{position:relative;overflow:hidden}.metric:before{content:'';position:absolute;top:0;left:18px;right:18px;height:2px;background:linear-gradient(90deg,#b18bff,transparent);opacity:.7}.metric strong{font-variant-numeric:tabular-nums}.pill{border:1px solid #61458a;box-shadow:inset 0 1px #ffffff14}.signal{text-shadow:0 0 32px #a576ff28}.card{overflow:hidden}.bar{height:11px;background:#27203e;box-shadow:inset 0 2px 4px #0005}.fill{box-shadow:0 0 16px #c392ff}.price{font-variant-numeric:tabular-nums}.timer{font-variant-numeric:tabular-nums;color:#e2ccff}details{transition:border-color .2s}details[open]{border-color:#7750ae!important}summary:hover{color:#d3b9ff}button,input,select{font:inherit}a{color:#c4a6ff}
@media(max-width:900px){.wrap{gap:13px!important;padding:12px!important}.sidebar{box-shadow:none}.head{border-radius:16px}.metric{min-height:98px}.card{border-radius:18px!important}.metrics{gap:10px}.sideitem{border-radius:10px}.head .logo{font-size:22px!important}}
@media(prefers-reduced-motion:reduce){*,*:before,*:after{transition:none!important;animation:none!important}}
</style></head><body><main class="wrap"><aside class="sidebar"><div class="sidebrand">ϟ SIGNAL LAB</div><div class="sideitem active">◈ &nbsp; Overview</div><a class="sideitem" href="/account" style="text-decoration:none">♙ &nbsp; Membership</a><div class="sideitem">⌁ &nbsp; Live Market</div><div class="sideitem">◎ &nbsp; Predictions</div><div class="sideitem">▥ &nbsp; Performance</div><div class="sidefoot">BTC / 15 MIN<br>EXPERIMENTAL MODEL<br>Not financial advice</div></aside><header class="head"><div class="heading"><span class="eyebrow">OVERVIEW / DASHBOARD</span><span class="logo">Dashboard</span><span class="subheading">Real-time market intelligence · BTC 15-minute contracts</span></div><span class="pill" id="status" aria-live="polite">CONNECTING</span><a href="/account" style="font-size:12px;text-decoration:none;white-space:nowrap">Account ↗</a></header><div class="metrics"><div class="metric"><span>LIVE BTC PRICE</span><strong id="metricbtc">—</strong><small>Exchange composite</small></div><div class="metric"><span>KALSHI TARGET</span><strong id="metrictarget">—</strong><small>Estimated reference · verify with Kalshi</small></div><div class="metric"><span>TIME REMAINING</span><strong id="metrictime">—</strong><small>15-minute market</small></div><div class="metric"><span>TRACKED SIGNAL ACCURACY</span><strong id="metricaccuracy">—</strong><small>Settled signals only</small></div></div><section class="card"><div class="between"><span class="label">EARLY PICK · LOCKED AT MINUTE 2</span><span class="pill" id="phase">ANALYZING</span></div><div id="signal" class="signal warn">WAIT</div><div id="strength" class="muted">Waiting for exchange data</div><div id="notice" class="notice" hidden></div></section><section class="card"><div class="between"><span class="label">CURRENT ROUND</span><span class="timer" id="timer">--:--</span></div><div class="bar"><div class="fill" id="progress"></div></div><div class="grid"><div><div class="label">KALSHI TARGET</div><div class="price" id="target">—</div><div class="muted small">Estimated reference · not settlement feed</div></div><div><div class="label">NOW <span id="arrow"></span></div><div class="price" id="now">—</div><div class="muted small">Updates ~1 second</div></div></div><div class="diff" id="diff">—</div><div class="label">BTC LIVE MOVEMENT</div><svg viewBox="0 0 600 170" preserveAspectRatio="none"><path id="reference" fill="none" stroke="#8c9fb7" stroke-dasharray="6 6"/><path id="chart" fill="none" stroke="#32ddb0" stroke-width="3" stroke-linejoin="round"/></svg><div class="between muted small"><span id="up">UP buy —</span><span id="down">DOWN buy —</span></div></section><section class="card"><div class="label">NEW VERSION PERFORMANCE · OFFICIAL SETTLEMENT</div><div class="donutrow"><div class="donut" id="accuracydonut"><div class="donutcore" id="donutlabel">—</div></div><div class="donutlegend"><div><i class="dot good"></i> Correct predictions</div><div><i class="dot bad"></i> Incorrect predictions</div><div><i class="dot neutral"></i> Pending results excluded</div></div></div><div class="grid" style="margin-top:15px"><div><div class="label">GRADED SIGNALS</div><div class="price" id="graded">0</div></div><div><div class="label">ACCURACY</div><div class="price" id="accuracy">—</div></div></div><p class="muted small" id="scorecard">No verified results yet</p><div id="recent" class="muted small"></div><p class="muted small">Accuracy excludes WAIT rounds and pending settlements. No results are invented. Experimental model; historical performance does not guarantee future outcomes.</p></section><details open><summary>Early exit monitor (not an executable sell signal)</summary><p class="muted small" id="cashout">Waiting for quotes</p><p class="muted small">Profitable cash-out requires actual bid, available liquidity, entry price, and fees. Ask prices alone cannot establish profit.</p></details><details><summary>Exchange checks</summary><p class="muted small" id="quotes">Waiting for data</p></details><details><summary>Manual cash-out profit calculator</summary><p class="small muted">Enter your purchase price and quantity. Indicative quotes may change before an order fills.</p><label>Side <select id="side"><option value="UP">UP</option><option value="DOWN">DOWN</option></select></label> <label>Entry price (cents) <input id="entry" type="number" min="1" max="99" step="0.01"></label> <label>Contracts <input id="qty" type="number" min="1" step="1" value="1"></label> <label>Estimated total fees ($) <input id="fees" type="number" min="0" step="0.01" value="0"></label><p class="small" id="profit">Waiting for live bid</p></details><details><summary>Automatic learning evaluation (shadow mode)</summary><p class="muted small" id="learning">Collecting live prediction features</p></details><details><summary>Prediction mistake audit</summary><p class="muted small" id="audit">Waiting for graded results</p></details><details><summary>Diagnostics</summary><p class="muted small" id="ticker"></p><p class="muted small" id="indicators"></p><p class="muted small">Experimental forecast, not a calibrated probability. Kalshi's settlement index may differ from exchange spot prices.</p></details></main><script>
const e=id=>document.getElementById(id),money=n=>'$'+Number(n).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});let last=null;
function chart(a,t){if(!a||a.length<2)return;let v=a.map(p=>p[1]),low=Math.min(t,...v),high=Math.max(t,...v),pad=Math.max((high-low)*.15,2);low-=pad;high+=pad;let y=z=>160-(z-low)/(high-low)*150,x=i=>i*600/(v.length-1);e('chart').setAttribute('d',v.map((z,i)=>(i?'L':'M')+x(i).toFixed(1)+' '+y(z).toFixed(1)).join(' '));e('reference').setAttribute('d','M0 '+y(t)+' L600 '+y(t))}
async function calculateProfit(){let b=window.currentBids||{},side=e('side')?.value,entry=Number(e('entry')?.value),qty=Number(e('qty')?.value),fees=Number(e('fees')?.value),bid=Number(b[side]);if(!e('profit'))return;if(!e('entry').value||!Number.isFinite(bid)||b[side]==null||entry<=0||qty<1||fees<0){e('profit').textContent='Enter purchase price and wait for a live bid.';return}let pnl=(bid-entry)*qty/100-fees;e('profit').textContent='Bid '+bid+' cents; estimated P/L $'+pnl.toFixed(2)+' before slippage or unentered fees'}document.addEventListener('input',calculateProfit);document.addEventListener('change',calculateProfit);
async function refresh(){try{let r=await fetch('/api?'+Date.now(),{cache:'no-store'}),d=await r.json();e('status').textContent=d.status;if(d.btc!=null)e('metricbtc').textContent=money(d.btc);if(d.btc==null||d.target==null){e('strength').textContent=d.error||'Waiting for Kalshi market data';e('signal').textContent='WAIT';return;}e('target').textContent=money(d.target);e('metrictarget').textContent=money(d.target);e('metricbtc').textContent=money(d.btc);e('now').textContent=money(d.btc);let dif=d.btc-d.target;e('diff').textContent=(dif>=0?'▲ +$':'▼ -$')+Math.abs(dif).toFixed(2)+(dif>=0?' above target':' below target');e('diff').className='diff '+(dif>=0?'up':'down');if(last!==null){let z=d.btc-last;e('arrow').textContent=z>0?'▲':z<0?'▼':'•';e('arrow').className=z>0?'up':z<0?'down':'muted'}last=d.btc;let s=d.remaining;e('timer').textContent=Math.floor(s/60)+':'+String(s%60).padStart(2,'0');e('metrictime').textContent=e('timer').textContent;e('progress').style.width=(100*(900-s)/900)+'%';e('signal').textContent=d.locked;e('signal').className='signal '+(d.locked==='UP'?'up':d.locked==='DOWN'?'down':'warn');e('strength').textContent=d.strength?.includes('MULTI-FACTOR')?'Multiple indicators agree':d.strength?.includes('REVERSAL')?'Mixed market signals':d.strength?.includes('MIXED')?'Mixed market signals':'Prediction under review';e('phase').textContent=d.locked==='ANALYZING'?'ANALYZING':'LOCKED';e('up').textContent='UP buy '+(d.up_ask??'—')+'¢';e('down').textContent='DOWN buy '+(d.down_ask??'—')+'¢';e('quotes').textContent=Object.entries(d.quotes||{}).map(([k,v])=>k+': '+money(v)).join(' · ');e('ticker').textContent=d.ticker;e('cashout').textContent=d.cashout||'NO SIGNAL';window.currentBids={UP:d.up_bid,DOWN:d.down_bid};calculateProfit();let p=d.performance||{};let learning=p.model_audit||{};e('learning').textContent=JSON.stringify(learning);let audit=p.mistake_audit||{};e('audit').textContent=(audit.sample??0)+' recent settled signals analyzed · '+(audit.mistakes??0)+' incorrect · '+(audit.recent_reasons||[]).map(x=>x.reason).join(', ')+'. Diagnostic categories only; not proof of cause or automatic retraining.';let h=d.historical;e('indicators').textContent=h?'5-minute historical candles: '+h.candles+' · RSI '+h.rsi+' · EMA9 '+h.ema9+' · EMA21 '+h.ema21+' · ATR '+h.atr+' · 15m pattern UP frequency '+Math.round(100*(h.pattern_up??0.5))+'% ('+(h.pattern_samples??0)+' matches) · volume ratio '+(h.volume_ratio??'—')+' · support '+h.support+' · resistance '+h.resistance:'Historical candle feed unavailable';e('graded').textContent=p.graded??0;e('accuracy').textContent=p.accuracy==null?'—':p.accuracy+'%';e('metricaccuracy').textContent=e('accuracy').textContent;let ap=Number(p.accuracy);e('donutlabel').textContent=p.accuracy==null?'—':ap+'%';e('accuracydonut').style.setProperty('--pct',p.accuracy==null?0:Math.max(0,Math.min(100,ap)));e('scorecard').textContent=(d.tracking_online?'':'⚠ Database tracking offline · ')+(p.wins??0)+' wins · '+(p.losses??0)+' losses · '+(p.skips??0)+' skips · '+(p.pending??0)+' pending';e('recent').innerHTML=(p.last||[]).slice(0,5).map(x=>'<div style="padding:5px 0;border-bottom:1px solid #302449">'+String(x.pick).replace(/[^A-Z]/g,'')+' → '+String(x.result).replace(/[^A-Z]/gi,'')+'</div>').join('');e('notice').hidden=!d.conflict;e('notice').textContent=d.conflict?'Market movement has changed since this prediction. Review before trading.':'';chart(d.history,d.target)}catch(err){e('status').textContent='RECONNECTING'}}
refresh();setInterval(refresh,1000)
</script></body></html>'''
import signal_lab_membership as membership
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  path=self.path.split("?")[0]
  if membership.get(self,path):return
  if path=="/api":
   if os.getenv("SIGNAL_LAB_PREMIUM_ENABLED")=="true" and membership.ready() and not membership.allowed(membership.session(self)):
    membership.respond(self,403,{"error":"Active subscription required","account_url":"/account"});return
   with L:data=json.dumps(S).encode()
   typ="application/json"
  elif path=="/":data=HTML.encode();typ="text/html; charset=utf-8"
  else:self.send_error(404);return
  self.send_response(200);self.send_header("Content-Type",typ);self.send_header("Cache-Control","no-store");self.send_header("X-Content-Type-Options","nosniff");self.send_header("Content-Length",str(len(data)));self.end_headers();self.wfile.write(data)
 def do_POST(self):
  if not membership.post(self,self.path.split("?")[0]):self.send_error(404)
 def log_message(self,*args):pass
threading.Thread(target=cycle,daemon=True).start()
ThreadingHTTPServer(("0.0.0.0",int(os.getenv("PORT","10000"))),Handler).serve_forever()
