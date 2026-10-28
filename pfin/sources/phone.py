"""Lists read off the phone screen (raw/phone/<app>-<section>-<time>.jsonl, from pfin.collectors.phone_snapshot).

Each row: {app, section, date, time, title, amount, status, raw}. Weak evidence: an official export or a web
tool for the same account wins over its date range (priority), and the screen read fills only the rest.
Identical rows in one list are real repeats (several 0.01 red packets in a minute), so the n-th copy is its
own record; re-reading the same list still dedupes.
"""
import re
from ..config import cfg
from ..model import Dataset,Source,observation
from ..store import jsonl
from ..util import channel_of,money,when


def parse(path,data):
 if path.suffix!='.jsonl':return
 seen={}
 for i,row in jsonl(data):
  row['occurred']=' '.join(x for x in (row.get('date'),row.get('time')) if x) or None
  k=(row.get('date'),row.get('time'),row.get('raw'));seen[k]=seen.get(k,0)+1;row['repeat']=seen[k]
  yield f"{row['app']}-{row['section']}",f'line:{i}',row,'UTC+8'


def identity(dataset,row):
 # The time may be blank (lists that show only dates); the rest must be there.
 keys=['app','section','date','raw','repeat']
 if all(str(row.get(k,'')).strip() not in ('','--','无','None') for k in keys):
  return '|'.join(str(row.get(k) or '').strip() for k in ['app','section','date','time','raw','repeat'])
 return None


def _card(dataset,p):
 return (re.search(r'尾号(\d{4})',p.get('raw') or '') or [None,None])[1] or cfg().cards.get(f'phone/{dataset}')


def bank(dataset,p,row):
 a=money(p.get('amount'));at,has=when(p.get('occurred'));card=_card(dataset,p)
 return observation(row,layer='money',role='funding',account=dataset.split('_')[0]+':'+str(card),card=card,at=at,has_time=has,
  amount=abs(a) if a is not None else None,sign=(1 if a and a>0 else -1 if a and a<0 else 0),counterparty=p.get('title'),title=p.get('raw'),
  channel=channel_of(p.get('title') or p.get('raw')),evidence='screen')


def credit_card(dataset,p,row):
 a=money(p.get('amount'));st=p.get('status') or '';at,has=when(p.get('occurred'));card=_card(dataset,p)
 sign=1 if ('退款' in st or (a is not None and a<0)) else (1 if '还款' in st else -1)
 return observation(row,layer='money',role='funding',account=f'bocom-credit:{card}',card=card,at=at,has_time=has,amount=abs(a) if a is not None else None,
  sign=sign,counterparty=p.get('title'),title=p.get('raw'),status=st,channel=channel_of(p.get('title')),evidence='screen')


def credit_line(account,channel):
 """花呗 / 月付 style lists: a purchase shows as a positive amount, a repayment as negative."""
 def normalize(dataset,p,row):
  a=money(p.get('amount'));at,has=when(p.get('occurred'));t=f"{p.get('title') or ''} {p.get('status') or ''}"
  return observation(row,layer='money',role='funding',account=account,at=at,has_time=has,amount=abs(a) if a is not None else None,
   sign=1 if a is not None and a<0 else -1,counterparty=p.get('title'),title=p.get('raw'),status=p.get('status'),channel=channel,evidence='screen',
   kind_hint='还款' if '还款' in t else None)
 return normalize


def wechat_app_bill(dataset,p,row):
 """WeChat app bill list: the only WeChat ledger before the official export's first day."""
 a=money(p.get('amount'));at,has=when(p.get('occurred'));t=p.get('raw') or ''
 if a is None or not a:return None
 return observation(row,layer='money',role='channel',account='wechat',at=at,has_time=has,amount=abs(a),sign=1 if a>0 else -1,
  counterparty=p.get('title'),title=t,status=p.get('status'),channel='wechat',evidence='screen',kind_hint='转账' if '转账' in t else None)


def order(channel):
 def normalize(dataset,p,row):
  a=money(p.get('amount') if p.get('amount') is not None else p.get('price'))
  if re.search(r'取消|关闭|失败|未支付',str(p.get('status') or '')) or not a:return None
  at,has=when(p.get('occurred'))
  return observation(row,layer='detail',role='detail',at=at,has_time=has,amount=abs(a),sign=-1,counterparty=p.get('merchant') or p.get('store'),
   title=p.get('title') or p.get('item') or p.get('raw'),status=p.get('status'),channel=channel,evidence='screen')
 return normalize


NORMALIZE={'bocom_bank-account':bank,'abc_bank-account':bank,'cmb_bank-account':bank,'bocom-card':credit_card,
 'huabei-bill':credit_line('huabei','alipay'),'meituan_yuefu-bill':credit_line('meituan-yuefu','meituan'),
 'douyin_yuefu-bill':credit_line('douyin-yuefu','douyin'),'wechat-bill':wechat_app_bill,
 'meituan-orders':order('meituan'),'sams-orders':order(None),'hema-orders':order(None)}


def normalize(dataset,p,row):
 f=NORMALIZE.get(dataset);return f(dataset,p,row) if f else None


SOURCE=Source('phone',{
 **{d:Dataset('money','funding') for d in ('bocom_bank-account','abc_bank-account','cmb_bank-account','bocom-card','huabei-bill','douyin_yuefu-bill','meituan_yuefu-bill')},
 'wechat-bill':Dataset('money','channel'),
 **{d:Dataset('detail','detail') for d in ('meituan-orders','sams-orders','hema-orders')},
 'alipay-bill':Dataset('money','channel',skip='superseded by alipay_cli official statement (same days, signs not printed)'),
 '*':Dataset('detail','detail',skip='no normalizer for this list yet')},parse,identity,normalize)
