"""Observations -> events (docs/ONTOLOGY.md §4).

One purchase is often seen several times: the channel ledger (WeChat / Alipay statement), the funding account
(bank app, credit card, 花呗, 月付), a real-time signal (payment card, bank SMS) and an order. Money is counted once
per event; every other observation is linked to it with a role.

Rounds: channel rows become events (joining the two legs of a move between own accounts, and returned transfers);
funding rows attach to them or become events of their own; signals attach, or stand in for a ledger that has
not been exported for that day yet; details attach (or, when paid with an unconnected credit line, become events).
"""
import bisect,collections,datetime as dt,json,re
from decimal import Decimal
from .config import cfg
from . import sources as S
from .rules import book_of

# Money that stays with the owner: repayments, withdrawals, savings sweeps, cash taken out, or a counterparty
# named as the owner. Payees that are credit lines being repaid (the purchase was counted when it was made):
# 携程金融 / 趣游保理 = 携程拿去花, 美团金融 = 美团月付, 中融小贷 = 抖音月付 / 放心借.
class _Row(dict):
 '''The columns prepare() reads. Coverage and resolve only touch record_key and payload, so the wide
 source/dataset/locator/identity columns are merged into one small dict per record instead of keeping
 every sqlite3.Row of the 53k-row scan alive. The JSON payload is kept as an attribute, not a key, so the
 big strings live exactly once (a plain dict row is still accepted, tests build them that way).'''


OWN=re.compile(r'还款|提现|零钱通|余额宝|现支|取现|取款|携程金融|趣游保理|美团金融|中融小贷|中融小额')


def own_transfer(o):
 if '_own' not in o:  # asked many times per observation while matching: compute once
  t=f"{o.get('kind_hint') or ''} {o.get('counterparty') or ''} {o.get('title') or ''}"
  o['_own']=bool(OWN.search(t)) or any(n and n in t for n in cfg().owner)
 return o['_own']


def kind_of(o):  # tests only
 """Event kind from its anchor: purchase, refund, income, p2p, internal."""
 t=f"{o.get('kind_hint') or ''} {o.get('counterparty') or ''} {o.get('title') or ''} {o.get('status') or ''}"
 if o['sign']==0 or own_transfer(o) or re.search(r'充值到|转入零钱',t):return 'internal'
 if re.search(r'转账|红包|群收款|零花钱',t):return 'p2p'
 if o['sign']>0:return 'refund' if re.search(r'退款|退回',t) else 'income'
 return 'purchase'


def prepare(row,payload=None):
 """Normalize one evidence row with its source; None when the source skips it or it moves no money.
 payload defaults to the row's own stored JSON, so latest_rows() can hand over the string without a second
 json.loads. Nothing is memoised: one process normalizes each record once, and a per-record cache would only
 keep every decoded payload and observation alive for the whole run (it doubled the GC cost of a resolve)."""
 if payload is None:payload=row.payload
 return _prepare(row,payload)


def _prepare(row,payload):
 src=S.get(row['source'])
 spec=src.spec(row['dataset']) if src else None
 if not spec or spec.skip or not src.normalize:return None
 o=src.normalize(row['dataset'],payload,row)
 if o:o.update(layer=o['layer'] or spec.layer,role=o['role'] or spec.role,priority=spec.priority,tolerance=spec.tolerance,pay_later=spec.pay_later,window=spec.window,payee=spec.payee,payee_alike=spec.payee_alike)
 return o


def coverage(observations):
 """Per account, the date range of each priority's money observations, and the last day any ledger covers."""
 span={};end={}
 for o in observations:
  if o['layer']!='money' or not o['at'] or not o['account']:continue
  k=(o['account'],o['priority']);a,b=span.get(k,(o['at'],o['at']));span[k]=(min(a,o['at']),max(b,o['at']))
 return span


def keep(o,span):
 """A lower-priority money row is dropped on the days a higher-priority ledger covers for its account
 (whole days: exports are requested by date, and screen reads may lack seconds)."""
 if o['layer']!='money':return True
 d=o['at'].date()
 return not any(acct==o['account'] and p>o['priority'] and a.date()<=d<=b.date() for (acct,p),(a,b) in span.items())


def latest_rows(db):
 """One row per record: the copy from the most recently imported file, so a later snapshot's status
 (e.g. an order that went from unpaid to completed) replaces the earlier one."""
 rows={}
 for r in db.execute('SELECT source,dataset,record_key,payload FROM observations ORDER BY rowid'):
  if type(r) is dict:  # test fixture: rows keyed by column name
   src,ds,rk,pl=r['source'],r['dataset'],r['record_key'],r['payload']
  else:
   src,ds,rk,pl=r[0],r[1],r[2],r[3]
  try:row=rows[rk]
  except KeyError:row=rows[rk]=_Row(source=src,dataset=ds,record_key=rk)
  row.payload=pl  # last copy wins: a later import replaces the earlier snapshot of the same record
 return rows.values()


def load(db,start=None,end=None):
 allobs=[];seen=set()  # latest_rows already collapses exports; seen keeps a malformed record from costing twice
 for r in latest_rows(db):
  k=r['record_key']
  if k in seen:continue
  seen.add(k)
  try:o=prepare(r,json.loads(r.payload))
  except (ValueError,TypeError,KeyError):continue
  if o and o['at']:allobs.append(o)
 span=coverage(allobs);kept=[o for o in allobs if keep(o,span)]
 ledger_end,ledger_start=ledger_bounds(kept)
 window=[o for o in kept if (start is None or o['at'].date()>=start) and (end is None or o['at'].date()<=end)]
 return window,ledger_end,ledger_start


def ledger_bounds(observations):
 """First and last moment each account's money ledger covers: {account: datetime} twice."""
 start,end={},{}
 for o in observations:
  if o['layer']!='money' or not o['account'] or not o['at']:continue
  a=o['account'];end[a]=max(end.get(a,o['at']),o['at']);start[a]=min(start.get(a,o['at']),o['at'])
 return end,start


def close(a,b,secs):
 """Gap in seconds when within the window, else None. With a date-only side, compare days (at least one)."""
 if a['at'] is None or b['at'] is None:return None
 if a['has_time'] and b['has_time']:
  g=abs((a['at']-b['at']).total_seconds());return g if g<=secs else None
 g=abs((a['at'].date()-b['at'].date()).days);return g*86400 if g<=max(1,secs//86400) else None


def compatible(ev,o):
 """Can observation o describe the same money movement as event ev's anchor?"""
 a=ev['anchor']
 if o['amount'] is None or a['amount'] is None:return False
 if abs(o['amount']-a['amount'])>(o.get('tolerance') or Decimal(0)):return False
 # Opposite signs are the two legs of a move between own accounts, when either side names it as such.
 if o['sign'] and a['sign'] and o['sign']!=a['sign'] and not (o['role']!='detail' and (own_transfer(o) or own_transfer(a))):return False
 if o['channel'] and a['channel'] and o['role']=='funding' and o['channel']!=a['channel']:return False
 m=a.get('method') or ''
 # A bank / card row or notice cannot belong to a WeChat payment made from the balance or from another card.
 if a['account']=='wechat' and (o['role']=='funding' or (o['role']=='signal' and o['account'] and o['account']!='wechat')):
  if '零钱' in m and '银行' not in m:return False  # paid from WeChat balance: no bank row belongs to it
  if o.get('card') and re.search(r'\((\d{4})\)',m) and o['card'] not in m:return False
 return True


def payee_ok(o,text):
 """Is this payment plausibly the delayed payment of order o (see Dataset.payee / payee_alike)?"""
 if re.search(o['payee'],text):return True
 like=o.get('payee_alike')
 return bool(like and re.search(like,str(o.get('title') or '')) and re.search(like,text))


def resolve(observations,ledger_end=None,ledger_start=None):
 """(events, unmatched observations by role)."""
 events=[];by_id={};by_amount=collections.defaultdict(list);amounts=[]
 def new(o):
  ev={'id':f"ev:{o['key'][:16]}",'anchor':o,'kind':kind_of(o),'links':[(o,'anchor','created')]}
  events.append(ev);[by_id.setdefault(i,ev) for i in o['ids']]
  a=o['amount']
  if a is not None:
   if a not in by_amount:bisect.insort(amounts,a)
   by_amount[a].append(ev)
  return ev
 def candidates(o):
  """Events whose anchor amount is within the observation's tolerance (all compatible ones are re-checked)."""
  if o['amount'] is None:return []
  tol=o.get('tolerance') or Decimal(0)
  if not tol:return by_amount.get(o['amount'],[])
  i,j=bisect.bisect_left(amounts,o['amount']-tol),bisect.bisect_right(amounts,o['amount']+tol)
  return [ev for a in amounts[i:j] for ev in by_amount[a]]
 def refund_back(o):
  """A WeChat '转账-退款'/'红包-退款' row is the return leg of an earlier transfer, not a new event."""
  if not str(o.get('kind_hint') or '').endswith('退款') or o['sign']<=0:return None
  for ev in reversed(events):
   a=ev['anchor']
   if a['account']==o['account'] and a['sign']<0 and a['amount']==o['amount'] and '退' in str(a.get('status')) and not ev.get('refunded') \
      and a['at'] and o['at'] and 0<=(o['at']-a['at']).total_seconds()<=7*86400:
    ev['links'].append((o,'refund','returned'));ev['refunded']=True;return ev
  return None
 def own_leg(o):
  """Two own accounts (e.g. personal and work Alipay) both log a move between them: join the second leg."""
  for ev in reversed(events[-200:]):
   a=ev['anchor']
   if a['role']=='channel' and a['account']!=o['account'] and a['amount']==o['amount'] and a['sign']==-o['sign'] and o['sign'] \
      and (own_transfer(a) or own_transfer(o)) and close(a,o,120) is not None:
    ev['links'].append((o,'channel','own move'));return ev
  return None
 def attach(o,role,window,payee=None):
  for i in o['ids']:
   if i in by_id and by_id[i]['anchor'] is not o:
    ev=by_id[i];ev['links'].append((o,role,'id'));[by_id.setdefault(j,ev) for j in o['ids']]
    # A returned-transfer notice joins its transfer by id: the money came back (when only notices exist).
    if str(o.get('kind_hint') or '').endswith('_refund') and ev['anchor']['layer']=='signal':ev['refunded']=True
    return ev
  best=None
  for ev in candidates(o):
   g=close(ev['anchor'],o,window)  # cheapest test first: most same-amount events are far away in time
   if g is None or (best is not None and g>=best[0]):continue
   if any(l[0]['source']==o['source'] and l[0]['dataset']==o['dataset'] for l in ev['links']):continue
   if not compatible(ev,o):continue
   if role=='detail' and ev['kind']=='internal':continue  # an order pays someone else, never an own move
   if payee and not payee_ok(o,f"{ev['anchor'].get('counterparty') or ''} {ev['anchor'].get('title') or ''}"):continue
   best=(g,ev)
  if best:
   best[1]['links'].append((o,role,'amount+time'));[by_id.setdefault(i,best[1]) for i in o['ids']];return best[1]
  return None
 if ledger_end is None:ledger_end,ledger_start=ledger_bounds(observations)
 ledger_start=ledger_start or {}
 order=lambda o:o['at'] or dt.datetime.min.replace(tzinfo=cfg().tz)
 groups=collections.defaultdict(list)
 for o in observations:groups[o['role']].append(o)
 for o in sorted(groups['channel'],key=order):
  if not refund_back(o) and not own_leg(o):new(o)
 unmatched=collections.defaultdict(list)
 for role,window in (('funding',300),('signal',300),('detail',3600)):
  for o in sorted(groups[role],key=order):
   # A transfer card is sent when the payer sends; the bill records when it was accepted.
   if attach(o,role,3600 if o.get('kind_hint')=='transfer' else window):continue
   # A detail allowed a longer wait for its payment: only payments to the right payee qualify.
   if role=='detail' and o.get('window',0)>window and attach(o,role,o['window'],payee=bool(o.get('payee'))):continue
   if role=='funding':new(o)
   elif role=='detail' and o.get('pay_later'):
    # A real order with no payment found, probably on the platform's credit line before its records begin:
    # count it, on an account that says it is a guess. On days the credit line's own records cover, an
    # unmatched order is only unmatched (its bill would have been found).
    acct=o['pay_later'];covered=acct in ledger_start and ledger_start[acct].date()<=o['at'].date()<=ledger_end[acct].date()
    if covered:unmatched[role].append(o)
    else:o['account']=acct+':guess';new(o)
   # No ledger row for this account on that day yet (export not downloaded): the signal is the only record.
   # (An hour of grace: a late notice for the ledger's last rows that failed to match must not count twice.)
   elif role=='signal' and o['account'] and (o['account'] not in ledger_end or o['at']>ledger_end[o['account']]+dt.timedelta(hours=1)):
    if not o['sign'] and o.get('side'):o['sign']=o['side']
    new(o)
   else:unmatched[role].append(o)
 # A fee row names the trade it was charged on: it follows that trade's book and project.
 by_trade={i:ev for ev in events if not ev['anchor'].get('fee_of') for i in ev['anchor']['ids']}
 for ev in events:
  t=ev['anchor'].get('fee_of')
  if t and t in by_trade and by_trade[t] is not ev:ev['fee_of']=by_trade[t]['id']
 for ev in events:
  money_signs={l[0]['sign'] for l in ev['links'] if l[0]['layer']=='money' and l[0]['sign']}
  if money_signs=={1,-1}:ev['kind']='internal'  # money left one own account and arrived in another
  ev['book'],ev['project'],ev['book_by']=book_of(ev)
 ids={ev['id']:ev for ev in events}
 for ev in events:
  if ev.get('fee_of'):
   o=ids[ev['fee_of']];ev['kind']='fee';ev['book'],ev['project'],ev['book_by']=o['book'],o['project'],'fee of '+o['id']
 return events,unmatched


def write(db,events):
 """Replace the stored ontology (events, links) with this resolution."""
 # 34k single-row executes cost more in per-statement overhead than the commit does, so the rows are built
 # first and handed over in one executemany each. The statements and their order are unchanged.
 rows=[];links=[]
 for ev in events:
  a=ev['anchor']
  rows.append((ev['id'],ev['kind'],ev['book'],ev.get('project'),ev.get('book_by'),
   a['at'].isoformat() if a['at'] else None,int(a['has_time']),str(a['amount']) if a['amount'] is not None else None,a['sign'],a['account'],
   a.get('counterparty'),(a.get('title') or '')[:200],int(bool(ev.get('refunded'))),ev.get('fee_of'),str(ev['interest']) if ev.get('interest') else None))
  for o,role,how in ev['links']:
   links.append((ev['id'],o['key'],o['source'],o['dataset'],role,how))
 # Both tables are rewritten in full, so no parent row can go missing underneath a surviving child and the
 # delete-side foreign key check can only ever pass -- but it costs a scan of events per deleted link
 # (~110ms of a 270ms write). It is switched off for the rewrite and the constraint is verified once after.
 db.execute('PRAGMA foreign_keys=OFF')
 try:
  with db:
   db.execute('DELETE FROM links');db.execute('DELETE FROM events')
   db.executemany('INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',rows)
   db.executemany('INSERT OR IGNORE INTO links VALUES (?,?,?,?,?,?)',links)
 finally:
  db.execute('PRAGMA foreign_keys=ON')
 bad=db.execute('SELECT COUNT(*) FROM links WHERE event NOT IN (SELECT id FROM events)').fetchone()[0]
 if bad:raise RuntimeError(f'{bad} links reference a missing event')
