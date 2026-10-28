"""Which book / project an event belongs to: the owner's ordered rules (rules.json), per-transaction overrides
(overrides.json) and account defaults (config.json "accounts"). Format: docs/RULES.md.

rules.json: {"rules": [{"when": {...}, "book": "business", "project": "invoice", "kind": "internal", "loan": {...}, "note": "..."}]}
  when.text        substring of counterparty or title (case-insensitive)
  when.account     the account id, exactly
  when.sign        -1 money out / 1 money in
  when.dataset     the observation comes from this dataset (e.g. "okx-c2c-order")
  when.field       {"merchant_order_no": "^TP"}: the observation's field matches the regex
  when.round       amount is a multiple of this (e.g. 1000)
With dataset or field, every condition is tested on one linked observation; otherwise on the anchor.
The first matching rule wins; put narrow rules first.
"""
import re
from decimal import Decimal
from .config import cfg


def _holds(when,o):
 if 'account' in when and o['account']!=when['account']:return False
 if 'sign' in when and o['sign']!=when['sign']:return False
 if 'round' in when and (o['amount'] or 0)%Decimal(str(when['round'])):return False
 if 'text' in when:
  if '_text' not in o:o['_text']=f"{o.get('counterparty') or ''} {o.get('title') or ''}".lower()  # once per observation
  if when['text'].lower() not in o['_text']:return False
 if 'dataset' in when and o['dataset']!=when['dataset']:return False
 return all(re.search(pat,str((o.get('fields') or {}).get(k) or '')) for k,pat in (when.get('field') or {}).items())


def matches(when,ev):
 """Plain rules test the event's anchor. A rule naming a dataset or a field tests each linked observation, all
 conditions on the same one: e.g. the work-account leg of a move from personal Alipay into the own shop."""
 if 'dataset' in when or 'field' in when:return any(_holds(when,l[0]) for l in ev['links'])
 return _holds(when,ev['anchor'])


def loan_interest(loan,at):
 """Interest inside one instalment of an equal-payment loan, from the owner's schedule ("loan" in a rule):
 {"principal": "10000.00", "payments": ["880.00", ..., "880.12"], "first": "2022-01"}. The monthly rate is solved from
 principal and payments; the last instalment takes the rounding remainder, so the parts sum exactly."""
 pay=[Decimal(x) for x in loan['payments']];P=Decimal(loan['principal'])
 y,m=map(int,loan['first'].split('-'));k=(at.year-y)*12+at.month-m
 if not 0<=k<len(pay):return None
 lo,hi=0.0,0.1
 for _ in range(100):
  r=(lo+hi)/2;bal=float(P)
  for x in pay:bal=bal*(1+r)-float(x)
  lo,hi=(r,hi) if bal<0 else (lo,r)
 bal,cent=P,Decimal('0.01')
 for i,x in enumerate(pay):
  it=(bal*Decimal(r)).quantize(cent) if i<len(pay)-1 else x-bal
  if i==k:return it
  bal-=x-it


def book_of(ev):
 """(book, project, why). Order: override by id > owner rule > account default > personal. A rule or override
 may also set the event kind (e.g. a credit repayment is internal) or a loan schedule (interest is split out)."""
 c=cfg()
 # Any number of any linked record (trade, order, transfer id) can carry the owner's decision.
 for i in sorted({i for l in ev['links'] for i in l[0]['ids']}):
  if i in c.overrides:
   x=c.overrides[i]
   if x.get('kind'):ev['kind']=x['kind']
   return x.get('book','personal'),x.get('project'),'override: '+x.get('evidence','owner')
 for n,r in enumerate(c.rules):
  if matches(r.get('when') or {},ev):
   if r.get('kind'):ev['kind']=r['kind']
   if r.get('loan') and ev['anchor']['at']:ev['interest']=loan_interest(r['loan'],ev['anchor']['at'])
   return r.get('book','personal'),r.get('project'),f"rule {n+1}"+(f": {r['note'][:40]}" if r.get('note') else '')
 acct=c.accounts.get(ev['anchor']['account'] or '')
 if acct and acct.get('book'):return acct['book'],acct.get('project'),'account default'
 return 'personal',None,'default'
