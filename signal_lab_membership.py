"""Signal Lab membership endpoints. Inactive unless required secrets are configured."""
import os,json,time,hmac,hashlib,secrets,urllib.request,urllib.parse,datetime,http.cookies
from html import escape
from urllib.parse import urlparse

ORIGIN="https://signal-lab-live.onrender.com"
PRICE="price_1US2FFA8RCnYp8P3J4I4sYGb"
def ready():
 return all(os.getenv(x) for x in ("STRIPE_SECRET_KEY","STRIPE_WEBHOOK_SECRET","RESEND_API_KEY","LOGIN_FROM_EMAIL","SESSION_SECRET")) and os.getenv("STRIPE_SECRET_KEY","").startswith("sk_test_")
def db():
 import pg8000.dbapi as pg
 u=urlparse(os.environ["DATABASE_URL"])
 return pg.connect(host=u.hostname,port=u.port or 5432,user=urllib.parse.unquote(u.username),password=urllib.parse.unquote(u.password or ""),database=u.path.lstrip("/"),ssl_context=True,timeout=8)
def sha(v):return hashlib.sha256(v.encode()).hexdigest()
def digest(v):return hmac.new(os.environ["SESSION_SECRET"].encode(),v.encode(),hashlib.sha256).hexdigest()
def stripe(path,params=None):
 data=urllib.parse.urlencode(params,doseq=True).encode() if params is not None else None
 req=urllib.request.Request("https://api.stripe.com/v1/"+path,data=data,headers={"Authorization":"Bearer "+os.environ["STRIPE_SECRET_KEY"],"Content-Type":"application/x-www-form-urlencoded"})
 with urllib.request.urlopen(req,timeout=12) as r:return json.load(r)
def respond(h,status,obj,cookie=None):
 raw=json.dumps(obj).encode()
 h.send_response(status);h.send_header("Content-Type","application/json");h.send_header("Cache-Control","no-store");h.send_header("X-Content-Type-Options","nosniff")
 if cookie:h.send_header("Set-Cookie",cookie)
 h.send_header("Content-Length",str(len(raw)));h.end_headers();h.wfile.write(raw)
def session(h):
 c=http.cookies.SimpleCookie()
 try:c.load(h.headers.get("Cookie",""));token=c["sl_session"].value
 except (KeyError,http.cookies.CookieError):return None
 if not token:return None
 conn=db()
 try:
  q=conn.cursor();q.execute("SELECT m.id,m.email,m.stripe_customer_id,m.subscription_status FROM signal_lab_sessions s JOIN signal_lab_members m ON m.id=s.member_id WHERE s.token_hash=%s AND s.expires_at>now() AND m.email_verified_at IS NOT NULL",(digest(token),))
  return q.fetchone()
 finally:conn.close()
def allowed(member):return bool(member and member[3] in ("active","trialing"))
def send_code(email,code):
 body=json.dumps({"from":os.environ["LOGIN_FROM_EMAIL"],"to":[email],"subject":"Your Signal Lab login code","text":"Your Signal Lab code is "+code+". It expires in 10 minutes. Never share this code."}).encode()
 req=urllib.request.Request("https://api.resend.com/emails",data=body,headers={"Authorization":"Bearer "+os.environ["RESEND_API_KEY"],"Content-Type":"application/json"})
 with urllib.request.urlopen(req,timeout=12) as r:r.read()
def post(h,path):
 n=int(h.headers.get("Content-Length","0"))
 if n<1 or n>100000:respond(h,400,{"error":"Invalid request"});return True
 raw=h.rfile.read(n)
 if path=="/billing/webhook":
  if not ready():respond(h,503,{"error":"Not configured"});return True
  sig=h.headers.get("Stripe-Signature","")
  try:
   parts=dict(x.split("=",1) for x in sig.split(",") if "=" in x);ts=int(parts["t"])
   if abs(time.time()-ts)>300:raise ValueError()
   expected=hmac.new(os.environ["STRIPE_WEBHOOK_SECRET"].encode(),str(ts).encode()+b"."+raw,hashlib.sha256).hexdigest()
   if not any(hmac.compare_digest(expected,x[3:]) for x in sig.split(",") if x.startswith("v1=")):raise ValueError()
   ev=json.loads(raw);eid=ev["id"];typ=ev["type"];obj=ev["data"]["object"]
   conn=db()
   try:
    q=conn.cursor();q.execute("SELECT 1 FROM signal_lab_stripe_events WHERE event_id=%s",(eid,))
    if q.fetchone():respond(h,200,{"ok":True});return True
    if typ=="checkout.session.completed" and obj.get("mode")=="subscription":
     mid=obj.get("client_reference_id")
     if mid and str(mid).isdigit():
      q.execute("UPDATE signal_lab_members SET stripe_customer_id=%s,stripe_subscription_id=%s WHERE id=%s",(obj.get("customer"),obj.get("subscription"),int(mid)))
    elif typ in ("customer.subscription.created","customer.subscription.updated","customer.subscription.deleted"):
     q.execute("UPDATE signal_lab_members SET subscription_status=%s,stripe_subscription_id=%s WHERE stripe_customer_id=%s",(obj.get("status","none"),obj.get("id"),obj.get("customer")))
    q.execute("INSERT INTO signal_lab_stripe_events(event_id,event_type) VALUES (%s,%s)",(eid,typ));conn.commit()
   finally:conn.close()
   respond(h,200,{"ok":True})
  except Exception:respond(h,400,{"error":"Invalid webhook"})
  return True
 if not ready():respond(h,503,{"error":"Membership setup pending"});return True
 try:body=json.loads(raw)
 except Exception:respond(h,400,{"error":"Invalid JSON"});return True
 if path=="/auth/request":
  email=str(body.get("email","")).strip().lower()
  if len(email)>254 or len(email)<5 or "@" not in email or any(c.isspace() for c in email):respond(h,400,{"error":"Enter a valid email"});return True
  conn=db()
  try:
   q=conn.cursor();q.execute("INSERT INTO signal_lab_members(email) VALUES (%s) ON CONFLICT(email) DO NOTHING",(email,))
   q.execute("SELECT id FROM signal_lab_members WHERE email=%s",(email,));mid=q.fetchone()[0]
   q.execute("SELECT COUNT(*) FROM signal_lab_login_codes WHERE member_id=%s AND created_at>now()-interval '1 hour'",(mid,))
   if q.fetchone()[0]>=5:respond(h,429,{"error":"Too many codes requested. Try later."});return True
   code=f"{secrets.randbelow(1000000):06d}";q.execute("INSERT INTO signal_lab_login_codes(member_id,code_hash,expires_at) VALUES (%s,%s,now()+interval '10 minutes')",(mid,digest(str(mid)+":"+code)));conn.commit()
   send_code(email,code)
  finally:conn.close()
  respond(h,200,{"ok":True});return True
 if path=="/auth/verify":
  email=str(body.get("email","")).strip().lower();code=str(body.get("code",""))
  if len(code)!=6 or not code.isdigit():respond(h,400,{"error":"Invalid code"});return True
  conn=db()
  try:
   q=conn.cursor();q.execute("SELECT id FROM signal_lab_members WHERE email=%s",(email,));v=q.fetchone()
   if not v:respond(h,401,{"error":"Invalid or expired code"});return True
   mid=v[0];q.execute("SELECT id,code_hash,attempts FROM signal_lab_login_codes WHERE member_id=%s AND consumed_at IS NULL AND expires_at>now() ORDER BY id DESC LIMIT 1 FOR UPDATE",(mid,));v=q.fetchone()
   if not v or v[2]>=5:respond(h,401,{"error":"Invalid or expired code"});return True
   q.execute("UPDATE signal_lab_login_codes SET attempts=attempts+1 WHERE id=%s",(v[0],))
   if not hmac.compare_digest(v[1],digest(str(mid)+":"+code)):
    conn.commit();respond(h,401,{"error":"Invalid or expired code"});return True
   q.execute("UPDATE signal_lab_login_codes SET consumed_at=now() WHERE id=%s",(v[0],))
   q.execute("UPDATE signal_lab_members SET email_verified_at=now() WHERE id=%s",(mid,))
   token=secrets.token_urlsafe(40)
   q.execute("INSERT INTO signal_lab_sessions(token_hash,member_id,expires_at) VALUES (%s,%s,now()+interval '14 days')",(digest(token),mid));conn.commit()
  finally:conn.close()
  respond(h,200,{"ok":True},"sl_session="+token+"; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age=1209600");return True
 if path=="/auth/logout":
  member=session(h)
  c=http.cookies.SimpleCookie()
  try:c.load(h.headers.get("Cookie",""));token=c["sl_session"].value
  except (KeyError,http.cookies.CookieError):token=None
  if token:
   conn=db()
   try:q=conn.cursor();q.execute("DELETE FROM signal_lab_sessions WHERE token_hash=%s",(digest(token),));conn.commit()
   finally:conn.close()
  respond(h,200,{"ok":True},"sl_session=; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age=0");return True
 if path in ("/billing/checkout","/billing/portal"):
  m=session(h)
  if not m:respond(h,401,{"error":"Sign in first"});return True
  try:
   if path=="/billing/checkout":
    if allowed(m):respond(h,409,{"error":"Already subscribed"});return True
    p={"mode":"subscription","line_items[0][price]":PRICE,"line_items[0][quantity]":"1","client_reference_id":str(m[0]),"success_url":ORIGIN+"/account?checkout=success","cancel_url":ORIGIN+"/account?checkout=cancel"}
    if m[2]:p["customer"]=m[2]
    else:p["customer_email"]=m[1]
    v=stripe("checkout/sessions",p)
   else:
    if not m[2]:respond(h,400,{"error":"No billing account yet"});return True
    v=stripe("billing_portal/sessions",{"customer":m[2],"return_url":ORIGIN+"/account"})
   respond(h,200,{"url":v["url"]})
  except Exception:respond(h,502,{"error":"Billing temporarily unavailable"})
  return True
 return False
def get(h,path):
 if path=="/auth/me":
  if not ready():respond(h,200,{"configured":False,"signed_in":False});return True
  m=session(h);respond(h,200,{"configured":True,"signed_in":bool(m),"email":m[1] if m else None,"premium":allowed(m),"subscription_status":m[3] if m else None});return True
 if path=="/account":
  html=PAGE.encode();h.send_response(200);h.send_header("Content-Type","text/html; charset=utf-8");h.send_header("Cache-Control","no-store");h.send_header("Content-Length",str(len(html)));h.end_headers();h.wfile.write(html);return True
 return False
PAGE="""<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Signal Lab | Account</title><style>*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at top,#332054,#0a0b17 65%);color:#f3ecff;font:16px system-ui,sans-serif;min-height:100vh}main{max-width:510px;margin:7vh auto;padding:24px}.panel{background:#19152d;border:1px solid #43315f;border-radius:24px;padding:30px;box-shadow:0 20px 70px #0005}h1{margin:0 0 8px;font-size:30px}p{color:#b9b1cc;line-height:1.6}input,button{width:100%;padding:14px;border-radius:12px;margin:8px 0;font:inherit}input{background:#100f1e;border:1px solid #5b4775;color:white}button{background:#a783ff;color:#100d1e;border:0;font-weight:800;cursor:pointer}button:disabled{opacity:.5}a{color:#c9adff}#message{min-height:26px;color:#d8c3ff}.hide{display:none}</style><main><p><a href="/">← Dashboard</a></p><section class="panel"><h1>ϟ Signal Lab</h1><p>Founding Member · $9.99/month · Experimental market analytics, not financial advice.</p><p id="message">Checking membership setup…</p><div id="login" class="hide"><input id="email" type="email" autocomplete="email" placeholder="Email address"><button onclick="requestCode()">Email me a login code</button><div id="verify" class="hide"><input id="code" inputmode="numeric" maxlength="6" autocomplete="one-time-code" placeholder="6-digit code"><button onclick="verifyCode()">Verify & sign in</button></div></div><div id="member" class="hide"><p id="identity"></p><button id="subscribe" onclick="billing('/billing/checkout')">Subscribe · $9.99/month (test)</button><button id="manage" onclick="billing('/billing/portal')">Manage subscription</button><button onclick="logout()">Sign out</button></div></section></main><script>
const el=id=>document.getElementById(id);const msg=s=>el('message').textContent=s;
async function api(path,data){const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data||{})});const x=await r.json();if(!r.ok)throw Error(x.error||'Request failed');return x}
async function load(){try{const r=await fetch('/auth/me');const d=await r.json();if(!d.configured){msg('Membership sign-in is being configured. No payments are enabled.');return}el('login').classList.toggle('hide',d.signed_in);el('member').classList.toggle('hide',!d.signed_in);if(d.signed_in){el('identity').textContent=d.email+' · '+(d.premium?'Premium active':d.subscription_status||'No active subscription');el('subscribe').classList.toggle('hide',d.premium);el('manage').classList.toggle('hide',!d.subscription_status||d.subscription_status==='none');msg(d.premium?'Your membership is active.':'Sign in successful. Subscription access requires verified payment.')}else msg('Sign in securely with a one-time email code.')}catch(e){msg('Service temporarily unavailable')}}
async function requestCode(){try{await api('/auth/request',{email:el('email').value});el('verify').classList.remove('hide');msg('Check your email for a 6-digit code.')}catch(e){msg(e.message)}}
async function verifyCode(){try{await api('/auth/verify',{email:el('email').value,code:el('code').value});await load()}catch(e){msg(e.message)}}
async function billing(path){try{const d=await api(path);if(d.url&&d.url.startsWith('https://'))location.assign(d.url)}catch(e){msg(e.message)}}
async function logout(){await api('/auth/logout');await load()}
load();</script></html>"""
