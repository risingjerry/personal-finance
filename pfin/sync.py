"""One scheduled job for everything: `pfin sync`.

Each run: read the Mac's own sources (bank SMS; WeChat cards unless the real-time watcher owns them), keep
connector logins warm and fetch the ones that are due, run any other command listed, then import, resolve and
refresh the library. Adding a source means adding a line to config.json, never another job.

config.json:
  "sync": {
    "wechat_notices": false,                    # true when no real-time watcher runs
    "connectors": {                             # raw folder -> schedule (docs/CONNECTORS.md)
      "jd":   {"fetch_hours": 24},              # keepalive every manifest keepalive_hours, fetch daily
      "didi": {"fetch_hours": 168, "args": ["--browser", "--max-pages", "3"]},
      "alipay": {"keepalive": false}            # listed for status only: QR logins are never automated
    },
    "commands": [                               # anything else, each with its own minimum interval
      {"name": "my-export", "every_minutes": 60, "run": ["my-export", "--out", "{raw}/mine"]}
    ],
    "replaces": ["com.example.old-job"]         # older launchd jobs `pfin sync --install` removes
  }
"{raw}" / "{home}" in a command expand to the data folders. State: sync-state.json (last run per command).
A connector that needs a person to log in again is reported under "needs_login", not as a failure.
"""
import datetime as dt,json,os,plistlib,shutil,subprocess,sys,time
from pathlib import Path
from .config import cfg
from . import connect

LABEL='com.pfin.sync'


def _state_path():return cfg().home/'sync-state.json'


def _load_state():
 try:return json.loads(_state_path().read_text())
 except (OSError,ValueError):return {}


def _save_state(state):
 tmp=_state_path().with_suffix('.tmp');tmp.write_text(json.dumps(state,ensure_ascii=False,indent=1));os.chmod(tmp,0o600);os.replace(tmp,_state_path())


def _expand(arg):
 return os.path.expanduser(arg.replace('{raw}',str(cfg().raw)).replace('{home}',str(cfg().home)))


def _python():
 """An interpreter that can read .xlsx exports (config "python"), else this one."""
 p=cfg().python
 return p if p and os.path.exists(p) else sys.executable


def _step(name,argv,timeout=1800,env=None):
 started=time.monotonic()
 try:r=subprocess.run(argv,capture_output=True,text=True,timeout=timeout,cwd=Path(__file__).resolve().parents[1],env=env)
 except (OSError,subprocess.TimeoutExpired) as exc:
  return {'step':name,'ok':False,'seconds':round(time.monotonic()-started,1),'detail':type(exc).__name__}
 tail=(r.stderr or r.stdout or '').strip().splitlines()[-1:] if r.returncode else []
 return {'step':name,'ok':r.returncode==0,'code':r.returncode,'seconds':round(time.monotonic()-started,1),'detail':tail[0][:200] if tail else ''}


NEEDS_LOGIN=3  # docs/CONNECTORS.md exit codes
RETRY=3600  # a failed fetch is tried again after an hour, not every round


def _connector(name,plan,state,clock,now,found):
 """Keepalive when due, then fetch when due, for one raw folder. Returns the steps run."""
 key,args=connect.folder(name);st=state.setdefault('connector:'+name,{});steps=[]
 if key not in found:return [{'step':name,'ok':False,'detail':f'no connector {key}'}]
 def step(verb,*extra,timeout=300):
  argv,env=connect.command(key,verb,*extra,found=found,extra_env=connect.folder_env(name))
  r=_step(f'{name} {verb}',argv,timeout=timeout,env=env)
  if r.get('code')==NEEDS_LOGIN:r.update(ok=True,needs_login=True);st['needs_login']=True
  elif r['ok']:st['needs_login']=False
  steps.append(r);return r
 hours=float(plan.get('keepalive_hours') or found[key]['manifest'].get('keepalive_hours') or 24)
 if plan.get('keepalive',True) and (now or clock-st.get('keepalive',0)>=hours*3600):
  step('keepalive');st['keepalive']=clock
 every=plan.get('fetch_hours')
 # A known lost login waits for the keepalive to see it back; without a keepalive the fetch itself is the probe.
 if every and not (st.get('needs_login') and plan.get('keepalive',True)) and (now or (clock-st.get('fetch',0)>=float(every)*3600 and clock-st.get('fetch_try',0)>=RETRY)):
  st['fetch_try']=clock
  r=step('fetch','--out',str(cfg().raw/name),*args,*[_expand(a) for a in plan.get('args',[])],timeout=float(plan.get('timeout_minutes',30))*60)
  if r['ok'] and not r.get('needs_login'):st['fetch']=clock
 return steps


def run(now=False):
 """One round. now=True ignores every command's minimum interval."""
 home=cfg().home;lock=home/'.ingest.lock'
 try:lock.mkdir()
 except FileExistsError:return {'skipped':'another ingest is running'}
 report={'at':dt.datetime.now(cfg().tz).isoformat(timespec='seconds'),'steps':[]}
 try:
  conf=getattr(cfg(),'sync',{}) or {}
  py=sys.executable
  if conf.get('wechat_notices'):report['steps'].append(_step('wechat_notices',[py,'-m','pfin.collectors.wechat_notices']))
  report['steps'].append(_step('bank_sms',[py,'-m','pfin.collectors.bank_sms']))
  state=_load_state();clock=time.time()
  if conf.get('connectors'):
   found=connect.discover()
   for name,plan in conf['connectors'].items():report['steps'].extend(_connector(name,plan or {},state,clock,now,found))
  for c in conf.get('commands') or []:
   name=c['name'];last=state.get(name,{}).get('last_run',0)
   if not now and clock-last<60*float(c.get('every_minutes',60)):continue
   if not shutil.which(c['run'][0]) and not os.path.exists(c['run'][0]):
    report['steps'].append({'step':name,'ok':False,'detail':'command not found'});continue
   result=_step(name,[_expand(a) for a in c['run']],timeout=float(c.get('timeout_minutes',30))*60)
   state[name]={'last_run':clock,'ok':result['ok'],'detail':result.get('detail','')}
   report['steps'].append(result)
  _save_state(state)
  report['steps'].append(_step('import',[_python(),'-m','pfin','import']))
  report['steps'].append(_step('resolve',[py,'-m','pfin','resolve','--write']))
  report['steps'].append(_step('library',[py,'-m','pfin','library']))
 finally:lock.rmdir()
 report['ok']=all(s['ok'] for s in report['steps'])
 report['needs_login']=[s['step'].split()[0] for s in report['steps'] if s.get('needs_login')]
 return report


def plist(interval=600):
 root=Path(__file__).resolve().parents[1]
 path=':'.join(dict.fromkeys([str(Path(sys.executable).parent),str(Path.home()/'.local/bin'),'/opt/homebrew/bin','/usr/local/bin','/usr/bin','/bin','/usr/sbin','/sbin']))
 log=str(Path.home()/'Library/Logs/pfin-sync.log')
 return {'Label':LABEL,'ProgramArguments':[sys.executable,'-m','pfin','sync','--json'],'WorkingDirectory':str(root),
  'EnvironmentVariables':{'PATH':path,'PYTHONUNBUFFERED':'1'},'StartInterval':int(interval),'RunAtLoad':True,
  'ProcessType':'Background','LowPriorityIO':True,'StandardOutPath':log,'StandardErrorPath':log}


def _agents():return Path.home()/'Library/LaunchAgents'


def install(interval=600,dry_run=False):
 data=plistlib.dumps(plist(interval),sort_keys=False)
 if dry_run:return data.decode()
 target=_agents()/f'{LABEL}.plist';target.write_bytes(data);os.chmod(target,0o644)
 uid=str(os.getuid())
 # the one job replaces older per-purpose jobs named in config.json "sync": {"replaces": [label, ...]}
 for label in (LABEL,*cfg().sync.get('replaces',[])):
  old=_agents()/f'{label}.plist'
  subprocess.run(['launchctl','bootout',f'gui/{uid}',str(old)],capture_output=True,check=False)
  if label!=LABEL and old.exists():old.unlink()
 r=subprocess.run(['launchctl','bootstrap',f'gui/{uid}',str(target)],capture_output=True,text=True,check=False)
 return f'installed {target}'+('' if r.returncode==0 else f' (bootstrap: {r.stderr.strip()[:120]})')


def uninstall():
 target=_agents()/f'{LABEL}.plist'
 subprocess.run(['launchctl','bootout',f'gui/{os.getuid()}',str(target)],capture_output=True,check=False)
 target.unlink(missing_ok=True);return f'removed {target}'
