"""
نظام الحضور الذكي — OPDC صلاح الدين
Railway Version — يتصل بـ Agent على الشبكة المحلية
"""
import os, json, base64, threading
import urllib.request, urllib.error
from datetime import datetime, timedelta, date
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from urllib.request import Request, urlopen
import sqlite3, time as _time

PORT          = int(os.getenv('PORT', 5000))
ANTHROPIC_KEY = os.getenv('ANTHROPIC_API_KEY', '')
AGENT_URL     = os.getenv('AGENT_URL', '')        # رابط Agent على حاسبتك
AGENT_SECRET  = os.getenv('AGENT_SECRET', 'opdc-secret-2026')
DATABASE_URL  = os.getenv('DATABASE_URL', '')

# ─── قاعدة البيانات ───────────────────────────────────────
USE_PG = bool(DATABASE_URL)

if USE_PG:
    import psycopg2
    from psycopg2.extras import RealDictCursor
    def get_db():
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
        return conn
    PH = '%s'
else:
    DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'attendance.db')
    def get_db():
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn
    PH = '?'

def init_db():
    conn = get_db(); c = conn.cursor()
    ID   = 'SERIAL' if USE_PG else 'INTEGER'
    DT   = 'DATE'   if USE_PG else 'TEXT'
    NOW  = 'NOW()'  if USE_PG else "(datetime('now','localtime'))"
    stmts = [
        f"CREATE TABLE IF NOT EXISTS nvr_config (id {ID} PRIMARY KEY, ip TEXT, port INTEGER DEFAULT 80, username TEXT, password TEXT)",
        f"CREATE TABLE IF NOT EXISTS agent_config (id {ID} PRIMARY KEY, url TEXT, secret TEXT)",
        f"CREATE TABLE IF NOT EXISTS devices (id {ID} PRIMARY KEY, name TEXT UNIQUE, ip TEXT, port INTEGER DEFAULT 80, username TEXT DEFAULT 'admin', password TEXT, channel_no INTEGER, location TEXT)",
        f"CREATE TABLE IF NOT EXISTS employees (id {ID} PRIMARY KEY, name TEXT, emp_no TEXT UNIQUE, fingerprint_no TEXT, emp_type TEXT DEFAULT 'regular', device_id INTEGER, regular_start TEXT DEFAULT '07:00', regular_end TEXT DEFAULT '13:45')",
        f"CREATE TABLE IF NOT EXISTS shift_schedule (id {ID} PRIMARY KEY, employee_id INTEGER, work_date {DT}, shift_name TEXT, UNIQUE(employee_id, work_date))",
        f"CREATE TABLE IF NOT EXISTS attendance_log (id {ID} PRIMARY KEY, employee_id INTEGER, employee_name TEXT, emp_no TEXT, work_date {DT}, shift_name TEXT, expected_time TEXT, actual_time TEXT, status TEXT DEFAULT 'pending', snapshot_b64 TEXT, ai_result TEXT, ai_notes TEXT, device_id INTEGER, channel_no INTEGER, created_at TIMESTAMP DEFAULT {NOW}, UNIQUE(employee_id, work_date, shift_name))",
    ]
    for s in stmts:
        c.execute(s)
    conn.commit(); conn.close()

# ─── Agent اتصال ──────────────────────────────────────────
def agent_get(path, params={}):
    conn = get_db()
    ag = conn.execute(f"SELECT * FROM agent_config LIMIT 1").fetchone()
    conn.close()
    url = (dict(ag)['url'] if ag else AGENT_URL).rstrip('/')
    if not url: return None, "Agent غير مُعدّ"
    secret = dict(ag)['secret'] if ag else AGENT_SECRET
    qs = '&'.join(f"{k}={v}" for k,v in params.items())
    full = f"{url}{path}?{qs}" if qs else f"{url}{path}"
    req = Request(full, headers={'X-Agent-Secret': secret})
    try:
        resp = urlopen(req, timeout=15)
        return json.loads(resp.read()), None
    except Exception as e:
        return None, str(e)

def agent_post(path, data={}):
    conn = get_db()
    ag = conn.execute(f"SELECT * FROM agent_config LIMIT 1").fetchone()
    conn.close()
    url = (dict(ag)['url'] if ag else AGENT_URL).rstrip('/')
    if not url: return None, "Agent غير مُعدّ"
    secret = dict(ag)['secret'] if ag else AGENT_SECRET
    body = json.dumps(data).encode()
    req = Request(f"{url}{path}", data=body, headers={'X-Agent-Secret': secret, 'Content-Type': 'application/json'}, method='POST')
    try:
        resp = urlopen(req, timeout=30)
        return json.loads(resp.read()), None
    except Exception as e:
        return None, str(e)

# ─── Claude AI ────────────────────────────────────────────
def analyze_image(b64, name, shift, time_str):
    if not ANTHROPIC_KEY:
        return {"present": True, "confidence": "low", "notes": "AI غير مفعّل"}
    body = json.dumps({"model":"claude-opus-4-6","max_tokens":200,"messages":[{"role":"user","content":[{"type":"image","source":{"type":"base64","media_type":"image/jpeg","data":b64}},{"type":"text","text":f"صورة كاميرا. الموظف: {name}، وردية: {shift}، وقت: {time_str}.\nهل يظهر شخص؟ JSON فقط:\n{{\"present\":true/false,\"confidence\":\"high/medium/low\",\"notes\":\"ملاحظة\"}}"}]}]}).encode()
    req = Request("https://api.anthropic.com/v1/messages", data=body, headers={"x-api-key":ANTHROPIC_KEY,"anthropic-version":"2023-06-01","content-type":"application/json"}, method='POST')
    try:
        resp = urlopen(req, timeout=15)
        result = json.loads(resp.read())
        txt = result['content'][0]['text']
        s,e = txt.find('{'), txt.rfind('}')+1
        return json.loads(txt[s:e])
    except Exception as ex:
        return {"present": False, "confidence": "low", "notes": str(ex)}

# ─── فحص موظف ────────────────────────────────────────────
def check_employee(emp, work_date, shift_name, expected_time):
    conn = get_db()
    existing = conn.execute(f"SELECT id,status FROM attendance_log WHERE employee_id={PH} AND work_date={PH} AND shift_name={PH}", (emp['id'],work_date,shift_name)).fetchone()
    if existing and dict(existing)['status'] not in ('pending','error','no_snapshot'):
        conn.close(); return
    dev = conn.execute(f"SELECT * FROM devices WHERE id={PH}", (emp['device_id'],)).fetchone() if emp.get('device_id') else None
    conn.close()

    h,m = map(int, expected_time.split(':'))
    base_dt  = datetime.strptime(str(work_date)[:10],"%Y-%m-%d").replace(hour=h,minute=m)
    start_dt = base_dt - timedelta(minutes=45)
    end_dt   = base_dt + timedelta(minutes=45)

    # جلب سجلات Face ID عبر Agent
    fps = []
    if dev:
        r, err = agent_post('/check/device', {
            "device": dict(dev),
            "nvr": {},
            "employees": [dict(emp)],
            "shift_start": expected_time,
            "work_date": str(work_date)[:10]
        })
        if r and r.get('ok'):
            results = r.get('results', [])
            if results and results[0].get('status') == 'ok':
                fps = [str(emp['fingerprint_no'])]

    did_fp = str(emp['fingerprint_no']) in fps

    conn = get_db()
    if did_fp:
        status,img,ai_r,ai_n = 'ok',None,None,'بصّم ✅'
    else:
        # سحب صورة من NVR عبر Agent
        nvr_conn = get_db()
        nvr = nvr_conn.execute(f"SELECT * FROM nvr_config LIMIT 1").fetchone()
        nvr_conn.close()
        img = None; err = None
        if nvr and dev:
            snap_time = base_dt.strftime("%Y%m%dT%H%M%SZ")
            r2, err2 = agent_post('/nvr/snapshot', {
                "ip": dict(nvr)['ip'], "port": dict(nvr)['port'],
                "username": dict(nvr)['username'], "password": dict(nvr)['password'],
                "channel": dict(dev)['channel_no'], "snap_time": snap_time
            })
            if r2 and r2.get('ok'):
                img = r2.get('snapshot')
            else:
                err = err2 or 'خطأ الكاميرا'

        if img:
            ai = analyze_image(img, emp['name'], shift_name, expected_time)
            status = 'present_no_fp' if ai['present'] else 'absent'
            ai_r,ai_n = ai['confidence'], ai['notes']
        else:
            status,ai_r,ai_n = 'no_snapshot', None, f"خطأ: {err}"

    ex_id = dict(existing)['id'] if existing else None
    if ex_id:
        conn.execute(f"UPDATE attendance_log SET status={PH},snapshot_b64={PH},ai_result={PH},ai_notes={PH} WHERE id={PH}", (status,img,ai_r,ai_n,ex_id))
    else:
        conn.execute(f"INSERT INTO attendance_log (employee_id,employee_name,emp_no,work_date,shift_name,expected_time,status,snapshot_b64,ai_result,ai_notes,device_id,channel_no) VALUES ({','.join([PH]*12)})",
            (emp['id'],emp['name'],emp['emp_no'],str(work_date)[:10],shift_name,expected_time,status,img,ai_r,ai_n,emp.get('device_id'),dict(dev)['channel_no'] if dev else None))
    conn.commit(); conn.close()

# ─── HTTP Handler ──────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self,fmt,*a): pass
    def send_json(self,data,code=200):
        body=json.dumps(data,ensure_ascii=False,default=str).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Content-Length',len(body))
        self.send_header('Access-Control-Allow-Origin','*')
        self.end_headers(); self.wfile.write(body)
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin','*')
        self.send_header('Access-Control-Allow-Headers','Content-Type')
        self.send_header('Access-Control-Allow-Methods','GET,POST,DELETE,OPTIONS')
        self.end_headers()
    def get_body(self):
        n=int(self.headers.get('Content-Length',0))
        return json.loads(self.rfile.read(n)) if n else {}

    def do_GET(self):
        parsed=urlparse(self.path); path=parsed.path; params=parse_qs(parsed.query)
        g=lambda k,d='': params.get(k,[d])[0]
        if path in ('/','/index.html'):
            f=os.path.join(os.path.dirname(os.path.abspath(__file__)),'index.html')
            content=open(f,'rb').read()
            self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8')
            self.send_header('Content-Length',len(content)); self.end_headers(); self.wfile.write(content); return
        conn=get_db()
        if path=='/api/nvr':
            r=conn.execute(f"SELECT id,ip,port,username FROM nvr_config LIMIT 1").fetchone()
            conn.close(); self.send_json(dict(r) if r else {})
        elif path=='/api/agent':
            r=conn.execute(f"SELECT id,url FROM agent_config LIMIT 1").fetchone()
            conn.close(); self.send_json(dict(r) if r else {})
        elif path=='/api/agent/ping':
            conn.close()
            r,err = agent_get('/ping')
            self.send_json(r if r else {"ok":False,"error":err})
        elif path=='/api/devices':
            rows=[dict(r) for r in conn.execute("SELECT id,name,ip,port,username,channel_no,location FROM devices ORDER BY id").fetchall()]
            conn.close(); self.send_json(rows)
        elif path=='/api/employees':
            rows=[dict(r) for r in conn.execute("SELECT e.*,d.name as device_name FROM employees e LEFT JOIN devices d ON e.device_id=d.id ORDER BY e.name").fetchall()]
            conn.close(); self.send_json(rows)
        elif path=='/api/schedule':
            rows=[dict(r) for r in conn.execute(f"SELECT ss.*,e.name FROM shift_schedule ss JOIN employees e ON ss.employee_id=e.id WHERE ss.work_date={PH}",(g('date',str(date.today())),)).fetchall()]
            conn.close(); self.send_json(rows)
        elif path=='/api/attendance':
            q=f"SELECT id,employee_name,emp_no,work_date,shift_name,expected_time,status,ai_result,ai_notes,snapshot_b64 FROM attendance_log WHERE work_date={PH}"; p=[g('date',str(date.today()))]
            if g('shift'): q+=f" AND shift_name={PH}"; p.append(g('shift'))
            if g('status'): q+=f" AND status={PH}"; p.append(g('status'))
            q+=" ORDER BY shift_name,employee_name"
            rows=[dict(r) for r in conn.execute(q,p).fetchall()]
            conn.close(); self.send_json(rows)
        elif path=='/api/stats':
            r=conn.execute(f"SELECT COUNT(*) total,SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) ok,SUM(CASE WHEN status='present_no_fp' THEN 1 ELSE 0 END) present_no_fp,SUM(CASE WHEN status='absent' THEN 1 ELSE 0 END) absent,SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) pending FROM attendance_log WHERE work_date={PH}",(g('date',str(date.today())),)).fetchone()
            conn.close(); self.send_json(dict(r))
        else:
            conn.close(); self.send_json({"error":"not found"},404)

    def do_POST(self):
        path=urlparse(self.path).path; d=self.get_body(); conn=get_db()
        if path=='/api/nvr':
            conn.execute(f"DELETE FROM nvr_config")
            conn.execute(f"INSERT INTO nvr_config (ip,port,username,password) VALUES ({PH},{PH},{PH},{PH})",(d['ip'],d.get('port',80),d['username'],d['password']))
            conn.commit(); conn.close(); self.send_json({"ok":True})
        elif path=='/api/nvr/test':
            conn.close()
            r,err = agent_post('/nvr/snapshot',{"ip":d['ip'],"port":d.get('port',80),"username":d['username'],"password":d['password'],"channel":1})
            if r and r.get('ok'): self.send_json({"ok":True,"snapshot":r['snapshot']})
            else: self.send_json({"ok":False,"error":err or r.get('error','فشل الاتصال')})
        elif path=='/api/agent':
            conn.execute(f"DELETE FROM agent_config")
            conn.execute(f"INSERT INTO agent_config (url,secret) VALUES ({PH},{PH})",(d['url'],d.get('secret',AGENT_SECRET)))
            conn.commit(); conn.close(); self.send_json({"ok":True})
        elif path=='/api/agent/register':
            conn.execute(f"DELETE FROM agent_config")
            conn.execute(f"INSERT INTO agent_config (url,secret) VALUES ({PH},{PH})",(d.get('url',''),d.get('secret','')))
            conn.commit(); conn.close(); self.send_json({"ok":True})
        elif path=='/api/agent/heartbeat':
            conn.close(); self.send_json({"ok":True})
        elif path=='/api/scan':
            conn.close()
            r,err = agent_post('/scan',d)
            if r: self.send_json(r)
            else: self.send_json({"ok":False,"error":err})
        elif path=='/api/nvr/cameras':
            conn.close()
            r,err = agent_post('/nvr/cameras',d)
            if r: self.send_json(r)
            else: self.send_json({"ok":False,"error":err or "Agent غير متصل"})
        elif path=='/api/devices':
            if d.get('id'): conn.execute(f"UPDATE devices SET name={PH},ip={PH},port={PH},username={PH},password={PH},channel_no={PH},location={PH} WHERE id={PH}",(d['name'],d['ip'],d.get('port',80),d.get('username','admin'),d.get('password',''),d['channel_no'],d.get('location',''),d['id']))
            else: conn.execute(f"INSERT INTO devices (name,ip,port,username,password,channel_no,location) VALUES ({PH},{PH},{PH},{PH},{PH},{PH},{PH})",(d['name'],d['ip'],d.get('port',80),d.get('username','admin'),d.get('password',''),d['channel_no'],d.get('location','')))
            conn.commit(); conn.close(); self.send_json({"ok":True})
        elif path=='/api/employees':
            if d.get('id'): conn.execute(f"UPDATE employees SET name={PH},emp_no={PH},fingerprint_no={PH},emp_type={PH},device_id={PH},regular_start={PH},regular_end={PH} WHERE id={PH}",(d['name'],d['emp_no'],d['fingerprint_no'],d['emp_type'],d.get('device_id'),d.get('regular_start','07:00'),d.get('regular_end','13:45'),d['id']))
            else: conn.execute(f"INSERT INTO employees (name,emp_no,fingerprint_no,emp_type,device_id,regular_start,regular_end) VALUES ({PH},{PH},{PH},{PH},{PH},{PH},{PH})",(d['name'],d['emp_no'],d['fingerprint_no'],d['emp_type'],d.get('device_id'),d.get('regular_start','07:00'),d.get('regular_end','13:45')))
            conn.commit(); conn.close(); self.send_json({"ok":True})
        elif path=='/api/schedule':
            conn.execute(f"INSERT OR REPLACE INTO shift_schedule (employee_id,work_date,shift_name) VALUES ({PH},{PH},{PH})",(d['employee_id'],d['work_date'],d['shift_name']))
            conn.commit(); conn.close(); self.send_json({"ok":True})
        elif path=='/api/schedule/bulk':
            for eid in d['employee_ids']:
                conn.execute(f"INSERT OR REPLACE INTO shift_schedule (employee_id,work_date,shift_name) VALUES ({PH},{PH},{PH})",(eid,d['work_date'],d['shift_name']))
            conn.commit(); conn.close(); self.send_json({"ok":True})
        elif path=='/api/check':
            work_date=d.get('date',str(date.today())); target_shift=d.get('shift','')
            shifts_map={'صباحية':'07:00','ظهيرة':'13:45','مسائية':'17:00','ليلية':'23:00'}
            regular=[dict(r) for r in conn.execute("SELECT * FROM employees WHERE emp_type='regular'").fetchall()]
            rotating=[dict(r) for r in conn.execute(f"SELECT e.*,ss.shift_name as sched_shift FROM employees e JOIN shift_schedule ss ON ss.employee_id=e.id WHERE e.emp_type='rotating' AND ss.work_date={PH}",(work_date,)).fetchall()]
            conn.close()
            def bg():
                for emp in regular:
                    if not target_shift or target_shift=='صباحية': check_employee(emp,work_date,'صباحية',emp.get('regular_start','07:00'))
                    if not target_shift or target_shift=='انصراف': check_employee(emp,work_date,'انصراف',emp.get('regular_end','13:45'))
                for emp in rotating:
                    shift=emp.get('sched_shift','صباحية')
                    if target_shift and shift!=target_shift: continue
                    check_employee(emp,work_date,shift,shifts_map.get(shift,'07:00'))
            threading.Thread(target=bg,daemon=True).start()
            self.send_json({"ok":True})
        elif path=='/api/rawlogs':
            dev=conn.execute(f"SELECT * FROM devices WHERE id={PH}",(d['device_id'],)).fetchone()
            if not dev: conn.close(); self.send_json({"ok":False,"error":"جهاز غير موجود"}); return
            emps={str(r['fingerprint_no']):r['name'] for r in conn.execute(f"SELECT name,fingerprint_no FROM employees WHERE device_id={PH}",(d['device_id'],)).fetchall()}
            conn.close()
            r,err = agent_post('/device/events',{"ip":dict(dev)['ip'],"port":dict(dev)['port'],"username":dict(dev)['username'],"password":dict(dev)['password'],"start":f"{d.get('date',str(date.today()))}T00:00:00","end":f"{d.get('date',str(date.today()))}T23:59:59"})
            if r: self.send_json({"ok":True,"logs":r.get('events',[]),"employees":emps})
            else: self.send_json({"ok":False,"error":err})
        else:
            conn.close(); self.send_json({"error":"not found"},404)

    def do_DELETE(self):
        path=urlparse(self.path).path; conn=get_db()
        try:
            rid=int(path.split('/')[-1])
            if 'devices' in path: conn.execute(f"DELETE FROM devices WHERE id={PH}",(rid,))
            elif 'employees' in path: conn.execute(f"DELETE FROM employees WHERE id={PH}",(rid,))
            conn.commit()
        except: pass
        conn.close(); self.send_json({"ok":True})

if __name__=='__main__':
    init_db()
    print("="*50)
    print("  نظام الحضور الذكي — OPDC")
    print(f"  Port: {PORT}")
    print("="*50)
    server=HTTPServer(('0.0.0.0',PORT),Handler)
    print(f"✅ Running on port {PORT}")
    try: server.serve_forever()
    except KeyboardInterrupt: print("\n🛑 Stopped")
