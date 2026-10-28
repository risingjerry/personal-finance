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


def _hint(when):
 """Cheap keys a rule's conditions imply, so most rules are skipped before any condition is evaluated."""
 return (when.get('account'),when.get('sign'))


def _pack(when):
 """One rule as a closure over its condition tuple: the dict lookups and the text lower() happen once per rule,
 not once per (event, rule). Behaviour is _holds(), minus the re.I-free re.search."""
 acc,sgn=_hint(when)
 ds,txt=when.get('dataset'),when.get('text') and when['text'].lower()
 rnd=Decimal(str(when['round'])) if 'round' in when else None
 fld=tuple((k,re.compile(p)) for k,p in (when.get('field') or {}).items())
 def packed(o):
  if acc is not None and o['account']!=acc:return False
  if sgn is not None and o['sign']!=sgn:return False
  if rnd is not None and (o['amount'] or 0)%rnd:return False
  if txt is not None:
   t=o.get('_text')
   if t is None:o['_text']=t=f"{o.get('counterparty') or ''} {o.get('title') or ''}".lower()  # once per observation
   if txt not in t:return False
  if ds is not None and o['dataset']!=ds:return False
  if fld:
   f=o.get('fields') or {}
   return all(rx.search(str(f.get(k) or '')) for k,rx in fld)
  return True
 return packed


_PACKED={}
def packed(when):
 """The closure for one rule's `when`: a rule is read once a run but tested against every event."""
 k=repr(sorted(when.items()))
 p=_PACKED.get(k)
 if p is None:p=_PACKED[k]=_pack(when)
 return p


def matches(when,ev):
 """Plain rules test the event's anchor. A rule naming a dataset or a field tests each linked observation, all
 conditions on the same one: e.g. the work-account leg of a move from personal Alipay into the own shop."""
 fn=packed(when)
 if 'dataset' in when or 'field' in when:return any(fn(l[0]) for l in ev['links'])
 return fn(ev['anchor'])


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


_RULES={}
def _compiled(c):
 """The owner's rules prepared once per config: each rule's closure, its implied keys and its verdict text. 47 rules
 tested against 13k events is the read path's largest single cost, so the per-(event,rule) dict work is hoisted out."""
 got=_RULES.get(id(c))
 if got is not None and got[0] is c:return got[1]
 out=[]
 for n,r in enumerate(c.rules):
  when=r.get('when') or {}
  fn=packed(when)
  out.append((fn,_hint(when)+(('dataset' in when or 'field' in when),),r,f"rule {n+1}"+(f": {r['note'][:40]}" if r.get('note') else '')))
 _RULES.clear();_RULES[id(c)]=(c,out)
 return out


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
 anchor=ev['anchor']
 for fn,(acc,sgn,per_leg),r,why in _compiled(c):
  # Skip on the implied keys first: a rule that names an account, a sign or a field cannot hold for this event.
  if acc is not None and anchor['account']!=acc and not per_leg:continue
  if sgn is not None and anchor['sign']!=sgn and not per_leg:continue
  if per_leg:
   if not any(fn(l[0]) for l in ev['links']):continue
  elif not fn(anchor):continue
  if r.get('kind'):ev['kind']=r['kind']
  if r.get('loan') and anchor['at']:ev['interest']=loan_interest(r['loan'],anchor['at'])
  return r.get('book','personal'),r.get('project'),why
 acct=c.accounts.get(ev['anchor']['account'] or '')
 if acct and acct.get('book'):return acct['book'],acct.get('project'),'account default'
 return 'personal',None,'default'
