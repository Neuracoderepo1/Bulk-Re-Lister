import json, os, sys, tempfile, time
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import core
core.init()


def fake_llm(bad=False):
    def llm(p):
        if "Reply with PASS" in p:
            return "PASS"
        src = json.loads(p.split("SOURCE: ", 1)[1])
        return json.dumps({"title": src["title"], "description": src["description"] + (" 99 pieces" if bad else " Great value.")})
    return llm


def add_job(jid, rows):
    with core.conn() as c:
        c.execute("INSERT INTO rl_jobs(id,email,status,input,created,updated,attempts) VALUES(?,?,?,?,?,?,0)", (jid, "a@b.c", "processing", json.dumps(rows), time.time(), time.time()))


def test_validation():
    assert core.validate_csv(b"title,description\nMug,Blue ceramic mug\n")[1] is None
    assert core.validate_csv(b"name,desc\nx,y\n")[1]
    assert core.validate_csv(b"title,description\n" + b"a,b\n" * 26)[1]


def test_rules():
    src = {"title": "Mug", "description": "Holds 350 ml"}
    assert core.check_rules(src, {"title": "Mug", "description": "Holds 350 ml"}) is None
    assert core.check_rules(src, {"title": "Mug", "description": "Holds 500 ml"}) == "invented number or spec"
    assert "banned" in core.check_rules(src, {"title": "Mug", "description": "guaranteed joy"})


def test_happy_path():
    add_job("j1", [{"title": "Mug", "description": "Blue mug"}] * 3)
    assert core.process_job("j1", fake_llm()) == "done"


def test_failure_refunds():
    add_job("j2", [{"title": "Mug", "description": "Blue mug"}] * 3)
    assert core.process_job("j2", fake_llm(bad=True), lambda j: True) == "refunded"
    add_job("j3", [{"title": "Mug", "description": "Blue mug"}])
    assert core.process_job("j3", fake_llm(bad=True), lambda j: False) == "needs_owner"


def test_hardening():
    src = {"title": "Mug", "description": "Holds tea"}
    assert core.check_rules(src, {"title": "Secure mug", "description": "Manicure safe"}) is None  # word boundaries
    assert "banned" in core.check_rules(src, {"title": "Mug", "description": "A cure for thirst"})
    assert "unsupported" in core.check_rules(src, {"title": "Mug", "description": "Made of leather"})
    assert core.check_rules(src, {"title": "Mug", "description": 5}) == "bad format"
    assert core.safe_cell("=SUM(A1)") == "'=SUM(A1)" and core.safe_cell("Mug") == "Mug"


def test_recovery_and_purge():
    add_job("r1", [{"title": "Mug", "description": "x"}])
    with core.conn() as c:
        c.execute("UPDATE rl_jobs SET updated=? WHERE id='r1'", (time.time() - 9999,))
    assert "r1" in core.recover_stale()
    with core.conn() as c:
        c.execute("UPDATE rl_jobs SET updated=?, attempts=3 WHERE id='r1'", (time.time() - 9999,))
    core.recover_stale()
    with core.conn() as c:
        assert c.execute("SELECT status FROM rl_jobs WHERE id='r1'").fetchone()[0] == "needs_owner"
        c.execute("UPDATE rl_jobs SET created=? WHERE id='j1'", (time.time() - 99 * 86400,))
    core.purge()
    with core.conn() as c:
        assert c.execute("SELECT status, output FROM rl_jobs WHERE id='j1'").fetchone()[:] == ("expired", None)


def test_reserve_cap():
    old = core.DAILY_SPEND_CAP
    core.DAILY_SPEND_CAP = core.today_sum("inference") + core.COST_PER_CALL * 1.5
    assert core.reserve("x") and not core.reserve("x")
    core.DAILY_SPEND_CAP = old


def test_reserve_cap_under_concurrency():
    import threading
    old = core.DAILY_SPEND_CAP
    core.DAILY_SPEND_CAP = core.today_sum("inference") + core.COST_PER_CALL * 5.5
    wins = []
    ts = [threading.Thread(target=lambda: wins.append(core.reserve("race"))) for _ in range(40)]
    [x.start() for x in ts]
    [x.join() for x in ts]
    core.DAILY_SPEND_CAP = old
    assert sum(wins) == 5, sum(wins)  # never overspends, never underspends


def test_recover_claims_atomically():
    add_job("c1", [{"title": "Mug", "description": "x"}])
    with core.conn() as c:
        c.execute("UPDATE rl_jobs SET updated=? WHERE id='c1'", (time.time() - 9999,))
    assert core.recover_stale().count("c1") == 1
    assert "c1" not in core.recover_stale()  # already claimed


def test_kill_switch_and_cap():
    assert core.intake_open()
    core.set_intake(False)
    assert not core.intake_open()
    core.set_intake(True)
    core.DAILY_SPEND_CAP = 0
    assert not core.intake_open()


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
            print("ok", n)
