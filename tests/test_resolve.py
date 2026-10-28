# All record values below are invented test data.
import datetime as dt,json,unittest
from decimal import Decimal
import _env
from connectors.alipay import rows as alipay_rows
from pfin import resolve as R
from pfin.rules import loan_interest

def row(source,dataset,key,payload):return {'source':source,'dataset':dataset,'record_key':key},payload
def ali(folder,key,record,merchant=False):
 """An Alipay statement / merchant-funds record as the alipay connector writes it."""
 r=alipay_rows({'kind':'alipay_merchant_funds' if merchant else 'alipay_bills','records':[record]})[0]
 return row(folder,r['source']+'-transaction',key,r)
def norm(*rows):return [o for o in (R.prepare(r,p) for r,p in rows) if o]
# Invented rules for testing; not an account configuration.
RULES=[{'when':{'account':'alipay:work','field':{'merchant_order_no':'^[0-9a-f]{32}$'}},'book':'business','project':'supplier'},
 {'when':{'account':'alipay:work','field':{'merchant_order_no':'^SHOP'}},'book':'business','project':'shop'},
 {'when':{'dataset':'okx-c2c-order'},'book':'business','project':'supplier'},
 {'when':{'text':'某某借'},'book':'personal','kind':'internal','loan':{'principal':'10000.00','payments':['880.00']*11+['880.12'],'first':'2022-10'}}]

WX={'交易单号':'0000000001','交易对方':'美团','交易时间':'2021-09-01 12:00:20','交易类型':'商户消费','商品':'示例商品','商户单号':'m1','备注':'/','当前状态':'支付成功','支付方式':'交通银行信用卡(2222)','收/支':'支出','金额(元)':88.80}
class ResolveTests(unittest.TestCase):
 def setUp(self):_env.isolated(owner=['张三','某店'],rules=RULES,accounts={'alipay:work':{'book':'business'}},connectors={'alipay-work':{'account':'alipay:work'},'alipay-merchant-work':{'account':'alipay:work'}})
 def test_one_purchase_seen_three_times_is_one_event(self):
  o=norm(row('wechat','payments','a',WX),
   row('phone','bocom-card','b',{'amount':'88.80','status':'消费','occurred':'2021-09-01 12:00:29','title':'财付通-美团','raw':'9-1，财付通-美团，12:00:29，消费，¥88.80'}),
   row('wechat_notices','payment','c',{'kind':'payment','amount':'88.80','occurred':'2021-09-01T12:00:30+08:00','merchant':'美团','method':'交通银行信用卡(2222)'}))
  ev,un=R.resolve(o)
  self.assertEqual(len(ev),1);self.assertEqual(ev[0]['kind'],'purchase');self.assertEqual(len(ev[0]['links']),3)
 def test_change_payment_does_not_pull_in_a_bank_row(self):
  o=norm(row('wechat','payments','a',{**WX,'支付方式':'零钱'}),
   row('phone','bocom_bank-account','b',{'amount':'-88.80','occurred':'2021-09-01 12:00:29','title':'财付通','raw':'财付通支出88.80'}))
  ev,_=R.resolve(o);self.assertEqual(len(ev),2)
 def test_two_equal_parking_fees_stay_two_events(self):
  p={**WX,'交易对方':'某停车场','金额(元)':6,'支付方式':'零钱'}
  o=norm(row('wechat','payments','a',p),row('wechat','payments','b',{**p,'交易单号':'0000000002','交易时间':'2021-09-01 12:02:00'}),
   row('wechat_notices','payment','c',{'kind':'payment','amount':'6','occurred':'2021-09-01T12:00:21+08:00'}),
   row('wechat_notices','payment','d',{'kind':'payment','amount':'6','occurred':'2021-09-01T12:02:01+08:00'}))
  ev,un=R.resolve(o);self.assertEqual([len(e['links']) for e in ev],[2,2]);self.assertFalse(un['signal'])
 def test_withdrawal_fee_and_bank_leg(self):
  o=norm(row('wechat','payments','a',{'交易单号':'0000000003','交易对方':'交通银行(1111)','交易时间':'2022-09-27 10:00:00','交易类型':'零钱提现','商品':'/','备注':'服务费¥0.50','当前状态':'提现已到账','支付方式':'交通银行储蓄卡(1111)','收/支':'/','金额(元)':1000.50}),
   row('phone','bocom_bank-account','b',{'amount':'+1000.00','occurred':'2022-09-27 10:00:01','title':'网上银行卡转入','raw':'网上银行卡转入收入1,000.00'}))
  ev,_=R.resolve(o);self.assertEqual(len(ev),1);self.assertEqual(ev[0]['kind'],'internal');self.assertEqual(str(ev[0]['anchor']['amount']),'1000.00')
 def test_old_okx_web_export_is_superseded(self):
  self.assertIsNone(R.prepare({'source':'okx','dataset':'c2c','record_key':'b'},{'状态':'交易完成','资金类型':'CNY','交易类型':'买入','创建时间':'2022-09-09 12:00:00','金额':'2000.00'}))
 def test_own_card_to_card_move_is_one_internal_event(self):
  o=norm(row('phone','abc_bank-account','a',{'amount':'-500.00','occurred':'2022-09-09 20:00:00','title':'转支','raw':'尾号3333 转支 -500.00'}),
   row('phone','bocom_bank-account','b',{'amount':'+500.00','occurred':'2022-09-09 20:00:01','title':'转账-张三(**3333)','raw':'转账-张三(**3333)收入500.00'}))
  ev,_=R.resolve(o);self.assertEqual(len(ev),1);self.assertEqual(ev[0]['kind'],'internal')
 def test_returned_transfer_nets_to_zero(self):
  base={'交易对方':'某人','商品':'转账备注:微信转账','商户单号':'/','备注':'/','支付方式':'零钱'}
  o=norm(row('wechat','payments','a',{**base,'交易单号':'1','交易时间':'2022-09-08 12:00:00','交易类型':'转账','当前状态':'对方已退还','收/支':'支出','金额(元)':2000}),
   row('wechat','payments','b',{**base,'交易对方':'/','交易单号':'2','交易时间':'2022-09-08 12:39:00','交易类型':'转账-退款','当前状态':'已全额退款','收/支':'收入','金额(元)':2000}))
  ev,_=R.resolve(o);self.assertEqual(len(ev),1);self.assertTrue(ev[0].get('refunded'))
 def test_work_alipay_order_prefix_and_fee_follow_project(self):
  base={'direction':'in','status':'交易成功','counterparty':'**三','fee':'0.00'}
  o=norm(ali('alipay-work','a',{**base,'trade_no':'T1','merchant_order_no':'SHOP000000000000000001','item':'订单1','amount':'100.00','paid_at':'2022-09-01T00:00:00+08:00'}),
   ali('alipay-work','b',{**base,'trade_no':'T2','merchant_order_no':'00000000000000000000000000000001','item':'订单2','amount':'125.00','paid_at':'2022-09-01T23:00:00+08:00'}),
   ali('alipay-work','c',{**base,'direction':'out','trade_no':'T3','merchant_order_no':'T1','counterparty':'支付宝','item':'服务费[T1]','amount':'0.60','paid_at':'2022-09-01T00:00:01+08:00'}),
   ali('alipay-work','d',{**base,'direction':'none','status':'交易关闭','trade_no':'T4','merchant_order_no':'SHOP000000000000000002','item':'订单4','amount':'5.00','paid_at':'2022-09-01T01:00:00+08:00'}))
  ev,_=R.resolve(o);by={e['anchor']['title']:e for e in ev};by={'a':by['订单1'],'b':by['订单2'],'fee':by['服务费[T1]']}
  self.assertEqual(len(ev),3)
  self.assertEqual(by['a']['project'],'shop');self.assertEqual(by['b']['project'],'supplier')
  self.assertEqual((by['fee']['kind'],by['fee']['project']),('fee','shop'))
 def test_okx_sale_paid_rounded_up_still_links(self):
  o=norm(ali('alipay','a',{'trade_no':'t9','amount':'2000.00','direction':'in','counterparty':'**四','item':'收钱码收款','paid_at':'2021-11-26T18:04:00+08:00'}),
   row('okx','okx-c2c-order','b',{'status':'completed','amount':'1999.50','direction':'in','merchant':'王五','title':'OKX C2C 卖出 100.00 USDT','occurred':'2021-11-26 18:00:00','extra':{'payment_method':'支付宝'}}))
  ev,un=R.resolve(o);self.assertEqual(len(ev),1);self.assertFalse(un['detail']);self.assertEqual(ev[0]['project'],'supplier')
 def test_order_links_to_the_seller_payment_not_the_own_transfer(self):
  o=norm(ali('alipay','a',{'trade_no':'t1','amount':'3000.00','direction':'in','counterparty':'某店','item':'转账','paid_at':'2022-10-01T19:00:30+08:00'}),
   ali('alipay','b',{'trade_no':'t2','amount':'3000.00','direction':'out','counterparty':'李四','item':'Transfer','paid_at':'2022-10-01T19:01:10+08:00'}),
   row('okx','okx-c2c-order','c',{'status':'completed','amount':'3000.00','direction':'out','merchant':'李四','title':'OKX C2C 买入','occurred':'2022-10-01 19:00:08','extra':{'payment_method':'支付宝'}}))
  ev,un=R.resolve(o);self.assertFalse(un['detail'])
  okx=[e for e in ev if any(l[0]['dataset']=='okx-c2c-order' for l in e['links'])]
  self.assertEqual(okx[0]['anchor']['counterparty'],'李四')
 def test_merchant_funds_only_fill_days_after_the_work_export(self):
  bill=ali('alipay-work','a',{'trade_no':'T1','amount':'10.00','direction':'in','status':'交易成功','counterparty':'**三','item':'订单1','paid_at':'2022-10-01T21:00:00+08:00'})
  fund=lambda k,at,bid:ali('alipay-merchant-work',k,{'occurred_at':at,'income':'10.00','expense':'0.00','counterparty':'**三','business_id':bid,'order_id':'SHOP000000000000000002','type':'交易','raw':{'备注':''}},merchant=True)
  got,*_=R.load(_FakeDB([bill,fund('b','2022-10-01 21:00:00','T1'),fund('c','2022-10-02 09:00:00','T2')]),dt.date(2022,10,1),dt.date(2022,10,2))
  self.assertEqual(sorted(o['key'] for o in got),['a','c'])
  ev,_=R.resolve(got);self.assertEqual({e['anchor']['key']:e['project'] for e in ev},{'a':None,'c':'shop'})
 def test_screen_read_wechat_bill_fills_only_days_before_the_export(self):
  wx=lambda k,at:row('wechat','payments',k,{**WX,'交易单号':k,'交易时间':at})
  screen=lambda k,day:row('phone','wechat-bill',k,{'amount':'-20.00','date':day,'occurred':day+' 18:01','title':'某店','raw':'某店,支出20.00元'})
  got,*_=R.load(_FakeDB([wx('a','2021-09-01 12:00:20'),wx('d','2021-09-30 10:00:00'),screen('b','2021-08-29'),screen('c','2021-09-01')]))
  self.assertEqual(sorted(o['key'] for o in got),['a','b','d'])
 def test_supplier_rule_matches_the_work_leg_of_a_test_payment_from_personal(self):
  o=norm(ali('alipay','a',{'trade_no':'t1','amount':'1.00','direction':'out','counterparty':'某店','item':'商品','paid_at':'2022-09-06T16:00:00+08:00'}),
   ali('alipay-work','b',{'trade_no':'t2','amount':'1.00','direction':'in','status':'交易成功','counterparty':'**五','item':'订单','merchant_order_no':'00000000000000000000000000000001','paid_at':'2022-09-06T16:00:00+08:00'}))
  ev,_=R.resolve(o);self.assertEqual(len(ev),1);self.assertEqual((ev[0]['kind'],ev[0]['book'],ev[0]['project']),('internal','business','supplier'))
 def test_credit_line_purchase_counts_and_its_repayment_does_not(self):
  o=norm(row('phone','douyin_yuefu-bill','a',{'amount':'100.00','date':'2022-01-03','occurred':'2022-01-03','title':'示例商品','status':'交易完成','raw':'1月3日，示例商品，100.00'}),
   row('phone','douyin_yuefu-bill','b',{'amount':'-100.00','date':'2022-02-15','occurred':'2022-02-15','title':'自动还款-2022年2月账单','status':'还款','raw':'2月15日，自动还款，-100.00，还款'}))
  ev,_=R.resolve(o);self.assertEqual(sorted(e['kind'] for e in ev),['internal','purchase'])
 def test_loan_repayment_rule_splits_interest(self):
  o=norm(row('phone','bocom_bank-account','a',{'amount':'-880.00','occurred':'2022-10-19 08:00:00','title':'代扣-某某借','raw':'尾号1111 代扣-某某借支出880.00'}))
  ev,_=R.resolve(o);self.assertEqual((ev[0]['kind'],str(ev[0]['interest'])),('internal','84.86'))
 def test_ctrip_order_links_to_its_naquhua_bill_and_the_repayment_is_internal(self):
  o=norm(row('ctrip','ctrip-order','a',{'amount':'2000.00','direction':'out','merchant':'携程','title':'某酒店','status':'已完成','occurred':'2022-10-01 00:00:00','id':'ctrip:order:1'}),
   row('ctrip','ctrip-naquhua-bill','b',{'record_type':'bill','amount':'2000.00','direction':'out','merchant':'携程','title':'某酒店','status':'计入10月账单','occurred':'2022-10-06 01:00:00','id':'n1'}),
   row('ctrip','ctrip-naquhua-transaction','c',{'record_type':'transaction','amount':'2000.00','direction':'in','merchant':'携程拿去花','title':'主动还款-2022年10月账单','status':'SUCCESS','occurred':'2022-10-27 20:00:00','id':'n2'}),
   row('phone','bocom_bank-account','d',{'amount':'-2000.00','occurred':'2022-10-27 20:00:01','title':'携程金融-还款','raw':'尾号1111 携程金融-还款支出2,000.00'}))
  ev,un=R.resolve(o);self.assertFalse(un['detail'])
  self.assertEqual(sorted((e['kind'],len(e['links'])) for e in ev),[('internal',2),('purchase',2)])
 def test_wechat_notice_after_the_export_becomes_the_event(self):
  o=norm(row('wechat','payments','a',{'交易单号':'1','交易时间':'2022-10-10 20:00:00','交易类型':'商户消费','收/支':'支出','金额(元)':5,'交易对方':'店','商品':'示例商品','当前状态':'支付成功','支付方式':'零钱','商户单号':'/','备注':'/'}),
   row('wechat_notices','payment','b',{'kind':'payment','amount':'8.00','merchant':'某店','description':'使用零钱支付¥8.00','occurred':'2022-10-21T16:00:00+08:00'}),
   row('wechat_notices','transfer','c',{'kind':'transfer','amount':'700.00','stage':'sent','direction':'incoming','transfer_id':'T9','description':'收到转账700.00元','occurred':'2022-10-24T22:00:00+08:00'}),
   row('wechat_notices','transfer','d',{'kind':'transfer','amount':'700.00','stage':'accepted','direction':'outgoing','flow':'in','transfer_id':'T9','description':'收到转账700.00元','occurred':'2022-10-24T22:00:25+08:00'}))
  ev,un=R.resolve(o);self.assertFalse(un['signal']);self.assertEqual(len(ev),3)
  t=[e for e in ev if e['anchor']['dataset']=='transfer'][0];self.assertEqual((t['anchor']['sign'],t['kind']),(1,'p2p'))
 def test_loan_instalments_split_interest_that_sums_to_the_total(self):
  L={'principal':'10000.00','payments':['880.00']*11+['880.12'],'first':'2022-10'}
  its=[loan_interest(L,dt.datetime(2022+(9+i)//12,(9+i)%12+1,19)) for i in range(12)]
  self.assertEqual(sum(its),Decimal('560.12'));self.assertGreater(its[0],its[-1]);self.assertIsNone(loan_interest(L,dt.datetime(2023,10,19)))
 def test_cash_withdrawal_and_credit_line_repayment_are_internal(self):
  o=norm(row('phone','abc_bank-account','a',{'amount':'-5000.00','occurred':'2019-02-27 12:00:00','title':'现支','raw':'尾号4444 现支 -5000.00'}),
   row('phone','bocom_bank-account','b',{'amount':'-1000.00','occurred':'2022-02-28 12:00:00','title':'携程金融-趣游保理','raw':'携程金融-趣游保理支出1,000.00'}))
  ev,_=R.resolve(o);self.assertEqual([e['kind'] for e in ev],['internal','internal'])
 def test_ctrip_order_paid_days_later_is_not_counted_twice(self):
  o=norm(row('ctrip','ctrip-order','a',{'amount':'2000.00','direction':'out','merchant':'携程','title':'某酒店','status':'已完成','occurred':'2022-02-09 00:00:00','id':'ctrip:order:2'}),
   ali('alipay','b',{'trade_no':'t3','amount':'2000.00','direction':'out','counterparty':'携程旅行网','item':'某酒店','paid_at':'2022-02-13T12:00:00+08:00'}))
  ev,un=R.resolve(o);self.assertEqual(len(ev),1);self.assertEqual(ev[0]['anchor']['account'],'alipay')
class _FakeDB:
 def __init__(self,rows):self.rows=[dict(r,payload=json.dumps(p)) for r,p in rows]
 def execute(self,_):return self.rows
if __name__=='__main__':unittest.main()
