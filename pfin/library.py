"""Organized, regenerable view of every record in evidence.sqlite: one folder per kind of record and institution
(payments, banks, credit, orders, investing; plugins add their own). Which book a movement belongs to is decided
by resolution, not by where its records are filed.

raw/ stays the evidence; library/ is derived and can be deleted and rebuilt at any time.
Each dataset becomes <path>.jsonl (one deduplicated record per line, oldest first) plus a line in library/CATALOG.md.
"""
import collections,datetime,json,os,re,shutil,sqlite3
from importlib import import_module
from .config import cfg

# (source, dataset regex) -> library path. Order matters; first match wins.
LAYOUT=[
 ('wechat','payments','payments/wechat/official-export'),
 ('phone','wechat-bill','payments/wechat/app-bill'),
 ('wechat_notices','payment|daily_report|monthly_report|allowance_.*|red_packet|transfer|withdrawal|refund|red_packet_refund|transfer_refund|income|deduction_notice|discount_note|pay_later_notice|payment_failed','payments/wechat/notices-{dataset}'),
 ('wechat_notices','repayment_due|credit_repayment|statement_notice|cashback','credit/bocom-credit/wechat-{dataset}'),
 ('wechat_notices','info','payments/wechat/notices-info'),
 ('alipay','alipay-transaction','payments/alipay/official-export'),
 ('alipay-(?P<label>.+)','alipay-transaction','payments/alipay-{label}/official-export'),
 ('alipay-(?P<label>.+)','alipay-merchant-transaction','payments/alipay-{label}/merchant-funds'),
 ('phone','alipay-bill','payments/alipay/app-bill'),
 ('phone','bocom_bank-account','banks/bocom-debit/app-transactions'),
 ('bank_sms','bocom-debit_txn','banks/bocom-debit/sms-transactions'),
 ('bank_sms','bocom-loan_payment','banks/bocom-debit/sms-mortgage'),
 ('phone','abc_bank-account','banks/abc/app-transactions'),
 ('phone','cmb_bank-account','banks/cmb/app-transactions'),
 ('bank_sms','(abc|cmb|bocom)-other','banks/sms-other/{dataset}'),
 ('phone','bocom-card','credit/bocom-credit/app-bill'),
 ('bank_sms','bocom-(credit_.*|statement)','credit/bocom-credit/sms-{dataset}'),
 ('wechat_notices','card_alert|weekly_statement','credit/bocom-credit/wechat-{dataset}'),
 ('phone','huabei-bill','credit/huabei/app-bill'),
 ('phone','meituan_yuefu-bill','credit/meituan-yuefu/app-bill'),
 ('phone','douyin_yuefu-bill','credit/douyin-yuefu/app-bill'),
 ('taobao_web','orders','orders/taobao/web-orders'),
 ('jd_web','orders','orders/jd/web-orders'),
 ('phone','(sams|meituan|hema|dianping|douyin|jd|eleme)-orders','orders/{app}/app-orders'),
 ('wechat_notices','other','payments/wechat/notices-unparsed'),
 ('[a-z0-9-]+','.*-(order|bill|transaction|trip)','web/{dataset}'),  # any other connector's rows
 ('okx','.*','investing/okx/{dataset}'),
 ('binance','.*','investing/binance/{dataset}'),
 ('bitget','.*','investing/bitget/{dataset}'),
]

def place(source,dataset):
 # Plugins may add their own rows: LIBRARY = [(source, dataset regex, path), ...]
 extra=[r for m in cfg().plugins for r in getattr(import_module(m),'LIBRARY',[])]
 for s,pat,path in LAYOUT+extra:
  m=re.fullmatch(s,source)
  if m and re.fullmatch(pat,dataset):
   return path.format(dataset=re.sub(r'\s+','-',dataset.strip()),app=dataset.split('-')[0],**m.groupdict())
 return f'unsorted/{source}/{dataset}'

def build(home=None):
 home=home or cfg().home
 db=sqlite3.connect(f"file:{home/'evidence.sqlite'}?mode=ro",uri=True)
 out=home/'library';tmp=home/'library.tmp'
 if tmp.exists():shutil.rmtree(tmp)
 groups=collections.defaultdict(dict)
 for source,dataset,rk,stamp,tz,doc,payload in db.execute('SELECT source,dataset,record_key,occurred_raw,timezone,doc,payload FROM observations ORDER BY rowid'):
  # One record per identity; the first document that carried it is its provenance.
  groups[(source,dataset)].setdefault(rk,(stamp,tz,doc,payload))
 docs={d:p for d,p in db.execute('SELECT hash,path FROM documents')}
 catalog=[]
 for (source,dataset),recs in sorted(groups.items()):
  rel=place(source,dataset);f=tmp/(rel+'.jsonl');f.parent.mkdir(parents=True,exist_ok=True)
  rows=sorted(recs.items(),key=lambda kv:(kv[1][0] or ''))
  with open(f,'w') as w:
   for rk,(stamp,tz,doc,payload) in rows:
    w.write(json.dumps({'record_key':rk,'occurred':stamp,'timezone':tz,'from':os.path.relpath(docs.get(doc,''),home) if docs.get(doc) else None,'data':json.loads(payload)},ensure_ascii=False)+'\n')
  stamps=[s for s,*_ in recs.values() if s]
  catalog.append((rel,source,dataset,len(recs),min(stamps)[:10] if stamps else '',max(stamps)[:10] if stamps else ''))
 for p in tmp.rglob('*'):os.chmod(p,0o700 if p.is_dir() else 0o600)
 lines=['# 账单资料库（由 evidence.sqlite 自动生成，可删可重建）','',f"生成时间：{datetime.datetime.now(cfg().tz).strftime('%Y-%m-%d %H:%M')}（{cfg().tz.key}）",'',
        '- payments 付款通道，banks 银行卡，credit 信用卡与先买后付，orders 购物订单（只补明细，钱按付款层算一次），web 网页工具，investing 交易所',
        '- 归哪本账（个人/生意/投资）由 `pfin resolve` 决定，不看放在哪个目录','- 每行：record_key（去重身份）、occurred、timezone、from（原始文件）、data（原始字段）','',
        '| 路径 | 来源 | 数据集 | 条数 | 最早 | 最晚 |','|---|---|---|---:|---|---|']
 lines+=[f'| {r} | {s} | {d} | {n} | {a} | {b} |' for r,s,d,n,a,b in catalog]
 (tmp/'CATALOG.md').write_text('\n'.join(lines)+'\n');os.chmod(tmp/'CATALOG.md',0o600)
 if out.exists():shutil.rmtree(out)
 tmp.rename(out)
 return {'datasets':len(catalog),'records':sum(c[3] for c in catalog),'unsorted':[c[0] for c in catalog if c[0].startswith('unsorted/')]}

