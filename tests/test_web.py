"""Web-layer tests: order -> signed webhook -> job -> download, plus auth, idempotency, limits.
Offline: Stripe and the model are stubbed; webhook signatures are real."""
import hashlib, hmac, json, os, sys, tempfile, time, types
os.environ.update(DB_PATH=os.path.join(tempfile.mkdtemp(), "w.db"), ADMIN_TOKEN="tok-123",
                  STRIPE_WEBHOOK_SECRET="whsec_test", STRIPE_SECRET_KEY="sk_test_x", MODEL="stub",
                  OWNER_EMAIL="owner@example.com")
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from fastapi.testclient import TestClient
import stripe, core, main

CSV = b"title,description\nMug,Blue ceramic mug\nCup,Holds 350 ml\n"
REFUNDS = []


def stub_llm(prompt):
    if "Reply with PASS" in prompt:
        return "PASS"
    if "SOURCE: " not in prompt:
        return "OK"
    src = json.loads(prompt.split("SOURCE: ", 1)[1])
    return json.dumps({"title": src["title"], "description": src["description"] + " Great value."})


core.anthropic_llm = lambda: stub_llm
stripe.checkout.Session.create = lambda **kw: types.SimpleNamespace(url="https://checkout.stripe.test/x", **kw)
stripe.Refund.create = lambda **kw: REFUNDS.append(kw)
client = TestClient(main.app, follow_redirects=False)


def sign(payload: bytes):
    t = str(int(time.time()))
    sig = hmac.new(b"whsec_test", t.encode() + b"." + payload, hashlib.sha256).hexdigest()
    return {"stripe-signature": f"t={t},v1={sig}", "content-type": "application/json"}


def paid_event(jid, amount=1500, pi="pi_1"):
    return json.dumps({"id": "evt_1", "object": "event", "type": "checkout.session.completed",
                       "data": {"object": {"object": "checkout.session", "client_reference_id": jid,
                                           "payment_status": "paid", "amount_total": amount,
                                           "currency": "usd", "payment_intent": pi}}}).encode()


def place_order():
    r = client.post("/order", data={"email": "a@b.co"}, files={"file": ("l.csv", CSV, "text/csv")})
    assert r.status_code == 303, r.text
    with core.conn() as c:
        return c.execute("SELECT id, status FROM jobs ORDER BY created DESC LIMIT 1").fetchone()


def test_full_flow():
    with TestClient(main.app, follow_redirects=False) as c2:  # runs lifespan (model check)
        assert c2.get("/").status_code == 200 and "Pay $15" in c2.get("/").text
    j = place_order()
    assert j["status"] == "pending"
    assert client.get(f"/job/{j['id']}/download").status_code == 404  # unpaid: no output
    body = paid_event(j["id"])
    assert client.post("/webhook", content=body, headers=sign(body)).status_code == 200
    with core.conn() as c:
        row = c.execute("SELECT status, output FROM jobs WHERE id=?", (j["id"],)).fetchone()
    assert row["status"] == "done", row["status"]
    d = client.get(f"/job/{j['id']}/download")
    assert d.status_code == 200 and "Great value." in d.text and d.headers["cache-control"] == "no-store"
    # idempotent redelivery: no second run, no second revenue entry
    before = core.today_sum("revenue")
    assert client.post("/webhook", content=body, headers=sign(body)).status_code == 200
    assert core.today_sum("revenue") == before


def test_webhook_rejects_bad_signature_and_wrong_amount():
    j = place_order()
    body = paid_event(j["id"])
    bad = sign(body); bad["stripe-signature"] = bad["stripe-signature"][:-4] + "0000"
    assert client.post("/webhook", content=body, headers=bad).status_code == 400
    body2 = paid_event(j["id"], amount=100)
    assert client.post("/webhook", content=body2, headers=sign(body2)).status_code == 200
    with core.conn() as c:
        assert c.execute("SELECT status FROM jobs WHERE id=?", (j["id"],)).fetchone()[0] == "pending"


def test_order_validation():
    r = client.post("/order", data={"email": "nope"}, files={"file": ("l.csv", CSV, "text/csv")})
    assert r.status_code == 400
    r = client.post("/order", data={"email": "a@b.co"}, files={"file": ("l.csv", b"x,y\n1,2\n", "text/csv")})
    assert r.status_code == 400
    r = client.post("/order", data={"email": "a@b.co"}, files={"file": ("l.csv", b"a" * 300_000, "text/csv")})
    assert r.status_code == 400


def test_admin_auth_and_kill_switch():
    assert client.get("/admin/jobs").status_code == 403
    assert client.get("/admin/jobs", headers={"x-admin-token": "wrong"}).status_code == 403
    h = {"x-admin-token": "tok-123"}
    assert client.get("/admin/jobs", headers=h).status_code == 200
    assert client.post("/admin/intake?open=0", headers=h).json() == {"intake_open": False}
    r = client.post("/order", data={"email": "a@b.co"}, files={"file": ("l.csv", CSV, "text/csv")})
    assert r.status_code == 503
    assert "capacity" in client.get("/").text
    client.post("/admin/intake?open=1", headers=h)


def test_manual_review_release_and_refund():
    h = {"x-admin-token": "tok-123"}
    with core.conn() as c:
        for jid in ("n1", "n2"):
            c.execute("INSERT INTO jobs(id,email,status,input,output,payment,created) VALUES(?,?,?,?,?,?,?)",
                      (jid, "a@b.co", "needs_owner", "[]", "title,description,status\n", "pi_x", time.time()))
    assert client.post("/admin/job/n1/release", headers=h).status_code == 200
    assert client.post("/admin/job/n1/release", headers=h).status_code == 409  # not replayable
    assert client.post("/admin/job/n2/refund", headers=h).status_code == 200
    assert REFUNDS and REFUNDS[-1]["payment_intent"] == "pi_x"
    assert client.post("/admin/job/n2/refund", headers=h).status_code == 409


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
            print("ok", n)
