"""WeChat notice cards (raw/wechat_notices/, written by pfin.collectors.wechat_notices):
微信支付 payment receipts, 买单吧 (bank card) alerts, and transfer / red-packet cards from chats.
They are signals: they confirm a payment the official bill records, and stand in for it on days the bill
has not been exported yet."""
import json
from ..config import cfg
from ..model import Dataset,Source,observation
from ..util import money,when
from ..collectors.wechat_notices import parse as parse_card

SIGNALS={'payment','refund','card_alert','transfer','red_packet','red_packet_refund','transfer_refund','withdrawal','credit_repayment','income'}


def parse(path,data):
 if path.suffix!='.json':return
 d=json.loads(data)
 if d.get('count')!=len(d['cards']):raise ValueError('Notice count mismatch')
 for i,c in enumerate(d['cards']):
  row=dict(c);row.update(parse_card(c['channel'],c));yield row['kind'],f'card:{i}',row,'UTC+8'


def identity(dataset,row):
 keys=['trans_id'] if row.get('trans_id') else ['transfer_id','stage'] if row.get('transfer_id') else ['chat','server_id']
 if all(str(row.get(k,'')).strip() not in ('','None') for k in keys):return '|'.join(str(row[k]).strip() for k in keys)
 return None


def normalize(k,p,row):
 if money(p.get('amount')) is None:return None
 at,has=when(p.get('occurred'))
 sign=1 if k in ('refund','red_packet_refund','transfer_refund','income') or p.get('direction')=='incoming' else -1
 side=0
 if k=='card_alert':sign=1 if '退' in str(p.get('txn_type')) else -1
 if k=='transfer':
  sign=0  # one transfer yields several stage cards; matching ignores the side
  # Used only when the card itself becomes the event: 'flow' on the accepted card, else who sent the card.
  side={'in':1,'out':-1}.get(p.get('flow')) or ({'incoming':1,'outgoing':-1}.get(p.get('direction'),0) if p.get('stage')=='sent' else 0)
  if p.get('stage')=='returned':k,side='transfer_refund',0  # the transfer came back: cancels it, never a new transfer
 ids={p['trans_id']} if p.get('trans_id') else set()
 if p.get('transfer_id'):ids.add('wxtransfer:'+str(p['transfer_id']))
 if k=='withdrawal' and p.get('withdraw_time'):ids.add('wxwithdraw:'+p['withdraw_time'])
 # 买单吧 alerts are about a credit card; everything else moves WeChat balance or a card bound to it.
 account=f"bocom-credit:{p.get('card')}" if k=='card_alert' else 'wechat'
 return observation(row,layer='signal',role='signal',account=account,card=p.get('card'),at=at,has_time=has,amount=money(p.get('amount')),sign=sign,
  counterparty=cfg().contacts.get(p.get('chat')) or p.get('merchant') or p.get('title'),title=p.get('description'),method=p.get('method'),ids=ids,
  channel='wechat',evidence='notice',kind_hint=k,side=side,chat=p.get('chat'))


SOURCE=Source('wechat_notices',{**{k:Dataset('signal','signal') for k in SIGNALS},
 '*':Dataset('signal','signal',skip='not a money movement (reports, reminders, 零花钱 spends already counted at top-up)')},parse,identity,normalize)
