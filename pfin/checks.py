"""Check each WeChat Pay daily report against the cards we captured; gaps stay gaps, never zero-filled."""
import argparse,collections,datetime,json,sqlite3
from decimal import Decimal as D
from .config import cfg

def check(db):
 rows={}
 for (key,payload) in db.execute("SELECT record_key,payload FROM observations WHERE source='wechat_notices'"):rows[key]=json.loads(payload)
 spent=collections.defaultdict(lambda:{'amount':D(0),'count':0,'red_packets':0});reports=[]
 for r in rows.values():
  day=r['occurred'][:10]
  if r['kind']=='payment' and r.get('amount'):spent[day]['amount']+=D(r['amount']);spent[day]['count']+=1
  elif r['kind']=='transfer' and r.get('flow')=='out' and r.get('amount'):spent[day]['amount']+=D(r['amount']);spent[day]['count']+=1
  elif r['kind']=='red_packet' and r.get('flow')=='out':spent[day]['red_packets']+=1
  elif r['kind']=='daily_report' and r.get('spent') is not None:reports.append(r)
 out=[]
 for r in sorted(reports,key=lambda r:r['occurred']):
  # The report posted on day N covers day N-1 (Beijing time).
  day=(datetime.date.fromisoformat(r['occurred'][:10])-datetime.timedelta(days=1)).isoformat();got=spent[day]
  gap_amount=D(r['spent'])-got['amount'];gap_count=r['spent_count']-got['count']
  if gap_amount==0 and gap_count==0:status='matched'
  elif gap_count==got['red_packets'] and gap_amount>0:status='gap_is_red_packets'
  else:status='unexplained'
  out.append({'day':day,'report_amount':r['spent'],'report_count':r['spent_count'],'captured_amount':str(got['amount']),'captured_count':got['count'],'red_packets_out':got['red_packets'],'gap_amount':str(gap_amount),'gap_count':gap_count,'status':status})
 return out

if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--db',default=str(cfg().db));a=p.parse_args()
 db=sqlite3.connect(f'file:{a.db}?mode=ro',uri=True);days=check(db);db.close()
 print(json.dumps({'days':days,'summary':dict(collections.Counter(d['status'] for d in days))},ensure_ascii=False,indent=1))
