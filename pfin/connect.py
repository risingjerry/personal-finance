"""The framework side of connectors: find them, and run their verbs as separate processes.

pfin never imports a connector's code to fetch: it runs `python3 -m <module> <verb>` and reads the files the
connector writes, so a connector can be any program that honours docs/CONNECTORS.md (the Alipay one is Node).

Where connectors come from:
  - every package under ./connectors/ that defines CONNECTOR (kit excluded)
  - config.json "connector_paths": extra folders whose packages define CONNECTOR (e.g. a business's own)
"""
import importlib,json,os,subprocess,sys
from pathlib import Path
from .config import cfg

ROOT=Path(__file__).resolve().parents[1]


def _search_paths():
 return [ROOT]+[Path(os.path.expanduser(p)) for p in (cfg()._load('config.json',{}).get('connector_paths') or [])]


def discover():
 """{key: {"module": dotted name, "path": folder on sys.path, "manifest": {...}}}"""
 found={}
 for base in _search_paths():
  pkg=base/'connectors' if (base/'connectors'/'__init__.py').exists() and base==ROOT else base
  prefix='connectors.' if base==ROOT else ''
  if not pkg.is_dir():continue
  if str(base) not in sys.path:sys.path.append(str(base))
  for d in sorted(pkg.iterdir()):
   if d.name.startswith(('_','.')) or d.name in ('kit','tests') or not (d/'__init__.py').exists():continue
   try:module=importlib.import_module(prefix+d.name)
   except Exception as exc:  # a broken connector must not hide the others
    print(f'连接器 {d.name} 无法加载：{type(exc).__name__}',file=sys.stderr);continue
   c=getattr(module,'CONNECTOR',None)
   if c is not None:found[c.key]={'module':prefix+d.name,'path':str(base),'manifest':c.manifest()}
 return found


def folder(name):
 """(connector key, extra fetch args) for a raw folder: config.json "connectors" may run a connector with
 other arguments into its own folder (e.g. "alipay-work" = alipay --home ~/.alipay-cli-work)."""
 entry=cfg().connectors.get(name) or {}
 return entry.get('connector',name),[os.path.expanduser(a) for a in entry.get('args',[])]


def folder_env(name):
 """Environment for every verb run for a raw folder: config.json "connectors" -> "env" (e.g. a second Alipay
 account's own home and browser port, so its login and keepalive never touch the first account)."""
 entry=cfg().connectors.get(name) or {}
 return {k:os.path.expanduser(str(v)) for k,v in (entry.get('env') or {}).items()}


def command(key,verb,*args,found=None,extra_env=None):
 """(argv, env) for one verb of one connector."""
 info=(found or discover())[key]
 env={**os.environ,**(extra_env or {}),'PYTHONPATH':os.pathsep.join(dict.fromkeys([str(ROOT),info['path']]+[p for p in os.environ.get('PYTHONPATH','').split(os.pathsep) if p]))}
 return [sys.executable,'-m',info['module'],verb,*args],env


def run(key,verb,*args,capture=False,timeout=None,extra_env=None):
 argv,env=command(key,verb,*args,extra_env=extra_env)
 return subprocess.run(argv,cwd=ROOT,env=env,capture_output=capture,text=True,timeout=timeout,check=False)
