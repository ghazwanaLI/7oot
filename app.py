"""
نظام الحضور الذكي — OPDC
بدون أي مكتبات خارجية — Python فقط
"""
import os, json, base64, sqlite3, threading, hashlib
import urllib.request, urllib.error, urllib.parse
from datetime import datetime, timedelta, date
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from urllib.request import Request, urlopen
import time as _time, webbrowser

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "attendance.db")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
PORT = 5000

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS nvr_config (id INTEGER PRIMARY KEY, ip TEXT, port INTEGER DEFAULT 80, username TEXT, password TEXT);
    CREATE TABLE IF NOT EXISTS devices (id INTEGER PRIMARY KEY, name TEXT UNIQUE, ip TEXT, port INTEGER DEFAULT 80, username TEXT DEFAULT 'admin', password TEXT, channel_no INTEGER, location TEXT);
    CREATE TABLE IF NOT EXISTS employees (id INTEGER PRIMARY KEY, name TEXT, emp_no TEXT UNIQUE, fingerprint_no TEXT, emp_type TEXT DEFAULT 'regular', device_id INTEGER, regular_start TEXT DEFAULT '07:00', regular_end TEXT DEFAULT '13:45');
    CREATE TABLE IF NOT EXISTS shift_schedule (id INTEGER PRIMARY KEY, employee_id INTEGER, work_date TEXT, shift_name TEXT, UNIQUE(employee_id, work_date));
    CREATE TABLE IF NOT EXISTS attendance_log (id INTEGER PRIMARY KEY, employee_id INTEGER, employee_name TEXT, emp_no TEXT, work_date TEXT, shift_name TEXT, expected_time TEXT, actual_time TEXT, status TEXT DEFAULT 'pending', snapshot_b64 TEXT, ai_result TEXT, ai_notes TEXT, device_id INTEGER, channel_no INTEGER, created_at TEXT DEFAULT (datetime('now','localtime')), UNIQUE(employee_id, work_date, shift_name));
    """)
    conn.commit(); conn.close()

def http_req(url, username, password, method='GET', body=None):
    headers = {'Content-Type': 'application/json'} if body else {}
    # Basic auth
    creds = base64.b64encode(f"{username}:{password}".encode()).decode()
    headers['Authorization'] = f'Basic {creds}'
    req = Request(url, data=body, headers=headers, method=method)
    try:
        resp = urlopen(req, timeout=10)
        return resp.read(), 200
    except urllib.error.HTTPError as e:
        # Digest auth
        www = e.headers.get('WWW-Authenticate', '')
        if 'Digest' not in www:
            return None, e.code
        def pparse(h):
            d = {}
            for p in h.replace('Digest ','').split(','):
                p = p.strip()
                if '=' in p:
                    k,v = p.split('=',1)
                    d[k.strip()] = v.strip().strip('"')
            return d
        pr = pparse(www)
        realm = pr.get('realm',''); nonce = pr.get('nonce',''); qop = pr.get('qop','')
        ha1 = hashlib.md5(f"{username}:{realm}:{password}".encode()).hexdigest()
        uri = '/' + '/'.join(url.split('/')[3:])
        ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
        if qop:
            nc='00000001'; cnonce=hashlib.md5(str(_time.time()).encode()).hexdigest()[:8]
            rh = hashlib.md5(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}".encode()).hexdigest()
            auth = f'Digest username="{username}",realm="{realm}",nonce="{nonce}",uri="{uri}",qop={qop},nc={nc},cnonce="{cnonce}",response="{rh}"'
        else:
            rh = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
            auth = f'Digest username="{username}",realm="{realm}",nonce="{nonce}",uri="{uri}",response="{rh}"'
        headers['Authorization'] = auth
        req2 = Request(url, data=body, headers=headers, method=method)
        try:
            resp2 = urlopen(req2, timeout=10)
            return resp2.read(), 200
        except urllib.error.HTTPError as e2:
            return None, e2.code
    except Exception as e:
        return None, str(e)

def fetch_face_events(ip, port, username, password, start_dt, end_dt):
    url = f"http://{ip}:{port}/ISAPI/AccessControl/AcsEvent?format=json"
    body = json.dumps({"AcsEventCond":{"searchID":"1","searchResultPosition":0,"maxResults":1000,"major":5,"minor":75,"startTime":start_dt.strftime("%Y-%m-%dT%H:%M:%S+03:00"),"endTime":end_dt.strftime("%Y-%m-%dT%H:%M:%S+03:00")}}).encode()
    data, status = http_req(url, username, password, 'POST', body)
    if data:
        try:
            events = json.loads(data).get("AcsEvent",{}).get("InfoList",[])
            return [str(e.get("cardNo",e.get("employeeNoString",""))) for e in events], None
        except: pass
    return [], f"خطأ {status}"

def fetch_snapshot(channel, snap_time=None):
    conn = get_db(); nvr = conn.execute("SELECT * FROM nvr_config LIMIT 1").fetchone(); conn.close()
    if not nvr: return None, "NVR غير مُعدّ"
    url = f"http://{nvr['ip']}:{nvr['port']}/ISAPI/Streaming/channels/{channel}01/picture"
    if snap_time: url += f"?snapTime={snap_time}"
    data, status = http_req(url, nvr['username'], nvr['password'])
    if data and len(data) > 500: return base64.b64encode(data).decode(), None
    return None, f"خطأ {status}"

def analyze_image(b64, name, shift, time_str):
    if not ANTHROPIC_KEY: return {"present": True, "confidence": "low", "notes": "AI غير مفعّل"}
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

def check_employee(emp, work_date, shift_name, expected_time):
    conn = get_db()
    existing = conn.execute("SELECT id,status FROM attendance_log WHERE employee_id=? AND work_date=? AND shift_name=?",(emp['id'],work_date,shift_name)).fetchone()
    if existing and existing['status'] not in ('pending','error','no_snapshot'): conn.close(); return
    h,m = map(int, expected_time.split(':'))
    base_dt = datetime.strptime(work_date,"%Y-%m-%d").replace(hour=h,minute=m)
    dev = conn.execute("SELECT * FROM devices WHERE id=?",(emp['device_id'],)).fetchone() if emp.get('device_id') else None
    conn.close()
    fps, _ = fetch_face_events(dev['ip'],dev['port'],dev['username'],dev['password'],base_dt-timedelta(minutes=45),base_dt+timedelta(minutes=45)) if dev else ([],None)
    did_fp = str(emp['fingerprint_no']) in fps
    conn = get_db()
    if did_fp:
        status,img,ai_r,ai_n = 'ok',None,None,'بصّم ✅'
    else:
        img,err = fetch_snapshot(dev['channel_no'] if dev else 1, base_dt.strftime("%Y%m%dT%H%M%SZ"))
        if img:
            ai = analyze_image(img,emp['name'],shift_name,expected_time)
            status = 'present_no_fp' if ai['present'] else 'absent'
            ai_r,ai_n = ai['confidence'],ai['notes']
        else:
            status,ai_r,ai_n = 'no_snapshot',None,f"خطأ: {err}"
    if existing:
        conn.execute("UPDATE attendance_log SET status=?,snapshot_b64=?,ai_result=?,ai_notes=? WHERE id=?",(status,img,ai_r,ai_n,existing['id']))
    else:
        conn.execute("INSERT OR IGNORE INTO attendance_log (employee_id,employee_name,emp_no,work_date,shift_name,expected_time,status,snapshot_b64,ai_result,ai_notes,device_id,channel_no) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",(emp['id'],emp['name'],emp['emp_no'],work_date,shift_name,expected_time,status,img,ai_r,ai_n,emp.get('device_id'),dev['channel_no'] if dev else None))
    conn.commit(); conn.close()

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
            self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8'); self.send_header('Content-Length',len(content)); self.end_headers(); self.wfile.write(content); return
        conn=get_db()
        if path=='/api/nvr':
            r=conn.execute("SELECT id,ip,port,username FROM nvr_config LIMIT 1").fetchone(); conn.close(); self.send_json(dict(r) if r else {})
        elif path=='/api/devices':
            rows=[dict(r) for r in conn.execute("SELECT id,name,ip,port,username,channel_no,location FROM devices ORDER BY id").fetchall()]; conn.close(); self.send_json(rows)
        elif path=='/api/employees':
            rows=[dict(r) for r in conn.execute("SELECT e.*,d.name as device_name FROM employees e LEFT JOIN devices d ON e.device_id=d.id ORDER BY e.name").fetchall()]; conn.close(); self.send_json(rows)
        elif path=='/api/schedule':
            rows=[dict(r) for r in conn.execute("SELECT ss.*,e.name FROM shift_schedule ss JOIN employees e ON ss.employee_id=e.id WHERE ss.work_date=?",(g('date',str(date.today())),)).fetchall()]; conn.close(); self.send_json(rows)
        elif path=='/api/attendance':
            q="SELECT * FROM attendance_log WHERE work_date=?"; p=[g('date',str(date.today()))]
            if g('shift'): q+=" AND shift_name=?"; p.append(g('shift'))
            if g('status'): q+=" AND status=?"; p.append(g('status'))
            q+=" ORDER BY shift_name,employee_name"
            rows=[dict(r) for r in conn.execute(q,p).fetchall()]; conn.close(); self.send_json(rows)
        elif path=='/api/stats':
            r=conn.execute("SELECT COUNT(*) total,SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) ok,SUM(CASE WHEN status='present_no_fp' THEN 1 ELSE 0 END) present_no_fp,SUM(CASE WHEN status='absent' THEN 1 ELSE 0 END) absent,SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) pending FROM attendance_log WHERE work_date=?",(g('date',str(date.today())),)).fetchone(); conn.close(); self.send_json(dict(r))
        else:
            conn.close(); self.send_json({"error":"not found"},404)
    def do_POST(self):
        path=urlparse(self.path).path; d=self.get_body(); conn=get_db()
        if path=='/api/nvr':
            conn.execute("DELETE FROM nvr_config"); conn.execute("INSERT INTO nvr_config (ip,port,username,password) VALUES (?,?,?,?)",(d['ip'],d.get('port',80),d['username'],d['password'])); conn.commit(); conn.close(); self.send_json({"ok":True})
        elif path=='/api/scan':
            # مسح الشبكة للبحث عن أجهزة Hikvision
            import threading, queue
            username = d.get('username','admin')
            password = d.get('password','')
            subnets  = d.get('subnets', ['192.168.0','192.168.1','192.168.88'])
            found    = []
            q        = queue.Queue()

            def check_host(ip, user, pw):
                url = f"http://{ip}/ISAPI/System/deviceInfo"
                try:
                    data, status = http_req(url, user, pw)
                    if data and status == 200:
                        # نوع الجهاز
                        info = data.decode('utf-8', errors='ignore')
                        dev_type = 'camera'
                        if 'Face' in info or 'Access' in info or 'DS-K' in info:
                            dev_type = 'faceid'
                        elif 'NVR' in info or 'DVR' in info:
                            dev_type = 'nvr'
                        # صورة لو كاميرا أو NVR
                        preview = None
                        if dev_type in ('camera','nvr'):
                            snap_url = f"http://{ip}/ISAPI/Streaming/channels/101/picture"
                            snap_data, _ = http_req(snap_url, user, pw)
                            if snap_data and len(snap_data) > 500:
                                preview = base64.b64encode(snap_data).decode()
                        q.put({"ip": ip, "type": dev_type, "info": info[:200], "preview": preview})
                except: pass

            threads = []
            for subnet in subnets:
                for i in range(1, 255):
                    ip = f"{subnet}.{i}"
                    t = threading.Thread(target=check_host, args=(ip, username, password), daemon=True)
                    threads.append(t)
                    t.start()
                    if len(threads) % 50 == 0:
                        for th in threads[-50:]: th.join(timeout=3)

            for th in threads: th.join(timeout=3)
            while not q.empty(): found.append(q.get())
            conn.close()
            self.send_json({"ok": True, "devices": found, "total": len(found)})
            # جلب كل الكاميرات من NVR تلقائياً
            conn.close()
            nvr2=get_db().execute("SELECT * FROM nvr_config LIMIT 1").fetchone()
            get_db().close()
            if not nvr2: self.send_json({"ok":False,"error":"NVR غير مُعدّ"}); return
            cameras=[]
            for ch in range(1,33):
                url=f"http://{nvr2['ip']}:{nvr2['port']}/ISAPI/Streaming/channels/{ch}01/picture"
                data,status=http_req(url,nvr2['username'],nvr2['password'])
                if data and len(data)>500:
                    cameras.append({"channel":ch,"name":f"Camera {ch:02d}","preview":base64.b64encode(data).decode()})
            self.send_json({"ok":True,"cameras":cameras})

        elif path=='/api/nvr/test':
            conn.close()
            url=f"http://{d['ip']}:{d.get('port',80)}/ISAPI/Streaming/channels/101/picture"
            data,status=http_req(url,d['username'],d['password'])
            if data and len(data)>500: self.send_json({"ok":True,"snapshot":base64.b64encode(data).decode()})
            else: self.send_json({"ok":False,"error":f"تأكد من IP وكلمة المرور ({status})"})
        elif path=='/api/devices':
            if d.get('id'): conn.execute("UPDATE devices SET name=?,ip=?,port=?,username=?,password=?,channel_no=?,location=? WHERE id=?",(d['name'],d['ip'],d.get('port',80),d.get('username','admin'),d.get('password',''),d['channel_no'],d.get('location',''),d['id']))
            else: conn.execute("INSERT INTO devices (name,ip,port,username,password,channel_no,location) VALUES (?,?,?,?,?,?,?)",(d['name'],d['ip'],d.get('port',80),d.get('username','admin'),d.get('password',''),d['channel_no'],d.get('location','')))
            conn.commit(); conn.close(); self.send_json({"ok":True})
        elif path=='/api/employees':
            if d.get('id'): conn.execute("UPDATE employees SET name=?,emp_no=?,fingerprint_no=?,emp_type=?,device_id=?,regular_start=?,regular_end=? WHERE id=?",(d['name'],d['emp_no'],d['fingerprint_no'],d['emp_type'],d.get('device_id'),d.get('regular_start','07:00'),d.get('regular_end','13:45'),d['id']))
            else: conn.execute("INSERT INTO employees (name,emp_no,fingerprint_no,emp_type,device_id,regular_start,regular_end) VALUES (?,?,?,?,?,?,?)",(d['name'],d['emp_no'],d['fingerprint_no'],d['emp_type'],d.get('device_id'),d.get('regular_start','07:00'),d.get('regular_end','13:45')))
            conn.commit(); conn.close(); self.send_json({"ok":True})
        elif path=='/api/schedule':
            conn.execute("INSERT OR REPLACE INTO shift_schedule (employee_id,work_date,shift_name) VALUES (?,?,?)",(d['employee_id'],d['work_date'],d['shift_name'])); conn.commit(); conn.close(); self.send_json({"ok":True})
        elif path=='/api/schedule/bulk':
            for eid in d['employee_ids']: conn.execute("INSERT OR REPLACE INTO shift_schedule (employee_id,work_date,shift_name) VALUES (?,?,?)",(eid,d['work_date'],d['shift_name']))
            conn.commit(); conn.close(); self.send_json({"ok":True})
        elif path=='/api/rawlogs':
            dev=conn.execute("SELECT * FROM devices WHERE id=?",(d['device_id'],)).fetchone()
            if not dev: conn.close(); self.send_json({"ok":False,"error":"جهاز غير موجود"}); return
            # جلب كل الموظفين لمطابقة الأرقام
            emps={str(r['fingerprint_no']):r['name'] for r in conn.execute("SELECT name,fingerprint_no FROM employees WHERE device_id=?",(d['device_id'],)).fetchall()}
            conn.close()
            # نافذة اليوم كاملة
            work_date = d.get('date', str(date.today()))
            start_dt = datetime.strptime(work_date,"%Y-%m-%d").replace(hour=0,minute=0,second=0)
            end_dt   = start_dt.replace(hour=23,minute=59,second=59)
            logs, err = fetch_face_events(dev['ip'],dev['port'],dev['username'],dev['password'],start_dt,end_dt)
            if err and not logs:
                self.send_json({"ok":False,"error":f"تعذر الاتصال بالجهاز: {err}"}); return
            # إعادة كل السجلات مع الوقت
            url2 = f"http://{dev['ip']}:{dev['port']}/ISAPI/AccessControl/AcsEvent?format=json"
            body2 = json.dumps({"AcsEventCond":{"searchID":"1","searchResultPosition":0,"maxResults":1000,"major":5,"minor":75,"startTime":start_dt.strftime("%Y-%m-%dT%H:%M:%S+03:00"),"endTime":end_dt.strftime("%Y-%m-%dT%H:%M:%S+03:00")}}).encode()
            data2, _ = http_req(url2,dev['username'],dev['password'],'POST',body2)
            raw_logs = []
            if data2:
                try:
                    events = json.loads(data2).get("AcsEvent",{}).get("InfoList",[])
                    for e in events:
                        raw_logs.append({"card_no":str(e.get("cardNo",e.get("employeeNoString",""))),"time":e.get("time",""),"door_no":e.get("doorNo",1)})
                except: pass
            self.send_json({"ok":True,"logs":raw_logs,"employees":emps,"total":len(raw_logs)})
            work_date=d.get('date',str(date.today())); target_shift=d.get('shift','')
            shifts_map={'صباحية':'07:00','ظهيرة':'13:45','مسائية':'17:00','ليلية':'23:00'}
            regular=[dict(r) for r in conn.execute("SELECT * FROM employees WHERE emp_type='regular'").fetchall()]
            rotating=[dict(r) for r in conn.execute("SELECT e.*,ss.shift_name as sched_shift FROM employees e JOIN shift_schedule ss ON ss.employee_id=e.id WHERE e.emp_type='rotating' AND ss.work_date=?",(work_date,)).fetchall()]
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
        else:
            conn.close(); self.send_json({"error":"not found"},404)
    def do_DELETE(self):
        path=urlparse(self.path).path; conn=get_db()
        try:
            rid=int(path.split('/')[-1])
            if 'devices' in path: conn.execute("DELETE FROM devices WHERE id=?",(rid,))
            elif 'employees' in path: conn.execute("DELETE FROM employees WHERE id=?",(rid,))
            conn.commit()
        except: pass
        conn.close(); self.send_json({"ok":True})

if __name__=='__main__':
    init_db()
    print("="*50)
    print("  نظام الحضور الذكي — OPDC صلاح الدين")
    print(f"  http://localhost:{PORT}")
    print("="*50)
    def ob(): _time.sleep(1.5); webbrowser.open(f"http://localhost:{PORT}")
    threading.Thread(target=ob,daemon=True).start()
    server=HTTPServer(('0.0.0.0',PORT),Handler)
    print("✅ النظام يعمل — اضغط Ctrl+C للإيقاف\n")
    try: server.serve_forever()
    except KeyboardInterrupt: print("\n🛑 متوقف")
