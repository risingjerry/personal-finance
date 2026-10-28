"""What the resolved events say: totals per book / project / kind, and what still needs the owner's eye."""
import collections
from decimal import Decimal


def short(o):
 return {'source':f"{o['source']}/{o['dataset']}",'at':o['at'].isoformat() if o['at'] else None,'amount':str(o['amount']),'sign':o['sign'],
         'counterparty':o['counterparty'],'title':(o['title'] or '')[:60]}


def report(events,unmatched,observations):
 per=collections.Counter((o['source'],o['dataset'],o['role']) for o in observations)
 linked=collections.Counter()
 for ev in events:
  for o,role,how in ev['links']:linked[(o['source'],o['dataset'],how)]+=1
 books=collections.defaultdict(lambda:collections.defaultdict(Decimal))
 for ev in events:
  if ev.get('refunded'):continue  # money went out and came back: net zero
  a=ev['anchor']['amount'] or Decimal(0);bk=ev['book']+(f"/{ev['project']}" if ev.get('project') else '')
  if ev.get('interest'):a-=ev['interest'];books[bk]['interest_out']+=ev['interest']  # repayment = principal (internal) + interest (cost)
  if ev['anchor'].get('fee_cost'):books[bk]['fee_out']+=ev['anchor']['fee_cost']  # e.g. WeChat withdrawal fee, kept out of the amount
  books[bk][ev['kind']+('_in' if ev['anchor']['sign']>0 else '_out' if ev['anchor']['sign']<0 else '')]+=a
 todo=collections.Counter();todo_n=collections.Counter()
 for ev in events:
  if ev['book_by']=='default' and ev['kind'] in ('purchase','p2p'):c=ev['anchor']['counterparty'];todo[c]+=ev['anchor']['amount'] or 0;todo_n[c]+=1
 funding_only=[ev for ev in events if ev['anchor']['role']=='funding']
 return {'observations':len(observations),'events':len(events),
  'events_seen_in_2plus_sources':sum(1 for ev in events if len({(l[0]['source'],l[0]['dataset']) for l in ev['links']})>1),
  'kinds':dict(collections.Counter(ev['kind'] for ev in events)),
  'books':{b:{k:str(v) for k,v in sorted(d.items())} for b,d in sorted(books.items())},
  'confirm_book_for':[{'counterparty':k,'events':todo_n[k],'amount':str(v)} for k,v in todo.most_common(25)],
  'per_source':{f'{s}/{d}':{'role':r,'count':n,'linked':{h:linked[(s,d,h)] for h in ('created','id','amount+time','own move','returned') if linked[(s,d,h)]}}
                for (s,d,r),n in sorted(per.items())},
  'unmatched':{k:len(v) for k,v in unmatched.items()},
  'funding_only_events':len(funding_only),
  'review':{'signals_without_event':[short(o) for o in unmatched['signal']],'details_without_event':[short(o) for o in unmatched['detail']],
            'funding_only':[short(ev['anchor']) for ev in funding_only]}}
