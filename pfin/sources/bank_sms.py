"""Bank SMS (raw/bank_sms/, written by pfin.collectors.bank_sms from the Mac Messages database).
Card movements are signals for the bank-app ledger of the same card; statements and reminders are skipped."""
import json
from ..model import Dataset,Source,observation
from ..util import channel_of,money,when
from ..collectors.bank_sms import parse as parse_sms

MOVES={'debit_txn','credit_txn','credit_refund','credit_repayment','loan_payment'}


def parse(path,data):
 if path.suffix!='.json':return
 d=json.loads(data)
 if d.get('count')!=len(d['messages']):raise ValueError('SMS count mismatch')
 for i,m in enumerate(d['messages']):
  row=dict(m);row.update(parse_sms(m));yield f"{m['bank']}-{row['kind']}",f'sms:{i}',row,'UTC+8'


def identity(dataset,row):return str(row['guid']) if row.get('guid') else None


def normalize(dataset,p,row):
 kind=dataset.split('-',1)[1]
 if kind not in MOVES:return None
 at,has=when(p.get('occurred'));bank=p.get('bank')
 # Same account id as the bank app list of that card, so a missing app list can be filled by its SMS.
 card=p.get('card');account=None if not card else f"{bank}-credit:{card}" if kind.startswith('credit') else f"{bank}:{card}"
 sign={'out':-1,'in':1}.get(p.get('flow'),0) if kind!='credit_repayment' else 1
 return observation(row,layer='signal',role='signal',account=account,card=p.get('card'),at=at,has_time=has,amount=money(p.get('amount')),sign=sign,
  counterparty=p.get('desc'),title=p.get('text'),channel=channel_of(p.get('desc')),evidence='notice',kind_hint='还款' if 'repayment' in kind else None)


SOURCE=Source('bank_sms',{'*':Dataset('signal','signal')},parse,identity,normalize)
