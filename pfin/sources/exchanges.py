"""Crypto exchanges: official CSV exports and read-only API dumps (raw/okx/, raw/binance/, raw/bitget/).

These feed the investing book (pfin invest); they are not merged with payments. The same folders are the
okx / binance / bitget connectors' output, so they also hold connector rows: OKX C2C payments are taken from
the okx connector's okx-c2c-order rows (exact UTC times) instead of the old web export here."""
import json,re
from pathlib import Path
from ..model import Dataset,Source
from .connectors import Combined
from ._files import csv_files,csv_table

INVESTING=Dataset('detail','detail',skip='investing data: summarized by pfin invest, not merged with payments')


def _okx_kind(h):
 if '交易单号' in h:return 'c2c'
 if '交易ID' in h:return 'fills'
 if '委托ID' in h:return 'orders'
 return 'trading_bills' if '关联订单id' in h else 'funding_bills'


def _binance_kind(h):
 if 'Order No' in h:return 'futures_orders'
 if 'Trade Id' in h:return 'futures_fills'
 if 'Transaction ID' in h:return 'futures_income'
 if '滑点' in h:return 'alpha_orders'
 if '订单编号' in h:return 'fiat_c2c'
 if '订单号' in h:return 'spot_orders'
 return 'account_ledger'


def _csv_parse(source,kind_of):
 def parse(path,data):
  for member,blob in csv_files(path,data):
   preamble=[False]
   def header_at(rows):
    preamble[0]=source=='okx' and bool(rows) and rows[0][0].startswith('用户ID:');return 1 if preamble[0] else 0
   header,rows=csv_table(blob,header_at)
   kind=kind_of(header) if kind_of else re.sub(r' \d{6,}-20\d\d.*','',Path(member).stem).removeprefix('导出')
   tz='UTC' if any('(UTC)' in h for h in header) else ('UTC+8' if 'UTC+8' in member or preamble[0] else '未核实')
   # Rows without an id dedupe by content across overlapping exports; identical rows within one file are
   # real repeats (several fills in one second), so the n-th copy is its own record.
   seen={}
   for index,row in rows:
    k=json.dumps(row,ensure_ascii=False,sort_keys=True);seen[k]=seen.get(k,0)+1;row['_repeat']=seen[k]
    yield kind,f'{member}:row:{index}',row,tz
  if path.suffix=='.json' and path.name.startswith(('transactions-','myTrades-')):
   d=json.loads(data)
   if d.get('path')=='/sapi/v1/pay/transactions':
    for i,row in enumerate(d['data']['data']):yield 'pay',f'data:{i}',row,'UTC epoch ms'
   elif d.get('path')=='/api/v3/myTrades':
    for i,row in enumerate(d['data']):yield 'spot_fills_api',f'data:{i}',row,'UTC epoch ms'
 return parse


def _keyed(table):
 def identity(dataset,row):
  keys=table(dataset)
  if keys and all(str(row.get(k,'')).strip() not in ('','--','无','None') for k in keys):return '|'.join(str(row[k]).strip() for k in keys)
  if '_repeat' in row:return 'content:'+json.dumps(row,ensure_ascii=False,sort_keys=True)
  return None
 return identity


OKX={'c2c':['交易单号'],'orders':['委托ID'],'fills':['交易品种','交易ID'],'trading_bills':['id'],'funding_bills':['id']}
BINANCE={'fiat_c2c':['订单编号'],'spot_orders':['订单号'],'alpha_orders':['订单号'],'futures_orders':['Order No'],'futures_fills':['Symbol','Trade Id'],
 'futures_income':['type','Transaction ID'],'pay':['transactionId'],'spot_fills_api':['symbol','id']}

SOURCES=[Combined(s) for s in [
 Source('okx',{'*':INVESTING,'c2c':Dataset('detail','detail',skip='superseded by the okx connector okx-c2c-order rows (exact UTC times)')},
  _csv_parse('okx',_okx_kind),_keyed(lambda d:OKX.get(d,[]))),
 Source('binance',{'*':INVESTING},_csv_parse('binance',_binance_kind),_keyed(lambda d:BINANCE.get(d,[]))),
 Source('bitget',{'*':INVESTING},_csv_parse('bitget',None),_keyed(lambda d:['订单号'] if '历史委托' in d else [])),
]]
