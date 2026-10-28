"""Snapshot finished phone screen reads into raw/phone as immutable files with a manifest."""
import argparse,datetime,hashlib,json,os
from pathlib import Path
from ..config import cfg
BJ=datetime.timezone(datetime.timedelta(hours=8))

def snapshot(out=None,raw=None):
 out=Path(out or cfg().phone_out);raw=raw or cfg().raw/'phone'
 """Copy each changed <app>-<section>.jsonl as an immutable timestamped snapshot with a manifest."""
 raw.mkdir(parents=True,exist_ok=True,mode=0o700);made=[]
 for src in sorted(out.glob('*.jsonl')):
  # Only <app>-<section>.jsonl lists; the phone automation tool's own backups (name.before-fix.jsonl) are not data.
  if '.' in src.stem:continue
  # The phone automation tool may be mid-write: keep only complete lines.
  data=src.read_bytes();data=data[:data.rfind(b'\n')+1]
  if not data:continue
  progress=out/f'progress-{src.stem}.json';p=json.loads(progress.read_text()) if progress.exists() else {}
  # A half-read list may be rebuilt by the phone automation tool later; only a finished list becomes evidence.
  if not p.get('done'):continue
  digest=hashlib.sha256(data).hexdigest()
  if any(json.loads(m.read_text()).get('sha256')==digest for m in raw.glob(src.stem+'-*.manifest.json')):continue
  rows=[json.loads(l) for l in data.decode('utf-8').splitlines() if l.strip()]
  dates=sorted(r['date'] for r in rows if r.get('date'))
  stamp=datetime.datetime.now(BJ).strftime('%Y%m%dT%H%M%S');dst=raw/f'{src.stem}-{stamp}.jsonl'
  dst.write_bytes(data);os.chmod(dst,0o600)
  manifest={'source':'phone screen read','evidence_grade':'screen_read',
            'file':dst.name,'sha256':digest,'rows':len(rows),'first_date':dates[0] if dates else None,'last_date':dates[-1] if dates else None,
            'list_end_reached':bool(p.get('done')),'end_reason':p.get('end_reason'),'snapshot_at':stamp,'timezone':'Asia/Shanghai (as shown on phone)'}
  m=dst.with_suffix('.manifest.json');m.write_text(json.dumps(manifest,ensure_ascii=False,indent=1));os.chmod(m,0o600);made.append(dst.name)
 return made

if __name__=='__main__':
 argparse.ArgumentParser(description=__doc__).parse_args();print(json.dumps({'snapshots':snapshot()},ensure_ascii=False))
