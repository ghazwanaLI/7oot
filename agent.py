"""
Agent محلي — OPDC نظام الحضور الذكي
يشتغل على حاسبتك ويربطها بالنظام أونلاين
"""
import os,sys,json,time,base64,hashlib,threading,queue,logging
import urllib.request,urllib.error
from datetime import datetime,timedelta
from http.server import HTTPServer,BaseHTTPRequestHandler
from urllib.parse import urlparse
from urllib.request import Request,urlopen

AGENT_PORT=8080
AGENT_SECRET=os.getenv('AGENT_SECRET','opdc-secret-2026')
RAILWAY_URL=os.getenv('RAILWAY_URL','')

logging.basicConfig(level=logging.INFO,format='%(asctime)s [Agent] %(message)s',
    handlers=[logging.FileHandler('agent.log',encoding='utf-8'),logging.StreamHandler(sys.stdout)])
log=logging.getLogger(__name__)

def http_req(url,username,password,method='GET',body=None):
    headers={'Content-Type':'application/json'} if body else {}
    creds=base64.b64encode(f"{username}:{password}".encode()).decode()
    headers['Authorization']=f'Basic {creds}'
    req=Request(url,data=body,headers=headers,method=method)
    try:
        resp=urlopen(req,timeout=10); return resp.read(),200
    except urllib.error.HTTPError as e:
        www=e.headers.get('WWW-Authenticate','')
        if 'Digest' not in www: return None,e.code
        def pp(h):
            d={}
            for p in h.replace('Digest ','').split(','):
                p=p.strip()
                if '=' in p:
                    k,v=p.split('=',1); d[k.strip()]=v.strip().strip('"')
            return d
        pr=pp(www); realm=pr.get('realm',''); nonce=pr.get('nonce',''); qop=pr.get('qop','')
        ha1=hashlib.md5(f"{username}:{realm}:{password}".encode()).hexdigest()
        uri='/'+'/'.join(url.split('/')[3:])
        ha2=hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
        if qop:
            nc='00000001'; cnonce=hashlib.md5(str(time.time()).encode()).hexdigest()[:8]
            rh=hashlib.md5(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}".encode()).hexdigest()
            auth=f'Digest username="{username}",realm="{realm}",nonce="{nonce}",uri="{uri}",qop={qop},nc={nc},cnonce="{cnonce}",response="{rh}"'
        else:
            rh=hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
            auth=f'Digest username="{username}",realm="{realm}",nonce="{nonce}",uri="{uri}",response="{rh}"'
        headers['Authorization']=auth
        req2=Request(url,data=body,headers=headers,method=method)
        try: resp2=urlopen(req2,timeout=10); return resp2.read(),200
        except urllib.error.HTTPError as e2: return None,e2.code
    except Exception as e: return None,str(e)

def fetch_face_events(ip,port,username,password,start_dt,end_dt):
    url=f"http://{ip}:{port}/ISAPI/AccessControl/AcsEvent?format=json"
    body=json.dumps({"AcsEventCond":{"searchID":"1","searchResultPosition":0,"maxResults":1000,"major":5,"minor":75,"startTime":start_dt.strftime("%Y-%m-%dT%H:%M:%S+03:00"),"endTime":end_dt.strftime("%Y-%m-%dT%H:%M:%S+03:00")}}).encode()
    data,status=http_req(url,username,password,'POST',body)
    if data:
        try:
            events=json.loads(data).get("AcsEvent",{}).get("InfoList",[])
            return [{"card_no":str(e.get("cardNo",e.get("employeeNoString",""))),"time":e.get("time",""),"door_no":e.get("doorNo",1)} for e in events],None
        except: pass
    return [],f"خطأ {status}"

def fetch_snapshot(ip,port,username,password,channel,snap_time=None):
    url=f"http://{ip}:{port}/ISAPI/Streaming/channels/{channel}01/picture"
    if snap_time: url+=f"?snapTime={snap_time}"
    data,status=http_req(url,username,password)
    if data and len(data)>500: return base64.b64encode(data).decode(),None
    return None,f"خطأ {status}"

def get_nvr_cameras(ip,port,username,password):
    cameras=[]
    for ch in range(1,33):
        data,status=fetch_snapshot(ip,port,username,password,ch)
        if data: cameras.append({"channel":ch,"name":f"Camera {ch:02d}","preview":data})
    return cameras

def scan_network(subnets,username,password):
    found=[]; q=queue.Queue()
    def check(ip):
        data,status=http_req(f"http://{ip}/ISAPI/System/deviceInfo",username,password)
        if data and status==200:
            info=data.decode('utf-8',errors='ignore')
            dev_type='faceid' if any(x in info for x in ['Face','Access','DS-K']) else 'nvr' if any(x in info for x in ['NVR','DVR']) else 'camera'
            preview=None
            if dev_type in ('camera','nvr'):
                snap,_=fetch_snapshot(ip,80,username,password,1)
                preview=snap
            q.put({"ip":ip,"type":dev_type,"preview":preview})
    threads=[]
    for subnet in subnets:
        for i in range(1,255):
            t=threading.Thread(target=check,args=(f"{subnet}.{i}",),daemon=True)
            threads.append(t); t.start()
            if len(threads)%100==0:
                for th in threads[-100:]: th.join(timeout=2)
    for th in threads: th.join(timeout=2)
    while not q.empty(): found.append(q.get())
    return found

class AgentHandler(BaseHTTPRequestHandler):
    def log_message(self,fmt,*a): pass
    def check_auth(self): return self.headers.get('X-Agent-Secret','')==AGENT_SECRET
    def send_json(self,data,code=200):
        body=json.dumps(data,ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Content-Length',len(body))
        self.send_header('Access-Control-Allow-Origin','*')
        self.end_headers(); self.wfile.write(body)
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin','*')
        self.send_header('Access-Control-Allow-Headers','X-Agent-Secret,Content-Type')
        self.end_headers()
    def get_body(self):
        n=int(self.headers.get('Content-Length',0))
        return json.loads(self.rfile.read(n)) if n else {}
    def do_GET(self):
        path=urlparse(self.path).path
        if path=='/ping': self.send_json({"ok":True,"agent":"OPDC-Agent","time":datetime.now().isoformat()}); return
        if not self.check_auth(): self.send_json({"error":"غير مصرح"},403); return
        self.send_json({"error":"not found"},404)
    def do_POST(self):
        if not self.check_auth(): self.send_json({"error":"غير مصرح"},403); return
        path=urlparse(self.path).path; d=self.get_body()
        if path=='/nvr/snapshot':
            img,err=fetch_snapshot(d['ip'],d.get('port',80),d['username'],d['password'],d.get('channel',1),d.get('snap_time'))
            self.send_json({"ok":bool(img),"snapshot":img,"error":err})
        elif path=='/nvr/cameras':
            nvr=d.get('nvr',d)
            cameras=get_nvr_cameras(nvr['ip'],nvr.get('port',80),nvr['username'],nvr['password'])
            self.send_json({"ok":True,"cameras":cameras})
        elif path=='/device/events':
            start_dt=datetime.fromisoformat(d.get('start',datetime.now().strftime('%Y-%m-%dT00:00:00')))
            end_dt=datetime.fromisoformat(d.get('end',datetime.now().strftime('%Y-%m-%dT23:59:59')))
            events,err=fetch_face_events(d['ip'],d.get('port',80),d['username'],d['password'],start_dt,end_dt)
            self.send_json({"ok":True,"events":events,"count":len(events),"error":err})
        elif path=='/check/device':
            dev=d.get('device',{}); employees=d.get('employees',[])
            shift_start=d.get('shift_start','07:00'); work_date=d.get('work_date',str(datetime.today().date()))
            h,m=map(int,shift_start.split(':'))
            base_dt=datetime.strptime(work_date,"%Y-%m-%d").replace(hour=h,minute=m)
            events,_=fetch_face_events(dev['ip'],dev.get('port',80),dev.get('username','admin'),dev.get('password',''),base_dt-timedelta(minutes=45),base_dt+timedelta(minutes=45))
            present={e['card_no'] for e in events}
            results=[]
            for emp in employees:
                fp=str(emp.get('fingerprint_no',''))
                if fp in present: results.append({"emp_id":emp['id'],"status":"ok"})
                else:
                    nvr=d.get('nvr',{})
                    img=None
                    if nvr.get('ip'): img,_=fetch_snapshot(nvr['ip'],nvr.get('port',80),nvr.get('username','admin'),nvr.get('password',''),dev.get('channel_no',1),base_dt.strftime("%Y%m%dT%H%M%SZ"))
                    results.append({"emp_id":emp['id'],"status":"no_fingerprint","snapshot":img})
            self.send_json({"ok":True,"results":results})
        elif path=='/scan':
            found=scan_network(d.get('subnets',['192.168.0','192.168.88']),d.get('username','admin'),d.get('password',''))
            self.send_json({"ok":True,"devices":found,"total":len(found)})
        else:
            self.send_json({"error":"not found"},404)

def heartbeat():
    while True:
        if RAILWAY_URL:
            try:
                req=Request(f"{RAILWAY_URL}/api/agent/heartbeat",data=json.dumps({"secret":AGENT_SECRET}).encode(),headers={"Content-Type":"application/json"},method='POST')
                urlopen(req,timeout=5)
            except: pass
        time.sleep(60)

if __name__=='__main__':
    log.info("="*50)
    log.info("🚀 OPDC Agent — نظام الحضور الذكي")
    log.info(f"📡 Port: {AGENT_PORT}")
    log.info("="*50)
    threading.Thread(target=heartbeat,daemon=True).start()
    server=HTTPServer(('0.0.0.0',AGENT_PORT),AgentHandler)
    log.info(f"✅ Agent يعمل على http://localhost:{AGENT_PORT}")
    log.info("اضغط Ctrl+C للإيقاف")
    try: server.serve_forever()
    except KeyboardInterrupt: log.info("🛑 متوقف")
