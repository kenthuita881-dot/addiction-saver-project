"""Addiction Saver backend (Flask + SQLite).

Run:
    pip install -r requirements.txt
    python app.py
Then open http://127.0.0.1:5000
"""
import datetime
import hmac
import os
import re
import math
import sqlite3
import time

from flask import Flask, abort, g, jsonify, request, send_from_directory

app = Flask(__name__)
DB_PATH = os.environ.get("DB_PATH", "addiction_saver.db")
PRICE_KES = 130
MIN_WORDS = 15                      # words needed in the daily reading reflection

# DEMO MODE: payments are marked as paid without charging anyone.
# Set to False once a real gateway is connected in /api/pay.
DEMO_PAYMENTS = True
PAY_METHODS = {"mpesa", "airtel", "card", "paypal", "googlepay", "bank"}
PHONE_METHODS = {"mpesa", "airtel"}

PAGES = {"index", "admin"}
MEETING_CATS = {"checkin", "substances", "digital", "family", "newcomers", "general"}
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")   # set this on the server to enable /admin

SCHEMA = """
CREATE TABLE IF NOT EXISTS counselors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL, specialty TEXT NOT NULL,
    email TEXT DEFAULT '', bio TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    counselor_id INTEGER NOT NULL, client_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('patient', 'counselor')),
    text TEXT NOT NULL, created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS trackers (
    client_id TEXT PRIMARY KEY, addiction TEXT NOT NULL,
    goal INTEGER NOT NULL, start_ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS payments (
    client_id TEXT PRIMARY KEY, phone TEXT, amount INTEGER, paid_at REAL, method TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS callbacks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL, phone TEXT NOT NULL, best_time TEXT DEFAULT '',
    message TEXT DEFAULT '', created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS meetings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL, cat TEXT NOT NULL DEFAULT 'general',
    daily INTEGER NOT NULL DEFAULT 0, dow INTEGER, time TEXT NOT NULL,
    mins INTEGER NOT NULL DEFAULT 60, host TEXT DEFAULT '', link TEXT DEFAULT '',
    desc TEXT DEFAULT '', created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS meeting_rsvps (
    client_id TEXT NOT NULL, meeting_id TEXT NOT NULL, created REAL NOT NULL,
    PRIMARY KEY (client_id, meeting_id)
);
CREATE TABLE IF NOT EXISTS reading_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id TEXT NOT NULL, book_id TEXT NOT NULL,
    pages INTEGER NOT NULL, days INTEGER NOT NULL, ppd INTEGER NOT NULL,
    start_date TEXT NOT NULL, created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS reading_checkins (
    client_id TEXT NOT NULL, date TEXT NOT NULL, book_id TEXT NOT NULL,
    pages_from INTEGER NOT NULL, pages_to INTEGER NOT NULL,
    note TEXT NOT NULL, created REAL NOT NULL,
    PRIMARY KEY (client_id, date)
);
"""


# ---------- database helpers ----------
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(SCHEMA)
        # upgrade databases created by an earlier version
        cols = {r[1] for r in conn.execute("PRAGMA table_info(payments)")}
        if "method" not in cols:
            conn.execute("ALTER TABLE payments ADD COLUMN method TEXT DEFAULT ''")


def client_id():
    """Anonymous visitor id sent by the browser. Replace with real logins later."""
    return (request.headers.get("X-Client-Id") or "").strip()[:64]


def is_paid(cid):
    row = get_db().execute("SELECT 1 FROM payments WHERE client_id = ?", (cid,)).fetchone()
    return row is not None


# ---------- pages ----------
@app.route("/")
def home():
    return send_from_directory(".", "index.html")


@app.route("/<name>.html")
def page(name):
    if name not in PAGES:
        abort(404)
    return send_from_directory(".", f"{name}.html")


@app.route("/style.css")
def css():
    return send_from_directory(".", "style.css")


@app.route("/script.js")
def js():
    return send_from_directory(".", "script.js")


# ---------- counselors ----------
@app.get("/api/counselors")
def list_counselors():
    rows = get_db().execute("SELECT * FROM counselors ORDER BY id").fetchall()
    return jsonify([dict(r) for r in rows])


@app.post("/api/admin/counselors")
def add_counselor():
    if (denied := admin_denied()):
        return denied
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    specialty = (data.get("specialty") or "").strip()
    if not name or not specialty:
        return jsonify(error="Name and specialty are required"), 400
    db = get_db()
    cur = db.execute(
        "INSERT INTO counselors (name, specialty, email, bio) VALUES (?, ?, ?, ?)",
        (name[:100], specialty[:100], (data.get("email") or "")[:120], (data.get("bio") or "")[:500]),
    )
    db.commit()
    return jsonify(id=cur.lastrowid), 201


@app.delete("/api/admin/counselors/<int:cid>")
def delete_counselor(cid):
    if (denied := admin_denied()):
        return denied
    db = get_db()
    db.execute("DELETE FROM counselors WHERE id = ?", (cid,))
    db.execute("DELETE FROM messages WHERE counselor_id = ?", (cid,))
    db.commit()
    return jsonify(ok=True)


# ---------- tracker ----------
@app.get("/api/tracker")
def get_tracker():
    row = get_db().execute("SELECT * FROM trackers WHERE client_id = ?", (client_id(),)).fetchone()
    return jsonify(dict(row) if row else None)


@app.post("/api/tracker")
def start_tracker():
    data = request.get_json(silent=True) or {}
    try:
        goal = max(1, min(3650, int(data.get("goal", 30))))
    except (TypeError, ValueError):
        return jsonify(error="Goal must be a number"), 400
    addiction = (data.get("addiction") or "").strip()[:100]
    if not addiction or not client_id():
        return jsonify(error="Addiction and client id are required"), 400
    db = get_db()
    db.execute(
        "INSERT OR REPLACE INTO trackers (client_id, addiction, goal, start_ts) VALUES (?, ?, ?, ?)",
        (client_id(), addiction, goal, time.time() * 1000),
    )
    db.commit()
    return jsonify(ok=True)


@app.post("/api/tracker/reset")
def reset_tracker():
    db = get_db()
    db.execute("UPDATE trackers SET start_ts = ? WHERE client_id = ?", (time.time() * 1000, client_id()))
    db.commit()
    return jsonify(ok=True)


# ---------- daily reading (the gate that unlocks the next day) ----------
def valid_date(s):
    try:
        return datetime.date.fromisoformat(str(s)) if len(str(s)) == 10 else None
    except ValueError:
        return None


def plan_math(pages, days):
    """pages / days -> pages per day (rounded up) and the real number of days needed."""
    ppd = math.ceil(pages / days)
    return ppd, math.ceil(pages / ppd)


def assignment_for(plans, date):
    """Which pages are assigned on this date? The latest plan that covers it wins."""
    for p in reversed(plans):
        start = datetime.date.fromisoformat(p["start_date"])
        j = (date - start).days + 1
        if 1 <= j <= p["days"]:
            return {"book_id": p["book_id"], "day": j,
                    "from": (j - 1) * p["ppd"] + 1, "to": min(p["pages"], j * p["ppd"])}
    return None


def load_plans(cid):
    rows = get_db().execute(
        "SELECT book_id, pages, days, ppd, start_date FROM reading_plans WHERE client_id = ? ORDER BY id", (cid,)
    ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/reading")
def get_reading():
    cid = client_id()
    checkins = get_db().execute(
        "SELECT date, book_id, pages_from AS 'from', pages_to AS 'to', note, created "
        "FROM reading_checkins WHERE client_id = ? ORDER BY date", (cid,)
    ).fetchall()
    return jsonify(plans=load_plans(cid), checkins=[dict(r) for r in checkins])


@app.post("/api/reading/plan")
def add_reading_plan():
    data = request.get_json(silent=True) or {}
    try:
        pages = int(data.get("pages"))
        days = int(data.get("days"))
    except (TypeError, ValueError):
        return jsonify(error="Pages and days must be numbers"), 400
    book_id = (data.get("book_id") or "").strip()[:60]
    if not book_id or not client_id() or not (20 <= pages <= 3000) or not (1 <= days <= 365):
        return jsonify(error="Please choose a book, its pages and the number of days"), 400
    if valid_date(data.get("start_date")) is None:
        return jsonify(error="Invalid start date"), 400
    ppd, real_days = plan_math(pages, days)
    db = get_db()
    db.execute(
        "INSERT INTO reading_plans (client_id, book_id, pages, days, ppd, start_date, created) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (client_id(), book_id, pages, real_days, ppd, data["start_date"], time.time()),
    )
    db.commit()
    return jsonify(ok=True, ppd=ppd, days=real_days), 201


@app.post("/api/reading/checkin")
def reading_checkin():
    cid = client_id()
    data = request.get_json(silent=True) or {}
    date = valid_date(data.get("date"))
    note = (data.get("note") or "").strip()[:800]
    if not cid or date is None:
        return jsonify(error="Invalid date"), 400
    if date > datetime.date.today() + datetime.timedelta(days=1):
        return jsonify(error="You cannot check in for a future day"), 400
    if len(note.split()) < MIN_WORDS:
        return jsonify(error=f"Please write at least {MIN_WORDS} words about what you read"), 400
    a = assignment_for(load_plans(cid), date)
    if a is None:
        return jsonify(error="No reading is assigned for that day"), 400
    db = get_db()
    db.execute(
        "INSERT OR REPLACE INTO reading_checkins (client_id, date, book_id, pages_from, pages_to, note, created) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (cid, data["date"], a["book_id"], a["from"], a["to"], note, time.time()),
    )
    db.commit()
    return jsonify(ok=True, pages_from=a["from"], pages_to=a["to"]), 201


# ---------- payment ----------
@app.get("/api/payment/status")
def payment_status():
    row = get_db().execute("SELECT method FROM payments WHERE client_id = ?", (client_id(),)).fetchone()
    return jsonify(paid=row is not None, method=(row["method"] if row else ""), price_kes=PRICE_KES)


@app.post("/api/pay")
def pay():
    data = request.get_json(silent=True) or {}
    method = (data.get("method") or "").strip()
    phone = (data.get("phone") or "").strip()
    if not client_id():
        return jsonify(error="Missing visitor id"), 400
    if method not in PAY_METHODS:
        return jsonify(error="Please choose a payment method"), 400
    if method in PHONE_METHODS and sum(ch.isdigit() for ch in phone) < 9:
        return jsonify(error="Enter a valid phone number"), 400

    if not DEMO_PAYMENTS:
        # REAL PAYMENTS: start the payment with your gateway here and return 202.
        #  - mpesa / airtel : Safaricom Daraja STK push (or Pesapal / Flutterwave / Paystack)
        #  - card           : create a payment with the gateway's hosted card fields. NEVER send raw card numbers here.
        #  - paypal         : create and capture the order with the PayPal Orders API
        #  - googlepay      : pass the Google Pay token to your gateway
        # Mark the visitor as paid ONLY inside the gateway's confirmed callback / webhook.
        return jsonify(error="Payment gateway not configured"), 501

    db = get_db()
    db.execute(
        "INSERT OR REPLACE INTO payments (client_id, phone, amount, paid_at, method) VALUES (?, ?, ?, ?, ?)",
        (client_id(), phone[:20], PRICE_KES, time.time(), method),
    )
    db.commit()
    return jsonify(paid=True, method=method)


# ---------- chat (only for paid visitors) ----------
@app.get("/api/messages")
def get_messages():
    if not is_paid(client_id()):
        return jsonify(error="Payment required"), 402
    counselor_id = request.args.get("counselor_id", type=int)
    rows = get_db().execute(
        "SELECT role, text, created FROM messages WHERE client_id = ? AND counselor_id = ? ORDER BY id",
        (client_id(), counselor_id),
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.post("/api/messages")
def send_message():
    if not is_paid(client_id()):
        return jsonify(error="Payment required"), 402
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()[:1000]
    role = data.get("role")
    counselor_id = data.get("counselor_id")
    if not text or role not in ("patient", "counselor") or not counselor_id:
        return jsonify(error="Invalid message"), 400
    db = get_db()
    db.execute(
        "INSERT INTO messages (counselor_id, client_id, role, text, created) VALUES (?, ?, ?, ?, ?)",
        (counselor_id, client_id(), role, text, time.time()),
    )
    db.commit()
    return jsonify(ok=True), 201


# ---------- call-back requests ----------
@app.post("/api/callback")
def callback_request():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    phone = (data.get("phone") or "").strip()
    if not name or not phone:
        return jsonify(error="Name and phone are required"), 400
    db = get_db()
    db.execute(
        "INSERT INTO callbacks (name, phone, best_time, message, created) VALUES (?, ?, ?, ?, ?)",
        (name[:100], phone[:25], (data.get("time") or "")[:20], (data.get("message") or "")[:500], time.time()),
    )
    db.commit()
    return jsonify(ok=True), 201



# ---------- meetings (added by the admin, listed publicly) ----------
def admin_denied():
    """Returns an error response unless the request carries the right admin token."""
    if not ADMIN_TOKEN:
        return jsonify(error="Admin disabled: set the ADMIN_TOKEN environment variable"), 503
    if not admin_ok():
        return jsonify(error="Unauthorized"), 401
    return None


@app.get("/api/meetings")
def list_meetings():
    rows = get_db().execute("SELECT * FROM meetings ORDER BY id").fetchall()
    return jsonify([{"id": str(r["id"]), "title": r["title"], "cat": r["cat"], "daily": bool(r["daily"]), "dow": r["dow"],
                     "time": r["time"], "mins": r["mins"], "host": r["host"], "link": r["link"], "desc": r["desc"]} for r in rows])


@app.post("/api/admin/meetings")
def add_meeting():
    if (denied := admin_denied()):
        return denied
    d = request.get_json(silent=True) or {}
    title = (d.get("title") or "").strip()[:100]
    cat = d.get("cat") if d.get("cat") in MEETING_CATS else "general"
    daily = bool(d.get("daily"))
    dow = d.get("dow")
    t = (d.get("time") or "").strip()
    mins = d.get("mins")
    link = (d.get("link") or "").strip()[:300]
    if not title:
        return jsonify(error="Title is required"), 400
    if not daily and not (isinstance(dow, int) and 0 <= dow <= 6):
        return jsonify(error="Choose a day of the week, or 'Every day'"), 400
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", t):
        return jsonify(error="Time must look like 19:00"), 400
    if not (isinstance(mins, int) and 5 <= mins <= 300):
        return jsonify(error="Length must be 5 to 300 minutes"), 400
    if link and not re.match(r"https?://", link):
        return jsonify(error="The join link must start with http:// or https://"), 400
    db = get_db()
    cur = db.execute(
        "INSERT INTO meetings (title, cat, daily, dow, time, mins, host, link, desc, created) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (title, cat, int(daily), None if daily else dow, t, mins, (d.get("host") or "")[:100], link, (d.get("desc") or "")[:400], time.time()))
    db.commit()
    return jsonify(id=cur.lastrowid), 201


@app.delete("/api/admin/meetings/<int:mid>")
def delete_meeting(mid):
    if (denied := admin_denied()):
        return denied
    db = get_db()
    db.execute("DELETE FROM meetings WHERE id = ?", (mid,))
    db.execute("DELETE FROM meeting_rsvps WHERE meeting_id = ?", (str(mid),))
    db.commit()
    return jsonify(ok=True)


# ---------- RSVP ----------
@app.get("/api/meetings/rsvp")
def rsvp_list():
    rows = get_db().execute("SELECT meeting_id FROM meeting_rsvps WHERE client_id = ?", (client_id(),)).fetchall()
    return jsonify(ids=[r[0] for r in rows])


@app.post("/api/meetings/rsvp")
def rsvp_set():
    data = request.get_json(silent=True) or {}
    mid = (data.get("meeting_id") or "").strip()
    if not client_id():
        return jsonify(error="Missing visitor id"), 400
    db = get_db()
    if not db.execute("SELECT 1 FROM meetings WHERE id = ?", (mid,)).fetchone():
        return jsonify(error="Unknown meeting"), 400
    if data.get("going"):
        db.execute("INSERT OR IGNORE INTO meeting_rsvps (client_id, meeting_id, created) VALUES (?, ?, ?)", (client_id(), mid, time.time()))
    else:
        db.execute("DELETE FROM meeting_rsvps WHERE client_id = ? AND meeting_id = ?", (client_id(), mid))
    db.commit()
    rows = db.execute("SELECT meeting_id FROM meeting_rsvps WHERE client_id = ?", (client_id(),)).fetchall()
    return jsonify(ok=True, ids=[r[0] for r in rows])


# ---------- admin: see and track your clients ----------
def admin_ok():
    sent = request.headers.get("X-Admin-Token", "")
    return bool(ADMIN_TOKEN) and hmac.compare_digest(sent.encode(), ADMIN_TOKEN.encode())


@app.get("/api/admin/summary")
def admin_summary():
    if not ADMIN_TOKEN:
        return jsonify(error="Admin disabled: set the ADMIN_TOKEN environment variable"), 503
    if not admin_ok():
        return jsonify(error="Unauthorized"), 401
    db = get_db()
    ids = {r[0] for t in ("trackers", "payments", "messages", "reading_checkins", "reading_plans", "meeting_rsvps")
           for r in db.execute(f"SELECT DISTINCT client_id FROM {t}")}
    clients = []
    for cid in ids:
        tr = db.execute("SELECT * FROM trackers WHERE client_id=?", (cid,)).fetchone()
        pay = db.execute("SELECT * FROM payments WHERE client_id=?", (cid,)).fetchone()
        reads = db.execute("SELECT COUNT(*), MAX(created) FROM reading_checkins WHERE client_id=?", (cid,)).fetchone()
        msgs = db.execute("SELECT COUNT(*), MAX(created) FROM messages WHERE client_id=?", (cid,)).fetchone()
        rs = db.execute("SELECT COUNT(*) FROM meeting_rsvps WHERE client_id=?", (cid,)).fetchone()[0]
        start = tr["start_ts"] if tr else None
        if start and start > 1e11:      # the browser stores milliseconds
            start /= 1000
        last = max([x for x in (reads[1], msgs[1], pay["paid_at"] if pay else None, start) if x] or [0])
        clients.append({
            "client": cid[:8], "paid": bool(pay), "method": pay["method"] if pay else "",
            "phone": pay["phone"] if pay else "", "addiction": tr["addiction"] if tr else "",
            "goal": tr["goal"] if tr else "", "start_ts": start,
            "readings": reads[0], "messages": msgs[0], "rsvps": rs, "last_active": last or None,
        })
    clients.sort(key=lambda c: c["last_active"] or 0, reverse=True)
    calls = [dict(r) for r in db.execute("SELECT name, phone, best_time, message, created FROM callbacks ORDER BY id DESC")]
    return jsonify(
        totals={"clients": len(clients), "paid": sum(c["paid"] for c in clients),
                "revenue_kes": sum(c["paid"] for c in clients) * PRICE_KES,
                "trackers": db.execute("SELECT COUNT(*) FROM trackers").fetchone()[0],
                "readings": db.execute("SELECT COUNT(*) FROM reading_checkins").fetchone()[0],
                "callbacks": len(calls),
                "rsvps": db.execute("SELECT COUNT(*) FROM meeting_rsvps").fetchone()[0]},
        clients=clients, callbacks=calls)


init_db()   # also runs under gunicorn

if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1", port=int(os.environ.get("PORT", 5000)))
