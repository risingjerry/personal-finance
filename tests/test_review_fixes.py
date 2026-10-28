"""One test per finding of the independent review: each would have counted money twice or missed it."""
import datetime as dt,json,unittest
from decimal import Decimal
from pathlib import Path
import _env
from connectors.alipay import rows as alipay_rows
from pfin import resolve as R
from pfin.report import report
from pfin.store import connect,ingest
from pfin.util import when
from pfin.sources import phone

def row(source,dataset,key,payload):return {'source':source,'dataset':dataset,'record_key':key},payload
def ali(folder,key,record,merchant=False):
 """An Alipay statement / merchant-funds record as the alipay connector writes it."""
 r=alipay_rows({'kind':'alipay_merchant_funds' if merchant else 'alipay_bills','records':[record]})[0]
 return row(folder,r['source']+'-transaction',key,r)
def norm(*rows):return [o for o in (R.prepare(r,p) for r,p in rows) if o]
class DB:
 def __init__(self,rows):self.rows=[dict(r,payload=json.dumps(p)) for r,p in rows]
 def execute(self,_):return self.rows
WX={'交易单号':'1','交易对方':'店','交易时间':'2022-09-01 12:00:20','交易类型':'商户消费','商品':'示例商品','商户单号':'/','备注':'/','当前状态':'支付成功','支付方式':'零钱','收/支':'支出','金额(元)':100}


class ReviewFixes(unittest.TestCase):
 def setUp(self):_env.isolated()

 def test_screen_row_on_a_covered_day_is_dropped_even_without_seconds(self):
  got,*_=R.load(DB([row('wechat','payments','a',WX),row('phone','wechat-bill','b',{'amount':'-100','date':'2022-09-01','occurred':'2022-09-01 12:00','title':'店','raw':'店,支出100'})]))
  self.assertEqual([o['key'] for o in got],['a'])

 def test_late_notice_within_an_hour_of_the_ledger_end_does_not_stand_alone(self):
  o=norm(row('wechat','payments','a',WX),row('wechat_notices','payment','b',{'kind':'payment','amount':'100','occurred':'2022-09-01T12:06:30+08:00','merchant':'店'}))
  ev,un=R.resolve(o);self.assertEqual(len(ev),1)

 def test_pay_later_order_does_not_swallow_an_unrelated_payment(self):
  o=norm(row('ctrip','ctrip-order','a',{'amount':'100','merchant':'携程','title':'某酒店','status':'已完成','occurred':'2022-10-01 09:00:00','id':'ctrip:order:1'}),
   ali('alipay','b',{'trade_no':'t','amount':'100','direction':'out','counterparty':'某餐厅','item':'饭','paid_at':'2022-10-05T12:00:00+08:00'}))
  ev,_=R.resolve(o);self.assertEqual(len(ev),2)

 def test_order_on_days_the_credit_line_covers_is_not_guessed(self):
  bill=lambda k,at:row('ctrip','ctrip-naquhua-bill',k,{'record_type':'bill','amount':'50','direction':'out','merchant':'携程','title':'示例商品','occurred':at,'id':k})
  o=norm(bill('n1','2022-02-01 10:00:00'),bill('n2','2022-10-30 10:00:00'),
   row('ctrip','ctrip-order','a',{'amount':'200.00','merchant':'携程','title':'某酒店','status':'已完成','occurred':'2022-10-01 00:40:00','id':'ctrip:order:9'}))
  ev,un=R.resolve(o);self.assertEqual(len(ev),2);self.assertEqual(len(un['detail']),1)

 def test_hotel_booking_paid_at_the_desk_days_later_is_one_event(self):
  o=norm(row('ctrip','ctrip-order','a',{'amount':'200','merchant':'携程','title':'某酒店','status':'已完成','occurred':'2021-04-02 18:00:00','id':'ctrip:order:3'}),
   row('wechat','payments','b',{**WX,'交易时间':'2021-04-04 16:00:00','交易对方':'某酒店管理公司','金额(元)':200}))
  ev,_=R.resolve(o);self.assertEqual(len(ev),1)

 def test_date_only_pay_later_order_finds_its_payment_days_later(self):
  o=norm(row('ctrip','ctrip-order','a',{'amount':'100','merchant':'携程','title':'某酒店','status':'已完成','occurred':'2022-10-01','id':'ctrip:order:1'}),
   ali('alipay','b',{'trade_no':'t','amount':'100','direction':'out','counterparty':'携程旅行网','item':'某酒店','paid_at':'2022-10-05T12:00:00+08:00'}))
  ev,_=R.resolve(o);self.assertEqual(len(ev),1)

 def test_latest_snapshot_of_a_record_wins(self):
  old=row('ctrip','ctrip-order','k',{'amount':'100','merchant':'携程','title':'某酒店','status':'未支付','occurred':'2022-10-01 09:00:00','id':'ctrip:order:1'})
  new=row('ctrip','ctrip-order','k',{**old[1],'status':'已完成'})
  got,*_=R.load(DB([old,new]));self.assertEqual(len(got),1)

 def test_bank_notice_does_not_join_a_wechat_balance_payment(self):
  o=norm(row('wechat','payments','a',WX),row('bank_sms','bocom-debit_txn','b',{'bank':'bocom','card':'1111','amount':'100','flow':'out','desc':'支付宝某店','occurred':'2022-09-01 12:01'}))
  ev,un=R.resolve(o,{'wechat':dt.datetime(2022,9,30,tzinfo=dt.timezone.utc),'bocom:1111':dt.datetime(2022,9,30,tzinfo=dt.timezone.utc)})
  self.assertEqual(len(un['signal']),1)

 def test_date_only_screen_rows_dedupe_across_snapshots(self):
  r={'app':'bocom','section':'card','date':'2022-10-01','time':'','raw':'10-1 某店 100','repeat':1}
  self.assertIsNotNone(phone.identity('bocom-card',r))

 def test_withdrawal_fee_is_booked(self):
  o=norm(row('wechat','payments','a',{**WX,'交易类型':'零钱提现','备注':'服务费¥0.50','收/支':'/','金额(元)':1000.50,'交易对方':'银行'}))
  ev,un=R.resolve(o);self.assertEqual(report(ev,un,o)['books']['personal']['fee_out'],'0.50')

 def test_signal_only_transfer_that_came_back_nets_to_zero(self):
  base={'kind':'transfer','amount':'100','transfer_id':'T1','chat':'c'}
  o=norm(row('wechat_notices','transfer','a',{**base,'stage':'sent','direction':'outgoing','occurred':'2022-10-20T10:00:00+08:00'}),
   row('wechat_notices','transfer','b',{**base,'stage':'returned','direction':'incoming','flow':'in','occurred':'2022-10-21T10:00:00+08:00'}))
  ev,un=R.resolve(o,{});self.assertEqual(len(ev),1);self.assertTrue(ev[0].get('refunded'))
  self.assertNotIn('p2p_out',report(ev,un,o)['books'].get('personal',{}))

 def test_override_on_a_linked_order_number_applies(self):
  _env.isolated(overrides={'taobao:9':{'book':'business','project':'shop'}})
  o=norm(ali('alipay','a',{'trade_no':'t','merchant_order_no':'T200P9','amount':'50','direction':'out','counterparty':'淘宝店','item':'货','paid_at':'2022-10-01T10:00:00+08:00'}))
  o[0]['ids']-={'taobao:9'};o+=norm(row('taobao','taobao-order','b',{'amount':'50','merchant':'淘宝店','title':'货','status':'交易成功','occurred':'2022-10-01 09:59:00','id':'taobao:order:9'}))
  ev,_=R.resolve(o);self.assertEqual((ev[0]['book'],ev[0]['project']),('business','shop'))

 def test_iso_time_with_fraction_and_utc(self):
  at,has=when('2022-10-01T12:34:56.123Z');self.assertTrue(has);self.assertEqual(at.strftime('%H:%M:%S'),'20:34:56')

 def test_overlapping_exchange_exports_count_once(self):
  c=_env.isolated();raw=Path(c.home)/'raw'/'binance'
  raw.mkdir(parents=True)
  head='用户 ID,时间,账户,操作,币种,变更,备注\n';line='1,2022-01-02 10:00:00,USD-M Futures,Realized Profit and Loss,USDT,-50,\n'
  (raw/'a.csv').write_text(head+line);(raw/'b.csv').write_text(head+line+line.replace('10:00:00','11:00:00'))
  db=connect(c.db);ingest(db,c.raw);from pfin.invest import summary
  self.assertEqual(summary(db)['binance']['result_usd_stable'],'-100.00')
