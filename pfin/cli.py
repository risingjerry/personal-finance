"""pfin: personal finance from the records you already have.

  pfin import                 read new files under raw/ into evidence.sqlite (idempotent)
  pfin rebuild                re-derive evidence.sqlite from raw/ (after a parser change)
  pfin resolve --from --to    merge observations into events, report per book; --write stores the ontology
  pfin coverage               per account: which ledgers cover which days, and what fills the gaps
  pfin sources                every source and dataset, its role, and why a dataset is left out
  pfin library               regenerate the browsable library/ (one jsonl per dataset)
  pfin invest                 investing book from exchange records
  pfin sync                   one round of collect -> import -> resolve -> library; --install for the one scheduled job
  pfin connectors             every connector, the raw folders it fills, their schedule and login state
  pfin status [FOLDER]        a connector's login status (--online asks the platform once)
  pfin login FOLDER           log a connector in (a person scans / types codes)
"""
import argparse,collections,datetime as dt,json,sys
from .config import cfg


def _date(s):return dt.date.fromisoformat(s)


def cmd_import(a):
 from .store import connect,ingest,summary
 db=connect();n=ingest(db);s=summary(db)
 print(json.dumps({'new_documents':n,'documents':s['documents'],'observations':s['observations']},ensure_ascii=False))


def cmd_rebuild(a):
 from .store import rebuild
 print(json.dumps({'documents':rebuild()}))


def cmd_resolve(a):
 from .store import connect
 from .resolve import load,resolve,write
 from .report import report
 db=connect(readonly=not a.write)
 obs,ledger_end,ledger_start=load(db,a.start,a.end);events,unmatched=resolve(obs,ledger_end,ledger_start);r=report(events,unmatched,obs)
 if a.write:
  if a.start or a.end:sys.exit('--write stores the whole ontology: run it without --from/--to')
  db.close();write(connect(),events)
 name=f"events-{a.start or 'all'}_{a.end or 'all'}.json";out=cfg().home/'recon'/name;out.parent.mkdir(exist_ok=True,mode=0o700)
 out.write_text(json.dumps(r,ensure_ascii=False,indent=1));out.chmod(0o600)
 print(json.dumps({k:v for k,v in r.items() if k not in ('review','per_source')},ensure_ascii=False,indent=1));print(f'review: {out}')


def cmd_coverage(a):
 from .store import connect
 from .resolve import prepare,coverage
 db=connect(readonly=True);seen=set();obs=[]
 for r in db.execute('SELECT source,dataset,record_key,payload FROM observations'):
  if r['record_key'] in seen:continue
  seen.add(r['record_key'])
  try:o=prepare(r,json.loads(r['payload']))
  except (ValueError,TypeError,KeyError):continue
  if o and o['at']:obs.append(o)
 by=collections.defaultdict(lambda:[None,None,0])
 for o in obs:
  k=(o['account'] or '-',o['role'],f"{o['source']}/{o['dataset']}",o['priority']);v=by[k]
  v[0]=min(v[0],o['at']) if v[0] else o['at'];v[1]=max(v[1],o['at']) if v[1] else o['at'];v[2]+=1
 print(f"{'account':24} {'role':8} {'dataset':42} {'pri':>3} {'rows':>6}  first      last")
 for (acct,role,ds,p),(f,l,n) in sorted(by.items()):
  print(f"{acct:24} {role:8} {ds:42} {p:>3} {n:>6}  {f:%Y-%m-%d} {l:%Y-%m-%d}")


def cmd_sources(a):
 from . import sources as S
 for name,s in sorted(S.all_sources().items()):
  for d,spec in s.datasets.items():print(f"{name:18} {d:28} {spec.layer:7} {spec.role:8} {spec.skip or ''}")


def cmd_library(a):
 from .library import build
 print(json.dumps(build(),ensure_ascii=False))


def cmd_invest(a):
 from .invest import summary
 print(json.dumps(summary(),ensure_ascii=False,indent=1))


def cmd_sync(a):
 from . import sync
 if a.install:print(sync.install(a.interval,dry_run=a.dry_run));return
 if a.uninstall:print(sync.uninstall());return
 r=sync.run(now=a.now)
 print(json.dumps(r,ensure_ascii=False) if a.json else '\n'.join(f"{'ok ' if s['ok'] else 'ERR'} {s['step']:16} {s.get('seconds','')}s {s.get('detail','')}" for s in r.get('steps',[])) or r)
 if r.get('ok') is False:sys.exit(1)


def cmd_connectors(a):
 from . import connect,sync
 found=connect.discover();plan=(cfg().sync or {}).get('connectors') or {};state=sync._load_state()
 folders={k:[] for k in found}
 for name in sorted(set(plan)|set(cfg().connectors)):folders.setdefault(connect.folder(name)[0],[]).append(name)
 ago=lambda t:dt.datetime.fromtimestamp(t,cfg().tz).strftime('%m-%d %H:%M') if t else '-'
 for key,info in sorted(found.items()):
  m=info['manifest'];print(f"{key:10} {m['label']:10} login={m['login']:8} keepalive={m['keepalive_hours']:g}h  {m['description']}")
  own=[key] if key not in folders[key] and (key in plan or (cfg().raw/key).is_dir()) else []
  for name in own+folders[key]:
   p=plan.get(name);st=state.get('connector:'+name,{})
   when='not scheduled' if p is None else f"fetch every {p['fetch_hours']:g}h" if p.get('fetch_hours') else 'keepalive only' if p.get('keepalive',True) else 'manual'
   print(f"  raw/{name:22} {when:20} keepalive {ago(st.get('keepalive'))}  fetch {ago(st.get('fetch'))}"+('  NEEDS LOGIN' if st.get('needs_login') else ''))
 for key in sorted(set(folders)-set(found)):print(f'{key}: configured but no such connector ({", ".join(folders[key])})')


def cmd_status(a):
 from . import connect
 names=[a.folder] if a.folder else sorted({connect.folder(n)[0] for n in ((cfg().sync or {}).get('connectors') or {})} or set(connect.discover()))
 code=0
 for n in names:
  key,_=connect.folder(n);print(f'== {n}',flush=True)
  code=max(code,connect.run(key,'status',*(['--online'] if a.online else []),extra_env=connect.folder_env(n)).returncode)
 sys.exit(code)


def cmd_demo(a):
 from .demo import run
 run()


def cmd_login(a):
 from . import connect
 key,_=connect.folder(a.folder)
 sys.exit(connect.run(key,'login',extra_env=connect.folder_env(a.folder)).returncode)


def main(argv=None):
 p=argparse.ArgumentParser(prog='pfin',description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
 sub=p.add_subparsers(dest='cmd',required=True)
 for n in ('import','rebuild','coverage','sources','library','invest'):sub.add_parser(n)
 y=sub.add_parser('sync');y.add_argument('--now',action='store_true',help='ignore every command\'s minimum interval');y.add_argument('--json',action='store_true')
 y.add_argument('--install',action='store_true');y.add_argument('--uninstall',action='store_true');y.add_argument('--interval',type=int,default=600);y.add_argument('--dry-run',action='store_true')
 sub.add_parser('connectors')
 sub.add_parser('demo',help='merge a month of made-up records in a throwaway folder (no real data)')
 x=sub.add_parser('status');x.add_argument('folder',nargs='?');x.add_argument('--online',action='store_true')
 x=sub.add_parser('login');x.add_argument('folder')
 r=sub.add_parser('resolve');r.add_argument('--from',dest='start',type=_date);r.add_argument('--to',dest='end',type=_date);r.add_argument('--write',action='store_true')
 a=p.parse_args(argv);globals()['cmd_'+a.cmd](a)
