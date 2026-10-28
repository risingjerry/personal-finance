"""evidence.sqlite: originals (documents), their rows (observations), and the resolved ontology (events, links).

A document is one original file, keyed by source + content hash, so re-importing a file is a no-op.
An observation is one row of a document. Rows with a natural id (trade number, order id...) share one
record_key across files, so the same payment exported twice is still one record; rows without one are
keyed by their place in the file. Nothing here equates a row with revenue or spending: that is resolve's job.
"""
import io,json,os,sqlite3,tempfile,zipfile
from pathlib import Path
from .config import cfg
from .util import sha
from . import sources as S

SCHEMA='''PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS documents(hash TEXT PRIMARY KEY,path TEXT NOT NULL,source TEXT NOT NULL,bytes INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS observations(doc TEXT NOT NULL REFERENCES documents(hash),locator TEXT NOT NULL,source TEXT NOT NULL,dataset TEXT NOT NULL,record_key TEXT NOT NULL,identity TEXT NOT NULL,occurred_raw TEXT,timezone TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(doc,locator));
CREATE INDEX IF NOT EXISTS evidence_record ON observations(record_key);
CREATE INDEX IF NOT EXISTS evidence_dataset ON observations(source,dataset);
CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY,kind TEXT NOT NULL,book TEXT NOT NULL,project TEXT,book_by TEXT,occurred TEXT,has_time INTEGER,
 amount TEXT,sign INTEGER,account TEXT,counterparty TEXT,title TEXT,refunded INTEGER,fee_of TEXT,interest TEXT);
CREATE TABLE IF NOT EXISTS links(event TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,record_key TEXT NOT NULL,source TEXT NOT NULL,dataset TEXT NOT NULL,role TEXT NOT NULL,how TEXT NOT NULL,PRIMARY KEY(event,record_key));
CREATE INDEX IF NOT EXISTS links_record ON links(record_key);
'''
# Row keys that hold the occurrence time, first match wins (kept stable: it feeds occurred_raw only).
STAMP_KEYS=['交易时间','创建时间','时间','委托时间','Time(UTC)','Date(UTC)','transactionTime','occurred','time','ts','created_at','occurred_at']


def connect(path=None,readonly=False):
 path=Path(path or cfg().db)
 if readonly:
  db=sqlite3.connect(f'file:{path}?mode=ro',uri=True);db.row_factory=sqlite3.Row;return db
 path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
 db=sqlite3.connect(path);os.chmod(path,0o600);db.row_factory=sqlite3.Row;db.executescript(SCHEMA);return db


def record_key(source,dataset,identity,doc,locator):
 return sha(json.dumps([source,'primary-account',dataset,identity if identity is not None else [doc,locator]],ensure_ascii=False).encode())


def add(db,doc,locator,source,dataset,row,tz='未核实'):
 row={str(k).lstrip('﻿').strip():v for k,v in row.items()}
 src=S.get(source);identity=src.identity(dataset,row) if src and src.identity else None
 rk=record_key(source,dataset,identity,doc,locator)
 stamp=next((row[k] for k in STAMP_KEYS if row.get(k)),None)
 db.execute('INSERT OR IGNORE INTO observations VALUES (?,?,?,?,?,?,?,?,?)',(doc,locator,source,dataset,rk,'stable_id' if identity is not None else 'source_row',
  str(stamp) if stamp is not None else None,tz,json.dumps(row,ensure_ascii=False,sort_keys=True,default=str)))


def ingest_file(db,path,root):
 """Import one original file; returns False when unchanged since last time or no source claims it."""
 source=Path(path).relative_to(root).parts[0];src=S.get(source)
 if not src or not src.parse:return False
 data=Path(path).read_bytes();doc=source+':'+sha(data)
 if db.execute('SELECT 1 FROM documents WHERE hash=?',(doc,)).fetchone():return False
 rows=list(src.parse(Path(path),data))
 if not rows:return False
 with db:
  db.execute('INSERT INTO documents VALUES (?,?,?,?)',(doc,str(path),source,len(data)))
  for dataset,locator,row,tz in rows:add(db,doc,locator,source,dataset,row,tz)
 return True


def ingest(db,root=None):
 # Files moved under a superseded/ folder were replaced by a later read and are kept only as history.
 root=Path(root or cfg().raw)
 return sum(ingest_file(db,p,root) for p in sorted(root.rglob('*')) if p.is_file() and 'superseded' not in p.relative_to(root).parts)


def rebuild(path=None,root=None):
 """Re-derive every observation from the originals (after a parser change), swapping the file atomically."""
 path=Path(path or cfg().db)
 fd,tmp=tempfile.mkstemp(prefix=path.name+'.',suffix='.rebuild',dir=path.parent);os.close(fd);os.unlink(tmp)
 try:
  db=connect(tmp);n=ingest(db,root);db.close();os.replace(tmp,path);return n
 finally:
  if os.path.exists(tmp):os.unlink(tmp)


def zip_members(data,suffix='.csv'):
 """(name, bytes) of each member with this suffix, refusing broken or oversized archives."""
 with zipfile.ZipFile(io.BytesIO(data)) as z:
  if z.testzip():raise ValueError('Bad ZIP')
  if sum(i.file_size for i in z.infolist())>300_000_000:raise ValueError('ZIP too large')
  return [(n,z.read(n)) for n in z.namelist() if n.lower().endswith(suffix)]


def jsonl(data):
 for i,line in enumerate(data.decode('utf-8').splitlines(),1):
  if line.strip():yield i,json.loads(line)


def summary(db):
 groups=[dict(x) for x in db.execute('''SELECT source,dataset,count(*) observations,count(DISTINCT record_key) identities,
  sum(identity='source_row') weak_identity_rows,min(occurred_raw) first_raw,max(occurred_raw) last_raw FROM observations GROUP BY source,dataset ORDER BY source,dataset''')]
 return {'documents':db.execute('SELECT count(*) FROM documents').fetchone()[0],'observations':sum(g['observations'] for g in groups),'groups':groups}
