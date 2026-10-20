"""Authenticated Kalshi CF Benchmarks BRTI research feed.
Requires KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PEM (PEM contents).
This is research-only: do not infer a settlement value from an incomplete window.
"""
import base64, datetime, json, os, threading, time
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

URL = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
PATH = "/trade-api/ws/v2"

def auth_headers(key_id, private_pem, timestamp_ms=None):
    if not key_id or not private_pem:
        raise ValueError("Kalshi API key ID and RSA private key are required")
    ts = str(timestamp_ms if timestamp_ms is not None else int(time.time()*1000))
    key = serialization.load_pem_private_key(private_pem.replace("\\n", "\n").encode(), password=None)
    signature = key.sign((ts+"GET"+PATH).encode(), padding.PSS(mgf=padding.MGF1(hashes.SHA256()),salt_length=padding.PSS.DIGEST_LENGTH),hashes.SHA256())
    return {"KALSHI-ACCESS-KEY":key_id,"KALSHI-ACCESS-TIMESTAMP":ts,"KALSHI-ACCESS-SIGNATURE":base64.b64encode(signature).decode()}

def parse_brti(message):
    data = json.loads(message) if isinstance(message,str) else message
    if data.get("type")!="cfbenchmarks_value":return None
    msg=data.get("msg") or {}
    if msg.get("index_id")!="BRTI":return None
    raw=json.loads(msg.get("data") or "{}")
    source_ms=int(raw["time"])
    value=float(raw["value"])
    trailing=msg.get("avg_60s_data") or {}
    final=msg.get("last_60s_windowed_average_15min") or {}
    return {"source_ms":source_ms,"value":value,"trailing_60s":float(trailing["value"]) if trailing.get("value") is not None else None,"trailing_samples":int(trailing.get("window_size") or 0),"final_minute_average":float(final["value"]) if final.get("value") is not None else None,"final_minute_samples":int(final.get("window_size") or 0),"final_window_end_ms":final.get("window_end_ts_exclusive")}

def run_forever(on_value, on_status=print):
    import websocket
    key_id=os.environ.get("KALSHI_API_KEY_ID","")
    private_pem=os.environ.get("KALSHI_PRIVATE_KEY_PEM","")
    while True:
        try:
            headers=auth_headers(key_id,private_pem)
            def opened(ws):
                ws.send(json.dumps({"id":1,"cmd":"subscribe","params":{"channels":["cfbenchmarks_value"],"index_ids":["BRTI"]}}))
            def message(ws, payload):
                data=json.loads(payload)
                if data.get("type")=="error":on_status("BRTI subscription error: "+str(data))
                result=parse_brti(data)
                if result:on_value(result)
            ws=websocket.WebSocketApp(URL,header=[k+": "+v for k,v in headers.items()],on_open=opened,on_message=message,on_error=lambda ws,e:on_status("BRTI WS error: "+str(e)),on_close=lambda ws,*args:on_status("BRTI WS disconnected"))
            ws.run_forever(ping_interval=20,ping_timeout=10)
        except Exception as e:on_status("BRTI WS unavailable: "+str(e))
        time.sleep(5)
