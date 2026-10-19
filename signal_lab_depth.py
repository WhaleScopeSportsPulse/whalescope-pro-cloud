"""Public exchange depth snapshots. Research-only; never used to place trades."""
import json,time,urllib.request
def _get(url):
 req=urllib.request.Request(url,headers={"User-Agent":"SignalLab/22","Accept":"application/json"})
 with urllib.request.urlopen(req,timeout=5) as r:return json.load(r)
def _side(levels,price_index=0,size_index=1):
 rows=[]
 for x in levels[:25]:
  try:
   p=float(x[price_index]);q=float(x[size_index])
   if p>0 and q>0:rows.append((p,q))
  except (ValueError,TypeError,IndexError):pass
 return sum(p*q for p,q in rows)
def snapshot():
 result={"timestamp":int(time.time()),"research_only":True,"exchanges":{}}
 for name,url in (
  ("kraken","https://api.kraken.com/0/public/Depth?pair=XBTUSD&count=25"),
  ("coinbase","https://api.exchange.coinbase.com/products/BTC-USD/book?level=2")):
  try:
   raw=_get(url)
   book=next(iter(raw["result"].values())) if name=="kraken" else raw
   bid=_side(book["bids"]);ask=_side(book["asks"])
   total=bid+ask
   result["exchanges"][name]={"bid_notional_usd":round(bid,2),"ask_notional_usd":round(ask,2),"imbalance":round((bid-ask)/total,4) if total else None,"status":"LIVE" if total else "EMPTY"}
  except Exception as exc:
   result["exchanges"][name]={"status":"UNAVAILABLE","error":str(exc)[:100]}
 return result
