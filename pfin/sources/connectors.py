"""Connector output (raw/<folder>/*-rows-*.jsonl): one row per order / bill / transaction / trip in the shared row
format ({source, record_type, id, occurred, amount, direction, merchant, title, status, extra}; docs/CONNECTORS.md).

A raw folder is named after its connector (raw/taobao, raw/jd ...) or after a config.json "connectors" entry that
runs a connector with other arguments (raw/alipay-work: the alipay connector on a second account). The entry may
name the ledger account its money rows belong to ({"account": "alipay:work"}).

Orders are details (what was bought); credit lines (美团月付, 拿去花) and Alipay statements are money ledgers;
OKX C2C orders are details of a payment made to the counterparty outside the exchange."""
import re
from ..model import Dataset,Source,observation
from ..store import jsonl
from ..config import cfg
from ..util import channel_of,money,when

CLOSED=re.compile(r'取消|关闭|失败|未支付')


def parse(path,data):
 if path.suffix!='.jsonl' or '-rows-' not in path.name:return
 for i,row in jsonl(data):yield f"{row['source']}-{row['record_type']}",f'line:{i}',row,'UTC+8'


def identity(dataset,row):
 keys=['source','record_type','id']
 if all(str(row.get(k,'')).strip() not in ('','None') for k in keys):return '|'.join(str(row[k]).strip() for k in keys)
 return None


def yuefu_bill(dataset,p,row):
 at,has=when(p.get('occurred'));x=p.get('extra') or {}
 return observation(row,layer='money',role='funding',account='meituan-yuefu',at=at,has_time=has,amount=money(p.get('amount')),
  sign={'out':-1,'in':1}.get(p.get('direction'),0),counterparty=p.get('merchant'),title=p.get('title'),ids={x.get('trade_no'),x.get('pay_id')},channel='meituan',evidence='web')


def yuefu_repayment(dataset,p,row):
 # 主动还款/代扣还款: money comes into the credit line from a bank card (the other leg is the bank's 美团金融 debit).
 at,has=when(p.get('occurred'))
 return observation(row,layer='money',role='funding',account='meituan-yuefu',at=at,has_time=has,amount=money(p.get('amount')),sign=1,
  counterparty='美团月付还款',title=p.get('title'),ids={p['id']},channel='meituan',evidence='web',kind_hint='还款')


def naquhua(dataset,p,row):
 """携程拿去花 (fetched with Ctrip orders): a purchase on the credit line, or a repayment / refund into it."""
 if p.get('status')=='FAILED':return None  # a failed auto-repayment moved no money
 at,has=when(p.get('occurred'));purchase=p.get('record_type')=='bill'
 return observation(row,layer='money',role='funding',account='ctrip-naquhua',at=at,has_time=has,amount=money(p.get('amount')),
  sign=-1 if purchase else 1,counterparty=p.get('merchant'),title=p.get('title'),status=p.get('status'),channel=None,evidence='web',
  kind_hint=None if purchase else '还款')


def okx_c2c(dataset,p,row):
 if p.get('status')!='completed':return None
 x=p.get('extra') or {};at,has=when(p.get('occurred'))
 return observation(row,layer='detail',role='detail',at=at,has_time=has,amount=money(p.get('amount')),sign={'out':-1,'in':1}.get(p.get('direction'),0),
  counterparty=p.get('merchant'),title=p.get('title'),channel=channel_of(x.get('payment_method')),evidence='web')


def order(dataset,p,row):
 a=money(p.get('amount') if p.get('amount') is not None else p.get('price'))
 if CLOSED.search(str(p.get('status') or '')) or not a:return None
 at,has=when(p.get('occurred'));oid=p.get('id','')
 return observation(row,layer='detail',role='detail',at=at,has_time=has,amount=abs(a),sign=-1,counterparty=p.get('merchant') or p.get('store'),
  title=p.get('title') or p.get('item') or p.get('raw'),status=p.get('status'),ids={'taobao:'+oid.split(':')[-1]} if oid.startswith('taobao:order:') else set(),evidence='web')


def _fee_of(text):
 """An Alipay merchant '服务费[<trade no>]' row is the channel fee of that trade."""
 m=re.search(r'服务费\[(\w+)\]',str(text or ''));return m[1] if m else None


def alipay_statement(dataset,p,row,account):
 """Alipay official statement rows (connectors/alipay fetch)."""
 x=p.get('extra') or {}
 if p.get('status')=='交易关闭' and p.get('direction') not in ('in','out'):return None  # closed unpaid order: no money moved
 at,has=when(p.get('occurred'));item=p.get('title')
 ids={x.get('trade_no'),x.get('merchant_order_no')}
 m=re.search(r'T200P(\d+)',x.get('merchant_order_no') or '')
 if m:ids.add('taobao:'+m.group(1))
 return observation(row,layer='money',role='channel',account=account or 'alipay',at=at,has_time=has,amount=money(p.get('amount')),
  sign={'out':-1,'in':1}.get(p.get('direction'),0),counterparty=p.get('merchant'),title=item,status=p.get('status'),ids=ids,channel='alipay',
  evidence='official_export',fee=money(x.get('fee')),kind_hint='还款' if '自动还款' in str(item) else None,fee_of=_fee_of(item),
  fields={'merchant_order_no':x.get('merchant_order_no') or ''})


def alipay_merchant(dataset,p,row,account):
 """A merchant account's balance ledger (open-platform API); misses payments funded by a bank card."""
 x=p.get('extra') or {};at,has=when(p.get('occurred'));note=p.get('title') or ''
 if not money(p.get('amount')):return None
 return observation(row,layer='money',role='channel',account=account or 'alipay',at=at,has_time=has,amount=money(p.get('amount')),
  sign={'out':-1,'in':1}.get(p.get('direction'),0),counterparty=p.get('merchant'),title=note,ids={x.get('business_id'),x.get('order_id')},
  channel='alipay',evidence='open_api',kind_hint=p.get('status'),fee_of=_fee_of(note),fields={'merchant_order_no':x.get('order_id') or ''})


ACCOUNT_AWARE={'alipay-transaction':alipay_statement,'alipay-merchant-transaction':alipay_merchant}
ORDERS=('taobao-order','jd-order','didi-trip','dianping-order','goofish-order','ctrip-order')
NORMALIZE={'ctrip-naquhua-bill':naquhua,'ctrip-naquhua-transaction':naquhua,'meituan-yuefu-bill':yuefu_bill,'meituan-yuefu-transaction':yuefu_repayment,'okx-c2c-order':okx_c2c,**{d:order for d in ORDERS}}


def source(name):
 account=(cfg().connectors.get(name) or {}).get('account')
 def normalize(dataset,p,row):
  if dataset in ACCOUNT_AWARE:return ACCOUNT_AWARE[dataset](dataset,p,row,account)
  f=NORMALIZE.get(dataset);return f(dataset,p,row) if f else None
 return Source(name,DATASETS,parse,identity,normalize)


DATASETS={
 'alipay-transaction':Dataset('money','channel',priority=2),'alipay-merchant-transaction':Dataset('money','channel',priority=1),
 'meituan-yuefu-bill':Dataset('money','funding',priority=2),'meituan-yuefu-transaction':Dataset('money','funding',priority=2),
 'okx-c2c-order':Dataset('detail','detail',tolerance=money(1)),
 **{d:Dataset('detail','detail') for d in ORDERS},
 # 携程 orders are often paid days later (拿去花 bills the stay, hotels charge at the desk): look a week ahead,
 # but only at payments to 携程 or to the same kind of merchant.
 'ctrip-order':Dataset('detail','detail',window=7*86400,payee='携程',payee_alike='酒店|宾馆|民宿|客栈|公寓',pay_later='ctrip-naquhua'),
 'ctrip-naquhua-bill':Dataset('money','funding',priority=2),'ctrip-naquhua-transaction':Dataset('money','funding',priority=2),
 'jd-baitiao-bill':Dataset('money','funding',skip='monthly statement, not a transaction'),
 '*':Dataset('detail','detail',skip='no normalizer for this dataset yet')}


ROWS=re.compile(r'-(order|bill|transaction|trip)$')  # a rows dataset is "<row source>-<record_type>"


def factory(name):
 """Any raw folder no other source claims is read as a connector's output (only its rows files are parsed)."""
 return source(name)


class Combined(Source):
 """A folder that has its own source and also holds connector rows (raw/okx: exchange exports + the okx
 connector's output). Rows datasets are told apart by name, so the folder's own dataset names must not end
 in -order/-bill/-transaction/-trip."""
 def __init__(self,own):
  rows=source(own.name);pick=lambda d:rows if ROWS.search(d) else own
  super().__init__(own.name,own.datasets,
   lambda path,data:rows.parse(path,data) if '-rows-' in path.name else own.parse(path,data),
   lambda d,r:pick(d).identity(d,r) if pick(d).identity else None,
   lambda d,p,r:pick(d).normalize(d,p,r) if pick(d).normalize else None,own.stamp_keys)
  self.rows=rows

 def spec(self,dataset):
  return self.rows.spec(dataset) if ROWS.search(dataset) else super().spec(dataset)
