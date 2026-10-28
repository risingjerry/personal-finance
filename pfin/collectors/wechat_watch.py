"""Real-time WeChat Pay / bank-card / transfer ingestion: react to new local-DB messages instead of polling.

Follows `<wechat_reader> db-watch` (offline DB, no WeChat attach; config.json "wechat_reader"). When a new message lands in a notice chat, or a
transfer / red-packet card arrives in any chat, it runs collect -> import -> library after a short quiet period.
Read-only on WeChat. Run: python3 -m pfin.collectors.wechat_watch
"""
import datetime,json,os,subprocess,sys,threading,time
from ..config import cfg
from .wechat_notices import CHATS,reader
QUIET=3.0  # seconds without new relevant events before ingesting (cards often arrive in bursts)
TRANSFER_SUBTYPES={2000,2001}  # 微信转账 / 红包 app messages


def log(msg):print(datetime.datetime.now(cfg().tz).strftime('%F %T'),msg,flush=True)


def relevant(ev):
 d=ev.get('data') or {};chat=d.get('chat') or ''
 if ev.get('event')=='gap':return chat in CHATS or chat=='brandservicesessionholder'
 if ev.get('event')!='message':return False
 m=d.get('message') or {}
 return chat in CHATS or (m.get('kind')==49 and m.get('subtype') in TRANSFER_SUBTYPES)


def ingest():
 """collect -> import -> library; the lock dir keeps it from overlapping the scheduled SMS job."""
 lock=cfg().home/'.ingest.lock'
 try:lock.mkdir()
 except FileExistsError:log('ingest skipped: another run holds the lock');return
 try:
  py=cfg().python if cfg().python and os.path.exists(cfg().python) else sys.executable
  for cmd in ([sys.executable,'-m','pfin.collectors.wechat_notices'],[py,'-m','pfin','import'],[sys.executable,'-m','pfin','library']):
   r=subprocess.run(cmd,cwd=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),capture_output=True,text=True,timeout=600)
   if r.returncode:log(f'{" ".join(cmd[1:])} failed: {r.stderr.strip()[-300:]}');return
  log('ingested')
 finally:lock.rmdir()


def main():
 pending=threading.Event();last=[0.0]
 def worker():
  while True:
   pending.wait()
   while time.monotonic()-last[0]<QUIET:time.sleep(0.5)
   pending.clear();ingest()
 threading.Thread(target=worker,daemon=True).start()
 pending.set();last[0]=time.monotonic()-QUIET  # catch up once on start
 proc=subprocess.Popen([*reader(),'db-watch','--client',cfg().wechat_client,'--seconds','0'],stdout=subprocess.PIPE,text=True)
 log('watching')
 for line in proc.stdout:
  try:ev=json.loads(line)
  except ValueError:continue
  if relevant(ev):last[0]=time.monotonic();pending.set()
 proc.wait();log(f'db-watch exited {proc.returncode}');sys.exit(1)  # launchd KeepAlive restarts us


if __name__=='__main__':main()
