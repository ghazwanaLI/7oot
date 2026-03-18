#!/usr/bin/env python3
"""نظام إدارة المحطة"""
import json, os, hashlib, uuid
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
from datetime import datetime, timedelta

PORT = int(os.environ.get("PORT", 8083))
DATABASE_URL = os.environ.get("DATABASE_URL", "")
USE_DB = bool(DATABASE_URL)
DB_FILE = "station_db.json"

def hash_pw(pw): return hashlib.sha256(pw.encode()).hexdigest()

# ── PostgreSQL ──
def get_conn():
    import pg8000, urllib.parse
    r = urllib.parse.urlparse(DATABASE_URL)
    return pg8000.connect(host=r.hostname, port=r.port or 5432,
        database=r.path.lstrip("/"), user=r.username, password=r.password, ssl_context=True)

def init_pg():
    conn = get_conn(); cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS station_store (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()
    cur.execute("SELECT value FROM station_store WHERE key='data'")
    if not cur.fetchone():
        cur.execute("INSERT INTO station_store VALUES ('data',%s)", [json.dumps(default_db(), ensure_ascii=False)])
        conn.commit()
    cur.close(); conn.close()

def pg_load():
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT value FROM station_store WHERE key='data'")
    row = cur.fetchone(); cur.close(); conn.close()
    return json.loads(row[0])

def pg_save(db):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("UPDATE station_store SET value=%s WHERE key='data'", [json.dumps(db, ensure_ascii=False)])
    conn.commit(); cur.close(); conn.close()

def load_db():
    if USE_DB: return pg_load()
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r", encoding="utf-8") as f: return json.load(f)
    db = default_db(); save_db(db); return db

def save_db(db):
    if USE_DB: pg_save(db); return
    with open(DB_FILE, "w", encoding="utf-8") as f: json.dump(db, f, ensure_ascii=False, indent=2)

PRODUCTS = [
    {"id": "premium", "name": "بنزين محسن", "color": "#ef4444", "price": 0},
    {"id": "regular", "name": "بنزين عادي", "color": "#f97316", "price": 0},
    {"id": "gasoil",  "name": "زيت الغاز",  "color": "#6366f1", "price": 0},
    {"id": "kerosene","name": "نفط أبيض",   "color": "#10b981", "price": 0},
]

def default_db():
    today = datetime.now().strftime("%Y-%m-%d")
    return {
        "station": {"name": "المحطة", "owner": "", "location": ""},
        "users": [{
            "id": 1, "fullname": "مدير النظام", "username": "admin",
            "password": hash_pw("admin123"), "role": "admin", "active": True
        }],
        "prices": {"premium": 750, "regular": 650, "gasoil": 500, "kerosene": 400, "gasoil_electronic": 500, "gasoil_private": 600, "gasoil_generator": 650, "gasoil_form9": 450, "gasoil_credit": 550},
        "pumps": [
            {"id": 1, "name": "بنزين محسن", "product": "premium", "active": True},
            {"id": 2, "name": "بنزين عادي", "product": "regular", "active": True},
            {"id": 3, "name": "زيت الغاز",  "product": "gasoil",  "active": True},
            {"id": 4, "name": "نفط أبيض",   "product": "kerosene","active": True},
        ],
        "daily_records": [],
        "tank_inventory": {"premium": 0, "regular": 0, "gasoil": 0, "kerosene": 0},
        "next_record_id": 1,
        "next_user_id": 2,
        "next_pump_id": 5,
    }

sessions = {}

class Handler(BaseHTTPRequestHandler):
    def log_message(self, f, *a): pass

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers(); self.wfile.write(body)

    def send_html(self, content):
        body = content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers(); self.wfile.write(body)

    def read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0: return {}
        data = b""
        remaining = length
        while remaining > 0:
            chunk = self.rfile.read(min(65536, remaining))
            if not chunk: break
            data += chunk; remaining -= len(chunk)
        return json.loads(data.decode("utf-8"))

    def get_user(self):
        token = self.headers.get("Authorization", "").replace("Bearer ", "").strip()
        uid = sessions.get(token)
        if not uid: return None
        return next((u for u in load_db()["users"] if u["id"] == uid), None)

    def require_auth(self):
        u = self.get_user()
        if not u: self.send_json({"error": "غير مصرح"}, 401)
        return u

    def do_OPTIONS(self):
        self.send_response(200)
        for h, v in [("Access-Control-Allow-Origin", "*"),
                     ("Access-Control-Allow-Methods", "GET,POST,PUT,DELETE,OPTIONS"),
                     ("Access-Control-Allow-Headers", "Content-Type,Authorization")]:
            self.send_header(h, v)
        self.end_headers()

    def do_GET(self):
        p = urlparse(self.path).path.rstrip("/")
        if p in ("", "/"):
            f = os.path.join(os.path.dirname(os.path.abspath(__file__)), "station_index.html")
            with open(f, "r", encoding="utf-8") as fh: self.send_html(fh.read()); return

        u = self.require_auth()
        if not u: return
        db = load_db()

        if p == "/api/me":
            self.send_json({"ok": True, "user": {k: v for k, v in u.items() if k != "password"}})
        elif p == "/api/config":
            self.send_json({"ok": True, "station": db["station"], "prices": db["prices"],
                            "pumps": db["pumps"], "products": PRODUCTS})
        elif p == "/api/tank":
            self.send_json({"ok": True, "inventory": db.get("tank_inventory", {})})
        elif p == "/api/records":
            records = db.get("daily_records", [])
            self.send_json({"ok": True, "records": records[-60:]})  # last 60 days
        elif p == "/api/today":
            today = datetime.now().strftime("%Y-%m-%d")
            rec = next((r for r in db.get("daily_records", []) if r["date"] == today), None)
            # Get last closing as today's opening
            yesterday_rec = None
            for r in reversed(db.get("daily_records", [])):
                if r["date"] < today:
                    yesterday_rec = r; break
            self.send_json({"ok": True, "today": rec, "yesterday": yesterday_rec})
        elif p == "/api/users":
            if u["role"] != "admin": self.send_json({"error": "غير مصرح"}, 403); return
            self.send_json({"ok": True, "users": [{k: v for k, v in x.items() if k != "password"} for x in db["users"]]})
        else:
            self.send_json({"error": "غير موجود"}, 404)

    def do_POST(self):
        p = urlparse(self.path).path.rstrip("/")

        if p == "/api/login":
            body = self.read_body(); db = load_db()
            user = next((u for u in db["users"] if u["username"] == body.get("username")
                        and u["password"] == hash_pw(body.get("password", ""))
                        and u.get("active", True)), None)
            if not user: self.send_json({"error": "اسم المستخدم أو كلمة المرور غير صحيحة"}, 401); return
            token = str(uuid.uuid4()); sessions[token] = user["id"]
            self.send_json({"ok": True, "token": token, "user": {k: v for k, v in user.items() if k != "password"}}); return

        if p == "/api/logout":
            token = self.headers.get("Authorization", "").replace("Bearer ", "").strip()
            sessions.pop(token, None); self.send_json({"ok": True}); return

        u = self.require_auth()
        if not u: return
        body = self.read_body(); db = load_db()

        if p == "/api/save-day":
            # Save today's record
            today = body.get("date", datetime.now().strftime("%Y-%m-%d"))
            records = db.get("daily_records", [])
            existing = next((i for i, r in enumerate(records) if r["date"] == today), None)
            record = {
                "id": body.get("id") or db.get("next_record_id", 1),
                "date": today,
                "pumps": body.get("pumps", []),
                "totals": body.get("totals", {}),
                "grand_total": body.get("grand_total", 0),
                "gasoil_dist": body.get("gasoil_dist", {}),
                "daily_deductions": body.get("daily_deductions", {}),
                "daily_net": body.get("daily_net", 0),
                "notes": body.get("notes", ""),
                "saved_by": u["fullname"],
                "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            }
            if existing is not None:
                records[existing] = record
            else:
                if "next_record_id" not in db: db["next_record_id"] = 1
                record["id"] = db["next_record_id"]; db["next_record_id"] += 1
                records.append(record)
            db["daily_records"] = records; save_db(db)
            self.send_json({"ok": True, "record": record})

        elif p == "/api/tank":
            db["tank_inventory"] = body.get("inventory", {}); save_db(db)
            self.send_json({"ok": True})

        elif p == "/api/prices":
            db["prices"] = body.get("prices", {}); save_db(db)
            self.send_json({"ok": True})

        elif p == "/api/pumps":
            if u["role"] != "admin": self.send_json({"error": "غير مصرح"}, 403); return
            if "next_pump_id" not in db: db["next_pump_id"] = 5
            pid = db["next_pump_id"]; db["next_pump_id"] += 1
            PRODUCT_NAMES = {"premium":"بنزين محسن","regular":"بنزين عادي","gasoil":"زيت الغاز","kerosene":"نفط أبيض"}
            product = body.get("product", "regular")
            pump = {"id": pid, "name": body.get("name", PRODUCT_NAMES.get(product, product)),
                    "product": body.get("product", "regular"), "active": True}
            db["pumps"].append(pump); save_db(db)
            self.send_json({"ok": True, "pump": pump})

        elif p == "/api/station":
            db["station"] = body.get("station", db["station"]); save_db(db)
            self.send_json({"ok": True})

        elif p == "/api/users":
            if u["role"] != "admin": self.send_json({"error": "غير مصرح"}, 403); return
            uid = db.get("next_user_id", 2); db["next_user_id"] = uid + 1
            nu = {"id": uid, "fullname": body.get("fullname", ""),
                  "username": body.get("username", ""),
                  "password": hash_pw(body.get("password", "")),
                  "role": body.get("role", "user"), "active": True}
            db["users"].append(nu); save_db(db)
            self.send_json({"ok": True})
        else:
            self.send_json({"error": "غير موجود"}, 404)

    def do_PUT(self):
        p = urlparse(self.path).path.rstrip("/")
        u = self.require_auth()
        if not u: return
        body = self.read_body(); db = load_db()

        if p.startswith("/api/pumps/"):
            if u["role"] != "admin": self.send_json({"error": "غير مصرح"}, 403); return
            pid = int(p.split("/")[-1])
            idx = next((i for i, p2 in enumerate(db["pumps"]) if p2["id"] == pid), None)
            if idx is None: self.send_json({"error": "غير موجود"}, 404); return
            for f in ["name", "product", "active"]:
                if f in body: db["pumps"][idx][f] = body[f]
            save_db(db); self.send_json({"ok": True})

        elif p.startswith("/api/users/"):
            if u["role"] != "admin": self.send_json({"error": "غير مصرح"}, 403); return
            uid2 = int(p.split("/")[-1])
            idx = next((i for i, x in enumerate(db["users"]) if x["id"] == uid2), None)
            if idx is None: self.send_json({"error": "غير موجود"}, 404); return
            for f in ["fullname", "username", "role", "active"]:
                if f in body: db["users"][idx][f] = body[f]
            if body.get("password"): db["users"][idx]["password"] = hash_pw(body["password"])
            save_db(db); self.send_json({"ok": True})
        else:
            self.send_json({"error": "غير موجود"}, 404)

    def do_DELETE(self):
        p = urlparse(self.path).path.rstrip("/")
        u = self.require_auth()
        if not u: return
        db = load_db()

        if p.startswith("/api/pumps/"):
            if u["role"] != "admin": self.send_json({"error": "غير مصرح"}, 403); return
            pid = int(p.split("/")[-1])
            db["pumps"] = [p2 for p2 in db["pumps"] if p2["id"] != pid]
            save_db(db); self.send_json({"ok": True})
        elif p.startswith("/api/records/"):
            if u["role"] != "admin": self.send_json({"error": "غير مصرح"}, 403); return
            rid = int(p.split("/")[-1])
            db["daily_records"] = [r for r in db.get("daily_records", []) if r["id"] != rid]
            save_db(db); self.send_json({"ok": True})
        else:
            self.send_json({"error": "غير موجود"}, 404)


if __name__ == "__main__":
    if USE_DB:
        print("⏳ تهيئة قاعدة البيانات...")
        init_pg()
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"\n  🏪  نظام إدارة المحطة")
    print(f"  ✅  السيرفر يعمل على المنفذ {PORT}")
    print(f"  🌐  http://localhost:{PORT}\n")
    try: server.serve_forever()
    except KeyboardInterrupt: server.shutdown()
