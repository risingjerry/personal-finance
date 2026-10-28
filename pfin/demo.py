"""`pfin demo`: a month of made-up records in a throwaway folder, merged into events. Shows what pfin does
without touching any real data: six records from Alipay, Taobao, Ctrip and 拿去花 become four events, each
money movement counted once."""
import json,tempfile
from pathlib import Path
from . import config,sources

ALIPAY=[
 dict(id='DEMO-A1',occurred='2026-01-05 20:15:02',amount='129.00',direction='out',merchant='某数码旗舰店',title='机械键盘',status='交易成功',
  extra={'trade_no':'DEMO-A1','merchant_order_no':'T200P1000000000000001'}),
 dict(id='DEMO-A2',occurred='2026-01-08 12:30:00',amount='200.00',direction='out',merchant='张三',title='转账',status='交易成功',extra={'trade_no':'DEMO-A2'}),
 dict(id='DEMO-A3',occurred='2026-01-09 09:00:00',amount='300.00',direction='out',merchant='花呗',title='花呗自动还款',status='还款成功',extra={'trade_no':'DEMO-A3'}),
]
ROWS={
 'alipay':[{'source':'alipay','record_type':'transaction',**r} for r in ALIPAY],
 'taobao':[dict(source='taobao',record_type='order',id='taobao:order:1000000000000001',occurred='2026-01-05 20:14:31',amount='129.00',
  direction='out',merchant='某数码旗舰店',title='机械键盘 87 键',status='交易成功',extra={})],
 'ctrip':[dict(source='ctrip',record_type='order',id='ctrip:order:1',occurred='2026-01-12 10:00:00',amount='420.00',direction='out',
   merchant='某酒店',title='某酒店 1 晚',status='已完成',extra={}),
  dict(source='ctrip-naquhua',record_type='bill',id='naquhua:1',occurred='2026-01-14 12:00:00',amount='420.00',direction='out',
   merchant='某酒店',title='酒店',status='SUCCESS',extra={})],
}


def run():
 home=Path(tempfile.mkdtemp(prefix='pfin-demo-'))
 (home/'config.json').write_text(json.dumps({'timezone':'Asia/Shanghai','owner':['示例用户']},ensure_ascii=False))
 for folder,rows in ROWS.items():
  d=home/'raw'/folder;d.mkdir(parents=True)
  (d/f'{folder}-rows-20260131T000000.jsonl').write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in rows))
 config.use(config.Config(home=home));sources.reset()
 from .store import connect,ingest
 from .resolve import load,resolve
 from .report import report
 db=connect();ingest(db)
 obs,end,start=load(db,None,None);events,unmatched=resolve(obs,end,start)
 print(f'{len(obs)} records -> {len(events)} events  (data: {home})\n')
 for e in sorted(events,key=lambda e:e['anchor']['at']):
  a=e['anchor'];seen=' + '.join(o['dataset'] for o,_,_ in e['links'])
  print(f"{a['at']:%m-%d %H:%M}  {e['kind']:9} {a['amount']:>8}  {a.get('counterparty') or '':8}  <- {seen}")
 r=report(events,unmatched,obs)
 print('\n'+json.dumps({k:v for k,v in r.items() if k not in ('review','per_source')},ensure_ascii=False,indent=1))
 return events
