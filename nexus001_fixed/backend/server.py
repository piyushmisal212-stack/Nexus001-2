"""Nexus 001 — Peer Lending Recovery Tracker (FastAPI backend)"""
from fastapi import FastAPI, APIRouter, HTTPException, Request, Response, Depends
from fastapi.responses import JSONResponse, RedirectResponse
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, EmailStr
from typing import List, Optional, Literal
from datetime import datetime, timezone, timedelta, date
from cryptography.fernet import Fernet
import os, io, base64, uuid, logging
from pathlib import Path
import httpx
import qrcode
from email.message import EmailMessage
import aiosmtplib

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

ENCRYPTION_KEY = os.environ['ENCRYPTION_KEY'].encode()
fernet = Fernet(ENCRYPTION_KEY)

GOOGLE_CLIENT_ID = os.environ['GOOGLE_CLIENT_ID']
GOOGLE_CLIENT_SECRET = os.environ['GOOGLE_CLIENT_SECRET']
FRONTEND_URL = os.environ.get('FRONTEND_URL', 'http://localhost:3000')
BACKEND_URL = os.environ.get('BACKEND_URL', 'http://localhost:8001')

app = FastAPI(title="Nexus 001 API")
api_router = APIRouter(prefix="/api")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("nexus001")

AVATAR_PALETTE = ["#3b82f6","#22c55e","#f59e0b","#ef4444","#a855f7","#ec4899","#14b8a6"]

# ==================== MODELS ====================
class UserPublic(BaseModel):
    user_id: str; email: str; name: str
    picture: Optional[str]=None; college: Optional[str]=None
    branch: Optional[str]=None; upi_id: Optional[str]=None
    gmail_address: Optional[str]=None; has_email_creds: bool=False
    whatsapp_session_active: bool=False; onboarded: bool=False

class OnboardIn(BaseModel):
    name: str; college: str; branch: str; upi_id: str

class ProfileIn(BaseModel):
    name: Optional[str]=None; college: Optional[str]=None
    branch: Optional[str]=None; upi_id: Optional[str]=None

class EmailSettingsIn(BaseModel):
    gmail_address: EmailStr; gmail_app_password: str

class TransactionIn(BaseModel):
    borrower_name: str; borrower_phone: str
    borrower_email: Optional[str]=None; amount: float
    due_date: str; category: Literal["LOAN","SHARED","PROJECT","FOOD","TRAVEL"]
    notes: Optional[str]=None

class TransactionUpdate(BaseModel):
    borrower_name: Optional[str]=None; borrower_phone: Optional[str]=None
    borrower_email: Optional[str]=None; amount: Optional[float]=None
    due_date: Optional[str]=None; category: Optional[str]=None
    notes: Optional[str]=None; status: Optional[Literal["Pending","Overdue","Paid"]]=None

class Transaction(BaseModel):
    id: str; user_id: str; borrower_name: str; borrower_phone: str
    borrower_email: Optional[str]=None; amount: float; due_date: str
    category: str; status: str; nudge_count: int=0
    last_nudged_at: Optional[str]=None; last_nudge_channel: Optional[str]=None
    avatar_color: str; initials: str; notes: Optional[str]=None; created_at: str

class NudgeSendIn(BaseModel):
    message: str; level: int=0

# ==================== HELPERS ====================
def initials_of(name):
    parts=[p for p in name.strip().split() if p]
    if not parts: return "?"
    if len(parts)==1: return parts[0][:2].upper()
    return (parts[0][0]+parts[-1][0]).upper()

def compute_status(due_date, current=None):
    if current=="Paid": return "Paid"
    try:
        due=date.fromisoformat(due_date)
    except: return current or "Pending"
    return "Overdue" if due<datetime.now(timezone.utc).date() else "Pending"

def encrypt_str(s): return fernet.encrypt(s.encode()).decode()
def decrypt_str(s): return fernet.decrypt(s.encode()).decode()

def to_public_user(u):
    return UserPublic(user_id=u["user_id"],email=u["email"],name=u.get("name",""),
        picture=u.get("picture"),college=u.get("college"),branch=u.get("branch"),
        upi_id=u.get("upi_id"),gmail_address=u.get("gmail_address"),
        has_email_creds=bool(u.get("gmail_app_password_enc")),
        whatsapp_session_active=bool(u.get("whatsapp_session_active",False)),
        onboarded=bool(u.get("upi_id")))

async def get_session_token(request):
    t=request.cookies.get("session_token")
    if t: return t
    auth=request.headers.get("Authorization","")
    if auth.lower().startswith("bearer "): return auth.split(" ",1)[1].strip()
    return None

async def current_user(request: Request):
    token=await get_session_token(request)
    if not token: raise HTTPException(401,"Not authenticated")
    sess=await db.user_sessions.find_one({"session_token":token},{"_id":0})
    if not sess: raise HTTPException(401,"Invalid session")
    exp=sess.get("expires_at")
    if isinstance(exp,str): exp=datetime.fromisoformat(exp)
    if exp and exp.tzinfo is None: exp=exp.replace(tzinfo=timezone.utc)
    if exp and exp<datetime.now(timezone.utc): raise HTTPException(401,"Session expired")
    user=await db.users.find_one({"user_id":sess["user_id"]},{"_id":0})
    if not user: raise HTTPException(401,"User not found")
    return user

def make_qr_b64(data):
    qr=qrcode.QRCode(box_size=10,border=2,error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(data); qr.make(fit=True)
    img=qr.make_image(fill_color="#0a0a0a",back_color="#ffffff")
    buf=io.BytesIO(); img.save(buf,format="PNG")
    return "data:image/png;base64,"+base64.b64encode(buf.getvalue()).decode()

def upi_link(upi_id,name,amount=None):
    from urllib.parse import quote_plus
    parts=[f"pa={quote_plus(upi_id)}",f"pn={quote_plus(name)}"]
    if amount is not None: parts.append(f"am={amount:.2f}")
    parts.append(f"tn={quote_plus('Repay to '+name)}")
    return "upi://pay?"+"&".join(parts)

# ==================== GOOGLE OAUTH ====================
GOOGLE_AUTH_URL="https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL="https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL="https://www.googleapis.com/oauth2/v2/userinfo"

@api_router.get("/auth/google")
async def auth_google():
    from urllib.parse import urlencode
    redirect_uri=f"{BACKEND_URL}/api/auth/google/callback"
    params={"client_id":GOOGLE_CLIENT_ID,"redirect_uri":redirect_uri,
            "response_type":"code","scope":"openid email profile",
            "access_type":"offline","prompt":"select_account"}
    return RedirectResponse(f"{GOOGLE_AUTH_URL}?{urlencode(params)}")

@api_router.get("/auth/google/callback")
async def auth_google_callback(request: Request, response: Response, code: str=None, error: str=None):
    if error or not code: return RedirectResponse(f"{FRONTEND_URL}/?error=auth_failed")
    redirect_uri=f"{BACKEND_URL}/api/auth/google/callback"
    async with httpx.AsyncClient(timeout=15) as hc:
        tr=await hc.post(GOOGLE_TOKEN_URL,data={"code":code,"client_id":GOOGLE_CLIENT_ID,
            "client_secret":GOOGLE_CLIENT_SECRET,"redirect_uri":redirect_uri,"grant_type":"authorization_code"})
    if tr.status_code!=200: return RedirectResponse(f"{FRONTEND_URL}/?error=token_failed")
    access_token=tr.json().get("access_token")
    async with httpx.AsyncClient(timeout=15) as hc:
        ur=await hc.get(GOOGLE_USERINFO_URL,headers={"Authorization":f"Bearer {access_token}"})
    if ur.status_code!=200: return RedirectResponse(f"{FRONTEND_URL}/?error=userinfo_failed")
    gu=ur.json()
    email=gu["email"]; name=gu.get("name",email.split("@")[0]); picture=gu.get("picture")
    existing=await db.users.find_one({"email":email},{"_id":0})
    if existing:
        user_id=existing["user_id"]
        await db.users.update_one({"user_id":user_id},{"$set":{"name":name,"picture":picture}})
    else:
        user_id=f"user_{uuid.uuid4().hex[:12]}"
        await db.users.insert_one({"user_id":user_id,"email":email,"name":name,"picture":picture,
            "created_at":datetime.now(timezone.utc).isoformat()})
    session_token=uuid.uuid4().hex+uuid.uuid4().hex
    expires_at=datetime.now(timezone.utc)+timedelta(days=7)
    await db.user_sessions.update_one({"user_id":user_id},
        {"$set":{"session_token":session_token,"user_id":user_id,
                 "expires_at":expires_at.isoformat(),"created_at":datetime.now(timezone.utc).isoformat()}},upsert=True)
    user=await db.users.find_one({"user_id":user_id},{"_id":0})
    onboarded=bool(user.get("upi_id"))
    resp=RedirectResponse(f"{FRONTEND_URL}/auth/callback#session_token={session_token}&onboarded={'true' if onboarded else 'false'}")
    resp.set_cookie(key="session_token",value=session_token,max_age=7*24*60*60,path="/",httponly=True,secure=True,samesite="none")
    return resp

@api_router.get("/auth/me",response_model=UserPublic)
async def auth_me(user: dict=Depends(current_user)): return to_public_user(user)

@api_router.post("/auth/logout")
async def auth_logout(request: Request, response: Response):
    token=await get_session_token(request)
    if token: await db.user_sessions.delete_one({"session_token":token})
    response.delete_cookie("session_token",path="/")
    return {"ok":True}

# ==================== USERS ====================
@api_router.post("/users/onboard",response_model=UserPublic)
async def users_onboard(body: OnboardIn, user: dict=Depends(current_user)):
    await db.users.update_one({"user_id":user["user_id"]},
        {"$set":{"name":body.name,"college":body.college,"branch":body.branch,"upi_id":body.upi_id}})
    return to_public_user(await db.users.find_one({"user_id":user["user_id"]},{"_id":0}))

@api_router.put("/users/profile",response_model=UserPublic)
async def users_profile(body: ProfileIn, user: dict=Depends(current_user)):
    update={k:v for k,v in body.model_dump().items() if v is not None}
    if update: await db.users.update_one({"user_id":user["user_id"]},{"$set":update})
    return to_public_user(await db.users.find_one({"user_id":user["user_id"]},{"_id":0}))

@api_router.put("/users/email-settings",response_model=UserPublic)
async def users_email_settings(body: EmailSettingsIn, user: dict=Depends(current_user)):
    await db.users.update_one({"user_id":user["user_id"]},
        {"$set":{"gmail_address":body.gmail_address,"gmail_app_password_enc":encrypt_str(body.gmail_app_password)}})
    return to_public_user(await db.users.find_one({"user_id":user["user_id"]},{"_id":0}))

@api_router.post("/users/email-test")
async def users_email_test(user: dict=Depends(current_user)):
    if not user.get("gmail_address") or not user.get("gmail_app_password_enc"):
        raise HTTPException(400,"Email not configured")
    pw=decrypt_str(user["gmail_app_password_enc"])
    msg=EmailMessage()
    msg["From"]=user["gmail_address"]; msg["To"]=user["gmail_address"]
    msg["Subject"]="Nexus 001 — Test Connection"
    msg.set_content("Your Gmail SMTP is connected.")
    try:
        await aiosmtplib.send(msg,hostname="smtp.gmail.com",port=465,
            username=user["gmail_address"],password=pw,use_tls=True,timeout=15)
    except Exception as e: raise HTTPException(400,f"SMTP failed: {e}")
    return {"ok":True}

@api_router.get("/users/qr")
async def users_qr(user: dict=Depends(current_user)):
    if not user.get("upi_id"): raise HTTPException(400,"UPI ID not set")
    link=upi_link(user["upi_id"],user.get("name","Lender"))
    return {"upi_link":link,"qr_base64":make_qr_b64(link)}

@api_router.delete("/users/me")
async def users_delete(user: dict=Depends(current_user)):
    uid=user["user_id"]
    await db.transactions.delete_many({"user_id":uid})
    await db.nudge_logs.delete_many({"user_id":uid})
    await db.user_sessions.delete_many({"user_id":uid})
    await db.users.delete_one({"user_id":uid})
    return {"ok":True}

@api_router.get("/users/export")
async def users_export(user: dict=Depends(current_user)):
    return {"transactions":await db.transactions.find({"user_id":user["user_id"]},{"_id":0}).to_list(10000)}

# ==================== TRANSACTIONS ====================
@api_router.get("/transactions",response_model=List[Transaction])
async def list_transactions(request: Request, status: Optional[str]=None, category: Optional[str]=None, user: dict=Depends(current_user)):
    q={"user_id":user["user_id"]}
    if status and status!="All": q["status"]=status
    if category and category!="All": q["category"]=category
    docs=await db.transactions.find(q,{"_id":0}).sort("created_at",-1).to_list(1000)
    today=datetime.now(timezone.utc).date()
    for d in docs:
        if d["status"]!="Paid":
            try:
                due=date.fromisoformat(d["due_date"])
                if due<today and d["status"]!="Overdue":
                    await db.transactions.update_one({"id":d["id"]},{"$set":{"status":"Overdue"}})
                    d["status"]="Overdue"
            except: pass
    return [Transaction(**d) for d in docs]

@api_router.post("/transactions",response_model=Transaction)
async def create_transaction(body: TransactionIn, user: dict=Depends(current_user)):
    count=await db.transactions.count_documents({"user_id":user["user_id"]})
    txn={"id":f"txn_{uuid.uuid4().hex[:12]}","user_id":user["user_id"],
        "borrower_name":body.borrower_name,"borrower_phone":body.borrower_phone,
        "borrower_email":body.borrower_email,"amount":float(body.amount),
        "due_date":body.due_date,"category":body.category,"status":compute_status(body.due_date),
        "nudge_count":0,"last_nudged_at":None,"last_nudge_channel":None,
        "avatar_color":AVATAR_PALETTE[count%len(AVATAR_PALETTE)],
        "initials":initials_of(body.borrower_name),"notes":body.notes,
        "created_at":datetime.now(timezone.utc).isoformat()}
    await db.transactions.insert_one(txn.copy())
    return Transaction(**txn)

@api_router.put("/transactions/{txn_id}",response_model=Transaction)
async def update_transaction(txn_id: str, body: TransactionUpdate, user: dict=Depends(current_user)):
    txn=await db.transactions.find_one({"id":txn_id,"user_id":user["user_id"]},{"_id":0})
    if not txn: raise HTTPException(404,"Not found")
    update={k:v for k,v in body.model_dump().items() if v is not None}
    if "borrower_name" in update: update["initials"]=initials_of(update["borrower_name"])
    if "due_date" in update or "status" in update:
        update["status"]=compute_status(update.get("due_date",txn["due_date"]),update.get("status",txn["status"]))
    await db.transactions.update_one({"id":txn_id},{"$set":update})
    return Transaction(**await db.transactions.find_one({"id":txn_id},{"_id":0}))

@api_router.delete("/transactions/{txn_id}")
async def delete_transaction(txn_id: str, user: dict=Depends(current_user)):
    res=await db.transactions.delete_one({"id":txn_id,"user_id":user["user_id"]})
    if res.deleted_count==0: raise HTTPException(404,"Not found")
    return {"ok":True}

@api_router.get("/transactions/{txn_id}/qr")
async def transaction_qr(txn_id: str, user: dict=Depends(current_user)):
    if not user.get("upi_id"): raise HTTPException(400,"Set your UPI ID in profile first")
    txn=await db.transactions.find_one({"id":txn_id,"user_id":user["user_id"]},{"_id":0})
    if not txn: raise HTTPException(404,"Not found")
    link=upi_link(user["upi_id"],user.get("name","Lender"),txn["amount"])
    return {"upi_link":link,"qr_base64":make_qr_b64(link),"amount":txn["amount"],"borrower_name":txn["borrower_name"]}

# ==================== NUDGES ====================
def days_overdue(due_date):
    try: d=date.fromisoformat(due_date)
    except: return 0
    return max(0,(datetime.now(timezone.utc).date()-d).days)

def build_wa_msg(level,name,amount,due_date,od,lender,upi):
    amt=f"₹{amount:,.0f}"
    if level<=0: return f"Hey {name} 👋\nJust a reminder — {amt} was due on {due_date}.\nPay here: 💰 {upi}\n— {lender}"
    if level==1: return f"Hi {name},\n{amt} has been pending for {od} days (due: {due_date}).\nPlease settle soon.\n💰 {upi}\n— {lender}"
    if level==2: return f"{name}, 3rd reminder.\n{amt} overdue {od} days. Delay affects your peer trust score.\n💰 {upi}\n— {lender} via Nexus 001"
    return f"⚠️ FINAL NOTICE — {name}\n{amt} unpaid for {od} days. Last reminder before High Risk flag.\n💰 {upi}\n— {lender}"

def build_email_subject(level,amount,od,lender):
    amt=f"₹{amount:,.0f}"
    subjects=[f"Friendly reminder about {amt} — {lender}",f"Payment reminder: {amt} pending for {od} days",
              f"Urgent: {amt} overdue — peer trust score affected",f"⚠️ FINAL NOTICE: {amt} unpaid — High Risk flag incoming"]
    return subjects[min(level,3)]

async def _record_nudge(user,txn,channel,level,message_text,status):
    await db.nudge_logs.insert_one({"id":f"log_{uuid.uuid4().hex[:12]}","user_id":user["user_id"],
        "transaction_id":txn["id"],"borrower_name":txn["borrower_name"],"channel":channel,
        "level":level,"message_text":message_text,"status":status,"sent_at":datetime.now(timezone.utc).isoformat()})
    if status=="sent":
        await db.transactions.update_one({"id":txn["id"]},
            {"$set":{"last_nudged_at":datetime.now(timezone.utc).isoformat(),"last_nudge_channel":channel},"$inc":{"nudge_count":1}})

@api_router.get("/nudge/preview/{txn_id}")
async def nudge_preview(txn_id: str, user: dict=Depends(current_user)):
    txn=await db.transactions.find_one({"id":txn_id,"user_id":user["user_id"]},{"_id":0})
    if not txn: raise HTTPException(404,"Not found")
    level=min(int(txn.get("nudge_count",0)),3)
    od=days_overdue(txn["due_date"])
    upi=user.get("upi_id","your-upi@bank"); lender=user.get("name","Lender")
    wa=build_wa_msg(level,txn["borrower_name"],txn["amount"],txn["due_date"],od,lender,upi)
    return {"level":level,"days_overdue":od,"whatsapp_message":wa,
            "email_subject":build_email_subject(level,txn["amount"],od,lender),"email_message":wa}

@api_router.post("/nudge/whatsapp/{txn_id}")
async def nudge_whatsapp(txn_id: str, body: NudgeSendIn, user: dict=Depends(current_user)):
    txn=await db.transactions.find_one({"id":txn_id,"user_id":user["user_id"]},{"_id":0})
    if not txn: raise HTTPException(404,"Not found")
    level=min(int(txn.get("nudge_count",0)),3)
    from urllib.parse import quote
    phone="".join(c for c in (txn.get("borrower_phone") or "") if c.isdigit())
    if len(phone)==10: phone="91"+phone
    wa_link=f"https://wa.me/{phone}?text={quote(body.message)}"
    await _record_nudge(user,txn,"whatsapp",level,body.message,"sent")
    return {"ok":True,"wa_link":wa_link,"level":level}

@api_router.post("/nudge/email/{txn_id}")
async def nudge_email(txn_id: str, body: NudgeSendIn, user: dict=Depends(current_user)):
    txn=await db.transactions.find_one({"id":txn_id,"user_id":user["user_id"]},{"_id":0})
    if not txn: raise HTTPException(404,"Not found")
    if not txn.get("borrower_email"): raise HTTPException(400,"Borrower has no email")
    if not user.get("gmail_address") or not user.get("gmail_app_password_enc"): raise HTTPException(400,"Configure Gmail in Settings first")
    level=min(int(txn.get("nudge_count",0)),3)
    od=days_overdue(txn["due_date"]); lender=user.get("name","Lender"); upi=user.get("upi_id","your-upi@bank")
    msg=EmailMessage()
    msg["From"]=f"{lender} <{user['gmail_address']}>"; msg["To"]=txn["borrower_email"]
    msg["Subject"]=build_email_subject(level,txn["amount"],od,lender)
    msg.set_content(body.message)
    try:
        pw=decrypt_str(user["gmail_app_password_enc"])
        await aiosmtplib.send(msg,hostname="smtp.gmail.com",port=465,username=user["gmail_address"],password=pw,use_tls=True,timeout=20)
        await _record_nudge(user,txn,"email",level,body.message,"sent")
        return {"ok":True,"level":level}
    except Exception as e:
        await _record_nudge(user,txn,"email",level,body.message,"failed")
        raise HTTPException(400,f"Email send failed: {e}")

@api_router.get("/nudge/log")
async def nudge_log(user: dict=Depends(current_user)):
    return await db.nudge_logs.find({"user_id":user["user_id"]},{"_id":0}).sort("sent_at",-1).to_list(500)

# ==================== INSIGHTS ====================
def _trust_score(transactions):
    score=100; today=datetime.now(timezone.utc).date()
    for t in transactions:
        if t["status"]=="Paid": score+=20
        else:
            try: score-=10*max(0,(today-date.fromisoformat(t["due_date"])).days)
            except: pass
    return max(0,min(100,score))

@api_router.get("/insights/summary")
async def insights_summary(user: dict=Depends(current_user)):
    docs=await db.transactions.find({"user_id":user["user_id"]},{"_id":0}).to_list(10000)
    total_lent=sum(t["amount"] for t in docs)
    total_recovered=sum(t["amount"] for t in docs if t["status"]=="Paid")
    pending=sum(t["amount"] for t in docs if t["status"]=="Pending")
    overdue=sum(t["amount"] for t in docs if t["status"]=="Overdue")
    recovery_rate=(total_recovered/total_lent*100.0) if total_lent>0 else 0.0
    by_cat={}
    for t in docs: by_cat[t["category"]]=by_cat.get(t["category"],0)+t["amount"]
    now=datetime.now(timezone.utc).replace(day=1); y,m=now.year,now.month
    bucket=[]
    for _ in range(6):
        bucket.append((y,m)); m-=1
        if m==0: m=12; y-=1
    bucket.reverse()
    trend=[]
    for yy,mm in bucket:
        dt=datetime(yy,mm,1); label=dt.strftime("%b"); ym=dt.strftime("%Y-%m")
        lent=sum(t["amount"] for t in docs if datetime.fromisoformat(t["created_at"]).strftime("%Y-%m")==ym)
        recovered=sum(t["amount"] for t in docs if t["status"]=="Paid" and datetime.fromisoformat(t["created_at"]).strftime("%Y-%m")==ym)
        trend.append({"month":label,"lent":lent,"recovered":recovered})
    by_b={}
    for t in docs:
        key=(t["borrower_name"],t.get("borrower_phone",""))
        b=by_b.setdefault(key,{"name":t["borrower_name"],"phone":t.get("borrower_phone",""),
            "owed":0.0,"txns":[],"avatar_color":t.get("avatar_color"),"initials":t.get("initials")})
        if t["status"]!="Paid": b["owed"]+=t["amount"]
        b["txns"].append(t)
    borrowers=[]
    for b in by_b.values():
        b["trust_score"]=_trust_score(b.pop("txns")); borrowers.append(b)
    borrowers.sort(key=lambda x:x["owed"],reverse=True)
    return {"total_lent":total_lent,"total_recovered":total_recovered,"pending":pending,"overdue":overdue,
            "recovery_rate":round(recovery_rate,1),"category_split":[{"name":k,"value":v} for k,v in by_cat.items()],
            "monthly_trend":trend,"top_borrowers":borrowers[:10]}

@api_router.get("/whatsapp/status")
async def whatsapp_status(user: dict=Depends(current_user)):
    return {"mode":"deeplink","active":True}

app.include_router(api_router)
app.add_middleware(CORSMiddleware,allow_credentials=True,allow_origin_regex=".*",allow_methods=["*"],allow_headers=["*"])

@app.on_event("shutdown")
async def shutdown_db_client(): client.close()
