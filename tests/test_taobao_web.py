# All record values below are invented test data.
import json,sys,tempfile,unittest
from pathlib import Path
import _env
from pfin.sources.taobao_web import orders
from pfin.store import connect,ingest,summary
def page(oid,ts,shop,title,item_fee,pay_key=None,pay_fee=None):
 d={f'MainGroup_{oid}':{'fields':{'orderId':oid,'createOrderTime':str(ts),'orderStatus':'6_3','archive':'true'}},
    f'Main_{oid}':{'fields':{'orderType':'sg'}},
    f'sellerInfo_{oid}':{'fields':{'seller':{'shopName':shop},'status':{'text':'交易成功'}}},
    f'item_{oid}_1_1':{'fields':{'item':{'title':title,'quantity':'1','priceInfo':{'actualTotalFee':item_fee}}}}}
 if pay_key:d[pay_key]={'fields':{'actualFee':{'prefix':'实付款','value':pay_fee}}}
 return {'data':d}
class TaobaoWebTests(unittest.TestCase):
 def test_paid_from_pay_component_with_suffix_and_time_in_beijing(self):
  r=orders([page('1',1640995200000,'某店','示例商品','¥100.00','pay_1/0','¥100.00')])[0]
  self.assertEqual((r['order_id'],r['occurred'],r['shop'],r['paid'],r['status']),('1','2022-01-01 08:00:00','某店','100.00','交易成功'))
 def test_missing_paid_stays_missing_and_repeat_order_counts_once(self):
  p=page('2',1641081600000,'某店','示例商品','¥20.00')
  r=orders([p,p])
  self.assertEqual(len(r),1);self.assertIsNone(r[0]['paid'])
 def test_ingest_only_final_file_and_dedupes_by_order_id(self):
  with tempfile.TemporaryDirectory() as t:
   raw=Path(t)/'raw';(raw/'taobao_web'/'partial').mkdir(parents=True)
   body={'kind':'boughtlist_mtop','pages':[page('3',1641081600000,'a','b','¥1','pay_3','¥1')]}
   (raw/'taobao_web'/'boughtlist-a.json').write_text(json.dumps(body))
   (raw/'taobao_web'/'boughtlist-b.json').write_text(json.dumps(body|{'note':'second save'}))
   (raw/'taobao_web'/'partial'/'boughtlist-p.json').write_text(json.dumps(body|{'kind':'progress'}))
   db=connect(Path(t)/'i.db');ingest(db,raw)
   g=[(x['dataset'],x['observations'],x['identities']) for x in summary(db)['groups'] if x['source']=='taobao_web']
   self.assertEqual(g,[('orders',2,1)])
if __name__=='__main__':unittest.main()
