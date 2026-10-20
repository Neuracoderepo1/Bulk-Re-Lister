"""Web layer: order form, Stripe checkout + webhook, delivery, review tools, worker loop."""
import hmac, json, os, smtplib, threading, time, uuid
from contextlib import asynccontextmanager
from email.message import EmailMessage

import stripe
from fastapi import BackgroundTasks, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

import core

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")
OWNER = os.getenv("OWNER_EMAIL")
PRICE_CENTS = 1500
HITS = {}
core.init()

PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Listing rewriter</title><style>
:root{--ink:#1d2a2a;--paper:#f3f6f4;--accent:#1f6f5c}
body{font:18px/1.55 Georgia,serif;color:var(--ink);background:var(--paper);max-width:34rem;margin:3rem auto;padding:0 1.2rem}
h1{font-size:2.4rem;line-height:1.1;margin:0 0 .6rem}
form{margin-top:2rem;padding-top:1.5rem;border-top:2px solid var(--ink)}
label{display:block;margin:1rem 0 .3rem;font-weight:bold}
input{font:inherit;width:100%;box-sizing:border-box;padding:.6rem;border:1px solid var(--ink);background:#fff}
button{font:inherit;margin-top:1.4rem;padding:.7rem 1.2rem;background:var(--accent);color:#fff;border:0;cursor:pointer}
button:focus-visible,input:focus-visible{outline:3px solid #e0a100}
small{display:block;margin-top:.5rem;opacity:.75}
</style><h1>Product listings rewritten, back in about ten minutes.</h1>
<p>Upload a CSV with <b>title</b> and <b>description</b> columns (up to 25 rows). We rewrite each listing using
only the facts you provide and check it automatically. Any row we can't verify comes back unchanged and is marked.
If more than a fifth of rows fail, you're refunded in full. $15 per batch. Please review before publishing.</p>
<form method=post action=/order enctype=multipart/form-data>
<label for=e>Email</label><input id=e name=email type=email maxlength=254 required>
<label for=f>CSV file</label><input id=f name=file type=file accept=.csv required>
<button>Pay $15 and start</button><small>Nothing runs until payment clears. Files are deleted after 14 days.</small></form>"""


def notify(to, subject, body, attach=None):
    host = os.getenv("SMTP_HOST")
    if not host or not to:
        print("EMAIL ->", to, subject)
        return
    try:
        m = EmailMessage()
        m["From"], m["To"], m["Subject"] = os.getenv("SMTP_FROM"), to, subject
        m.set_content(body)
        if attach:
            m.add_attachment(attach.encode(), maintype="text", subtype="csv", filename="listings.csv")
        with smtplib.SMTP(host, int(os.getenv("SMTP_PORT", "587")), timeout=30) as s:
            s.starttls()
            s.login(os.getenv("SMTP_USER"), os.getenv("SMTP_PASS"))
            s.send_message(m)
    except Exception as e:
        print("email failed", e)


def refund(job):
    try:  # idempotency key makes retries safe
        stripe.Refund.create(payment_intent=job["payment"], idempotency_key=f"refund-{job['id']}")
        return True
    except Exception as e:
        print("refund failed", e)
        return False


def get_job(jid):
    with core.conn() as c:
        return c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()


def run_job(jid):
    status = core.process_job(jid, core.anthropic_llm(), refund)
    job = get_job(jid)
    if status == "done":
        notify(job["email"], "Your listings are ready",
               f"Attached (also at {BASE_URL}/job/{jid}). Rows marked unchanged_needs_review "
               "could not be verified. Please review all output before publishing.", job["output"])
    elif status == "refunded":
        notify(job["email"], "Refund issued", "We couldn't meet our quality bar, so you were refunded in full.")
    elif status == "needs_owner":
        notify(OWNER, "Job needs review", f"{jid}: {job['failed']}/{job['total']} failed. Use /admin/jobs.")


def worker_loop():
    while True:
        try:
            core.purge()
            for jid in core.recover_stale():
                run_job(jid)
        except Exception as e:
            print("worker error", e)
        time.sleep(60)


@asynccontextmanager
async def lifespan(app):
    if not core.check_model():
        notify(OWNER, "Intake closed: model check failed", "Check MODEL and ANTHROPIC_API_KEY, then restart.")
    threading.Thread(target=worker_loop, daemon=True).start()
    yield


app = FastAPI(lifespan=lifespan)


def limited(ip, n=5, window=3600):
    now = time.time()
    h = [t for t in HITS.get(ip, []) if t > now - window]
    HITS[ip] = h
    if len(h) >= n:
        return True
    h.append(now)
    return False


def admin(tok):
    expected = os.getenv("ADMIN_TOKEN")
    if not expected or not hmac.compare_digest(tok or "", expected):
        raise HTTPException(403)


@app.get("/", response_class=HTMLResponse)
def home():
    return PAGE if core.intake_open() else "<p>We're at capacity. Please try again later.</p>"


@app.post("/order")
async def order(request: Request, email: str = Form(...), file: UploadFile = File(...)):
    if not core.intake_open():
        raise HTTPException(503, "At capacity. Try again later.")
    if limited(request.client.host if request.client else "?"):
        raise HTTPException(429, "Too many orders from this address. Try again later.")
    if "@" not in email or len(email) > 254:
        raise HTTPException(400, "Enter a valid email.")
    rows, err = core.validate_csv(await file.read(core.MAX_BYTES + 1))  # never reads more than the limit
    if err:
        raise HTTPException(400, err)
    jid = uuid.uuid4().hex
    s = stripe.checkout.Session.create(  # session first: a Stripe failure leaves no orphan job
        mode="payment", customer_email=email, client_reference_id=jid,
        line_items=[{"quantity": 1, "price_data": {"currency": "usd", "unit_amount": PRICE_CENTS,
                     "product_data": {"name": "Rewrite up to 25 listings"}}}],
        success_url=f"{BASE_URL}/job/{jid}", cancel_url=BASE_URL)
    with core.conn() as c:
        c.execute("INSERT INTO jobs(id,email,status,input,created) VALUES(?,?,?,?,?)",
                  (jid, email, "pending", json.dumps(rows), time.time()))
    return RedirectResponse(s.url, status_code=303)


@app.post("/webhook")
async def webhook(request: Request, bg: BackgroundTasks):
    raw = await request.body()
    try:  # verify the signature, then read the verified payload as plain JSON (SDK objects are not dicts)
        stripe.Webhook.construct_event(raw, request.headers.get("stripe-signature"),
                                       os.getenv("STRIPE_WEBHOOK_SECRET"))
        ev = json.loads(raw)
    except Exception:
        raise HTTPException(400, "bad signature")
    s = ev["data"]["object"]
    if ev["type"] == "checkout.session.completed" and s.get("payment_status") == "paid":
        jid = s.get("client_reference_id")
        if s.get("amount_total") != PRICE_CENTS or s.get("currency") != "usd":
            notify(OWNER, "Payment mismatch", f"Job {jid}: {s.get('amount_total')} {s.get('currency')}")
            return {"ok": True}
        with core.conn() as c:  # idempotent: only the first delivery flips pending -> processing
            n = c.execute("UPDATE jobs SET status='processing', payment=?, updated=? "
                          "WHERE id=? AND status='pending'", (s["payment_intent"], time.time(), jid)).rowcount
        if n:
            core.log("revenue", PRICE_CENTS / 100, jid)
            bg.add_task(run_job, jid)
    return {"ok": True}


@app.get("/job/{jid}", response_class=HTMLResponse)
def job_page(jid: str):
    j = get_job(jid)
    if not j or j["status"] == "expired":
        raise HTTPException(404, "Not found or expired.")
    if j["status"] == "done":
        return f"<p>Done. <a href=/job/{jid}/download>Download your CSV</a>. Please review before publishing.</p>"
    return f"<meta http-equiv=refresh content=10><p>Status: {j['status']}. This page refreshes itself.</p>"


@app.get("/job/{jid}/download")
def download(jid: str):
    j = get_job(jid)
    if not j or j["status"] != "done" or not j["output"]:
        raise HTTPException(404)
    return PlainTextResponse(j["output"], media_type="text/csv",
                             headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"})


@app.post("/admin/intake")
def admin_intake(open: int, x_admin_token: str = Header(None)):
    admin(x_admin_token)
    core.set_intake(bool(open))
    return {"intake_open": core.intake_open()}


@app.get("/admin/stats")
def stats(x_admin_token: str = Header(None)):
    admin(x_admin_token)
    return {k: core.today_sum(k) for k in ("revenue", "inference", "refund")}


@app.get("/admin/jobs")
def admin_jobs(status: str = "needs_owner", x_admin_token: str = Header(None)):
    admin(x_admin_token)
    with core.conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT id,email,status,failed,total,created FROM jobs WHERE status=? ORDER BY created", (status,))]


@app.get("/admin/job/{jid}", response_class=PlainTextResponse)
def admin_job(jid: str, x_admin_token: str = Header(None)):
    admin(x_admin_token)
    j = get_job(jid)
    if not j:
        raise HTTPException(404)
    return j["output"] or ""


@app.post("/admin/job/{jid}/release")
def admin_release(jid: str, x_admin_token: str = Header(None)):
    admin(x_admin_token)
    with core.conn() as c:
        n = c.execute("UPDATE jobs SET status='done' WHERE id=? AND status='needs_owner' AND output IS NOT NULL",
                      (jid,)).rowcount
    if not n:
        raise HTTPException(409, "Job is not awaiting review.")
    j = get_job(jid)
    notify(j["email"], "Your listings are ready", f"Download: {BASE_URL}/job/{jid}", j["output"])
    core.log("review", 0, jid, "released")
    return {"released": jid}


@app.post("/admin/job/{jid}/refund")
def admin_refund(jid: str, x_admin_token: str = Header(None)):
    admin(x_admin_token)
    j = get_job(jid)
    if not j or j["status"] != "needs_owner":
        raise HTTPException(409, "Job is not awaiting review.")
    if not refund(dict(j)):
        raise HTTPException(502, "Stripe refund failed.")
    with core.conn() as c:
        c.execute("UPDATE jobs SET status='refunded' WHERE id=?", (jid,))
    core.log("refund", 1, jid, "manual")
    notify(j["email"], "Refund issued", "Your order was refunded in full.")
    return {"refunded": jid}
