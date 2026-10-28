# All record values below are invented test data.
import json,sys,tempfile,unittest
from pathlib import Path
import _env
from pfin.collectors.wechat_notices import parse
from pfin.store import connect,ingest,summary
from pfin.checks import check
PAY={'title':'已支付¥20.00','description':'使用零钱支付¥20.00','pay_receipt':{'merchant':'某停车场','trans_id':'0000000001'}}
class NoticeTests(unittest.TestCase):
 def test_payment_card(self):
  self.assertEqual(parse('wechat_pay',PAY),{'kind':'payment','amount':'20.00','method':'零钱','merchant':'某停车场','trans_id':'0000000001'})
 def test_daily_report_and_bank_alert(self):
  d=parse('wechat_pay',{'title':'昨日记账日报，点击查看详情','description':'昨日支出¥1,000.00，共3笔 昨日入账¥200.00，共1笔'})
  self.assertEqual((d['spent'],d['spent_count'],d['received'],d['received_count']),('1000.00',3,'200.00',1))
  b=parse('bocom_card',{'title':'交易提醒','description':'交易卡号:0000001234 交易时间:2022年01月02日 09:00 交易类型:消费 交易金额:￥88.80'})
  self.assertEqual((b['kind'],b['amount'],b['txn_type'],b['card']),('card_alert','88.80','消费','1234'))
 def test_password_free_deduction_is_payment(self):
  d=parse('wechat_pay',{'title':'已扣费¥20.00','description':'通过零钱扣款￥20.00 扣费服务某服务','pay_receipt':{'merchant':'某服务','trans_id':'0000000002'}})
  self.assertEqual((d['kind'],d['amount'],d['method'],d['merchant'],d['deduction']),('payment','20.00','零钱','某服务',True))
 def test_transfer_counts_money_once_per_side(self):
  t=lambda stage,direction:parse('wechat_chat',{'subtype':2000,'direction':direction,'transfer':{'stage':stage,'amount':'150.00','transfer_id':'T1'}})['flow']
  self.assertEqual([t('sent','outgoing'),t('accepted','incoming'),t('sent','incoming'),t('accepted','outgoing'),t('returned','incoming'),t('returned','outgoing')],['out',None,None,'in','in',None])
  self.assertEqual(parse('wechat_chat',{'subtype':2001,'direction':'outgoing'}),{'kind':'red_packet','flow':'out','amount':None})
 def test_unknown_card_is_not_guessed(self):
  # Promotions are recognised as info and never carry an amount.
  self.assertEqual(parse('wechat_pay',{'title':'活动推荐','description':'领¥5红包'}),{'kind':'info'})
 def test_same_receipt_from_two_runs_is_one_record(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t)/'raw';(root/'wechat_notices').mkdir(parents=True);db=connect(Path(t)/'i.db')
   card={'chat':'gh_x','channel':'wechat_pay','server_id':'1','create_time':1,'occurred':'2022-10-24T00:00:00+08:00',**PAY}
   for n,cards in [('a',[card]),('b',[card,{**card,'server_id':'2'}])]:
    (root/'wechat_notices'/f'notices-{n}.json').write_text(json.dumps({'count':len(cards),'cards':cards}))
   ingest(db,root);g=summary(db)['groups'][0];db.close()
   self.assertEqual((g['dataset'],g['observations'],g['identities'],g['weak_identity_rows']),('payment',3,1,0))
 def test_daily_check_explains_gap_only_by_red_packet_count(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t)/'raw';(root/'wechat_notices').mkdir(parents=True);db=connect(Path(t)/'i.db')
   base={'chat':'gh_demo_0001','channel':'wechat_pay','create_time':1}
   cards=[{**base,'server_id':'1','occurred':'2022-10-01T10:00:00+08:00',**PAY},
          {'chat':'wxid_p','channel':'wechat_chat','server_id':'2','create_time':1,'occurred':'2022-10-01T11:00:00+08:00','subtype':2000,'direction':'outgoing','transfer':{'stage':'sent','amount':'100.00','transfer_id':'T1'}},
          {'chat':'g@chatroom','channel':'wechat_chat','server_id':'3','create_time':1,'occurred':'2022-10-01T12:00:00+08:00','subtype':2001,'direction':'outgoing'},
          {**base,'server_id':'4','occurred':'2022-10-02T12:00:00+08:00','title':'昨日记账日报','description':'昨日支出¥150.00，共3笔'},
          {**base,'server_id':'5','occurred':'2022-10-03T12:00:00+08:00','title':'昨日记账日报','description':'昨日支出¥9.00，共1笔'}]
   (root/'wechat_notices'/'notices-a.json').write_text(json.dumps({'count':len(cards),'cards':cards}));ingest(db,root)
   days={d['day']:d for d in check(db)};db.close()
   self.assertEqual((days['2022-10-01']['status'],days['2022-10-01']['gap_amount']),('gap_is_red_packets','30.00'))
   self.assertEqual(days['2022-10-02']['status'],'unexplained')
if __name__=='__main__':unittest.main()


class MoreCardKindsTests(unittest.TestCase):
    def test_english_paid_and_deducted_cards_are_payments(self):
        from pfin.collectors.wechat_notices import parse
        p=parse('wechat_pay',{'title':'¥100.00 paid','description':'Paid by COMM Credit Card (2222)¥100.00 Discount','pay_receipt':{'merchant':'M','trans_id':'0000000003'}})
        self.assertEqual((p['kind'],p['amount'],p['method'],p['trans_id']),('payment','100.00','COMM Credit Card (2222)','0000000003'))
        d=parse('wechat_pay',{'title':'Deducted: ¥6.00','description':'Deducted by Balance￥6.00 Service示例商品','pay_receipt':{}})
        self.assertEqual((d['kind'],d['amount'],d['method'],d['deduction']),('payment','6.00','Balance',True))
    def test_refunds_withdrawals_and_repayments(self):
        from pfin.collectors.wechat_notices import parse
        r=parse('wechat_pay',{'title':'退款到账通知','description':'退款金额¥2000.00 商品详情示例商品 商户名称某店 退款方式退回支付卡(交通银行2222) 到账时间2021年1月1日 12:00:00'})
        self.assertEqual((r['kind'],r['amount'],r['merchant']),('refund','2000.00','某店'))
        w=parse('wechat_pay',{'title':'零钱提现已到账','description':'提现金额￥1000.00 提现银行交通银行(1111) 提现时间2022-01-01 10:00:00'})
        self.assertEqual((w['kind'],w['stage'],w['amount']),('withdrawal','arrived','1000.00'))
        k=parse('bocom_card',{'title':'还款成功提醒','description':'卡片尾号:2222 还款时间:2022年01月01日 10:10:00 实际还款金额:￥800.00 人民币未还:￥0.00'})
        self.assertEqual((k['kind'],k['card'],k['amount'],k['remaining']),('credit_repayment','2222','800.00','0.00'))
    def test_marketing_is_info_and_unknown_stays_other(self):
        from pfin.collectors.wechat_notices import parse
        self.assertEqual(parse('bocom_card',{'title':'【最红星期五】消费达标直通5折！','description':'福利'})['kind'],'info')
        self.assertEqual(parse('wechat_pay',{'title':'完全没见过的标题','description':''})['kind'],'other')
