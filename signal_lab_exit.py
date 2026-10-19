"""Signal Lab read-only early-exit quote tool. No Kalshi credentials required."""
import json,re,urllib.request,urllib.parse
from html import escape

BASE="https://external-api.kalshi.com/trade-api/v2/markets/"
def respond(h,status,obj):
 raw=json.dumps(obj).encode()
 h.send_response(status)
 for k,v in (("Content-Type","application/json"),("Cache-Control","no-store"),("X-Content-Type-Options","nosniff"),("Content-Length",str(len(raw)))):
  h.send_header(k,v)
 h.end_headers();h.wfile.write(raw)
def get(h,path):
 if path=="/exit-monitor":
  raw=PAGE.encode()
  h.send_response(200);h.send_header("Content-Type","text/html; charset=utf-8");h.send_header("Cache-Control","no-store");h.send_header("Content-Length",str(len(raw)));h.end_headers();h.wfile.write(raw)
  return True
 if path!="/api/exit-quote":return False
 args=urllib.parse.parse_qs(urllib.parse.urlsplit(h.path).query)
 ticker=args.get("ticker",[""])[0].strip().upper()
 side=args.get("side",["UP"])[0].upper()
 try:entry=float(args.get("entry",[""])[0]);qty=int(args.get("qty",[""])[0])
 except (ValueError,TypeError):respond(h,400,{"error":"Enter an entry price in cents and number of contracts."});return True
 if not re.fullmatch(r"KXBTC15M-[A-Z0-9-]{4,60}",ticker) or side not in ("UP","DOWN") or not (0<=entry<=100) or not (1<=qty<=100000):
  respond(h,400,{"error":"Check ticker, side, entry price (0–100 cents), and quantity."});return True
 try:
  req=urllib.request.Request(BASE+urllib.parse.quote(ticker,safe="-"),headers={"Accept":"application/json","User-Agent":"SignalLab/exit-monitor"})
  with urllib.request.urlopen(req,timeout=8) as response:m=json.load(response).get("market",{})
  # Kalshi YES = UP, NO = DOWN; bids are the executable side, not the ask.
  key="yes_bid_dollars" if side=="UP" else "no_bid_dollars"
  bid=m.get(key)
  if bid is None:respond(h,200,{"status":"NO_QUOTE","ticker":ticker,"message":"No current bid available. Do not assume you can sell."});return True
  bid_cents=round(float(bid)*100,3)
  gross=round((bid_cents-entry)*qty/100,2)
  respond(h,200,{"status":"QUOTE","ticker":ticker,"side":side,"bid_cents":bid_cents,"entry_cents":entry,"contracts":qty,"gross_pnl_usd":gross,"fee_note":"Gross estimate only. Trading fees, slippage, partial fills, and liquidity are NOT included.","market_status":m.get("status"),"bid_size":m.get("yes_bid_size_fp") if side=="UP" else m.get("no_bid_size_fp")})
 except Exception:
  respond(h,503,{"status":"NO_QUOTE","message":"Kalshi quote unavailable. Do not assume profit."})
 return True
PAGE="""<!doctype html><html lang="en"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Signal Lab · Early Exit Monitor</title><style>body{background:#090f1e;color:#f1f5ff;font:16px system-ui;margin:0;padding:20px}main{max-width:530px;margin:auto}a{color:#80c8ff}label{display:block;margin:16px 0 5px}input,select,button{box-sizing:border-box;width:100%;padding:13px;border-radius:9px;border:1px solid #52617a;background:#17233a;color:white;font:inherit}button{background:#176dba;border:0;margin-top:20px;font-weight:700}section{margin-top:18px;background:#17233a;padding:18px;border-radius:12px;white-space:pre-wrap}small{color:#b8c4d8}</style><main><p><a href="/">← Dashboard</a></p><h1>Early Exit Monitor</h1><p>Read-only quote checker. No account connection or trading permissions required.</p><label>Kalshi 15-minute BTC market ticker</label><input id="ticker" placeholder="KXBTC15M-..."><label>Your position</label><select id="side"><option value="UP">UP / YES</option><option value="DOWN">DOWN / NO</option></select><label>Your purchase price (cents per contract)</label><input id="entry" type="number" min="0" max="100" step=".01" placeholder="40"><label>Number of contracts</label><input id="qty" type="number" min="1" step="1" placeholder="10"><button id="check">Check live sell bid</button><section id="result" aria-live="polite">Enter your trade details to check a quote.</section><p><small>This does not connect to your account, execute trades, guarantee a fill, or calculate net profit after fees. Never paste API keys or passwords here.</small></p></main><script>document.getElementById('check').onclick=async()=>{const p=new URLSearchParams();for(const id of ['ticker','side','entry','qty'])p.set(id,document.getElementById(id).value);const out=document.getElementById('result');out.textContent='Checking...';try{const r=await fetch('/api/exit-quote?'+p);const d=await r.json();out.textContent=d.status==='QUOTE'?'Current sell bid: '+d.bid_cents+'¢\\nEstimated GROSS P/L: $'+d.gross_pnl_usd+'\\nMarket: '+d.market_status+'\\n'+d.fee_note:(d.message||d.error||'Quote unavailable')}catch(e){out.textContent='Quote unavailable. Do not assume profit.'}};</script></html>"""
