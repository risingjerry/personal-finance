import json,sqlite3,sys,tempfile,unittest
from pathlib import Path
import _env
from pfin.collectors.bank_sms import body,parse,run
from pfin.store import connect,ingest,summary
R='2026-10-20 12:00:00'
p=lambda t,r=R:parse({'text':t,'received':r})
def chat_db(path,messages):
 db=sqlite3.connect(path)
 db.executescript('CREATE TABLE handle(ROWID INTEGER PRIMARY KEY,id TEXT);CREATE TABLE message(ROWID INTEGER PRIMARY KEY,guid TEXT,handle_id INTEGER,date INTEGER,text TEXT,attributedBody BLOB,is_from_me INTEGER)')
 db.executemany('INSERT INTO handle VALUES (?,?)',[(1,'95559'),(2,'10690000'),(3,'1069000095555')])
 for i,(h,t,b) in enumerate(messages,1):db.execute('INSERT INTO message VALUES (?,?,?,?,?,?,0)',(i,f'G{i}',h,(1792000000-978307200)*10**9,t,b))
 db.commit();db.close()
class BankSmsTests(unittest.TestCase):
 def test_debit_payment_and_transfer_in(self):
  d=p('您尾号*1111的卡于10月19日21:03在支付（财付通）微信支付快捷支付36.50元，交易后余额为1,200.00元。【交通银行】')
  self.assertEqual((d['kind'],d['card'],d['occurred'],d['amount'],d['flow'],d['balance']),('debit_txn','1111','2026-10-19 21:03','36.50','out','1200.00'))
  i=p('贵账户*1111于2026年10月18日09:00在网银转入资金500.00元，现余额1700.00元，对方户名：张三，附言：转账【交通银行】')
  self.assertEqual((i['flow'],i['amount'],i['counterparty']),('in','500.00','张三'))
 def test_refund_is_in_and_ambiguous_repayment_is_unknown(self):
  self.assertEqual(p('您尾号*1111的卡于10月02日10:00在支付宝-消费退款8.00元，交易后余额为10.00元。【交通银行】')['flow'],'in')
  self.assertIsNone(p('您尾号*1111的卡于10月02日10:00其他渠道转账还款8.00元,交易后余额为10.00元。【交通银行】')['flow'])
 def test_year_rolls_back_across_new_year(self):
  self.assertEqual(p('【交通银行】您尾号*1111的卡12月31日23:50在网上支付1.00元，现余额2.00元','2027-01-01 00:01:00')['occurred'],'2026-12-31 23:50')
 def test_credit_card_spend_takes_month_from_receipt(self):
  d=p('您尾号2222交行信用卡19日21时05分成功消费人民币88.00元，当前可用额度为人民币100.00元。【交通银行】')
  self.assertEqual((d['kind'],d['card'],d['occurred'],d['amount'],d['flow']),('credit_txn','2222','2026-10-19 21:05','88.00','out'))
 def test_unknown_text_is_not_guessed(self):
  self.assertEqual(p('【交通银行】买单吧还款红包月月领，每月领取5-10元'),{'kind':'other'})
 def test_attributed_body_text(self):
  self.assertEqual(body(None,b'\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84\x12NSAttributedString\x00\x84\x84\x08NSObject\x00\x85\x92\x84\x84\x84\x08NSString\x01\x94\x84\x01+\x06abc\xe5\x85\x83'),'abc元')
 def test_codes_ads_and_other_senders_never_saved_and_rerun_dedupes(self):
  with tempfile.TemporaryDirectory() as t:
   t=Path(t);chat=t/'chat.db';home=t/'home'
   spend='您尾号*1111的卡于10月19日21:03在网上支付1.00元，交易后余额为2.00元。【交通银行】'
   chat_db(chat,[(1,spend,None),(1,'【交通银行】动态密码123456，余额查询',None),(1,'【交通银行】立减5元活动',None),(2,'您的余额10.00元',None),(3,'【招商银行】验证码654321',None)])
   self.assertEqual(run(home,chat),{'new_messages':1})
   self.assertEqual(run(home,chat),{'new_messages':0})
   saved=json.loads(next((home/'raw'/'bank_sms').glob('*.json')).read_text())
   self.assertNotIn('123456',json.dumps(saved,ensure_ascii=False));self.assertEqual([m['guid'] for m in saved['messages']],['G1'])
   # The same message in a second raw file stays one record.
   (home/'raw'/'bank_sms'/'sms-dup.json').write_text(json.dumps(saved,ensure_ascii=False))
   db=connect(t/'i.db');ingest(db,home/'raw')
   g=[x for x in summary(db)['groups'] if x['source']=='bank_sms']
   self.assertEqual([(x['dataset'],x['observations'],x['identities']) for x in g],[('bocom-debit_txn',2,1)])
if __name__=='__main__':unittest.main()
