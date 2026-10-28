# All record values below are invented test data.
import json,tempfile,unittest
from pathlib import Path
import _env
from connectors.alipay import rows as alipay_rows
from pfin.store import connect,ingest,summary
def groups(db,source):return [(x['dataset'],x['observations'],x['identities']) for x in summary(db)['groups'] if x['source']==source]
class ConnectorRowsTests(unittest.TestCase):
 def setUp(self):_env.isolated()
 def test_rows_dedupe_across_runs_by_source_type_and_id(self):
  with tempfile.TemporaryDirectory() as t:
   raw=Path(t)/'raw';(raw/'jd').mkdir(parents=True)
   row={'source':'jd','record_type':'order','id':'1','occurred':'2022-01-02 10:00:00','amount':'9.90','direction':'out','merchant':'京东','title':'示例商品','status':'已完成','extra':{}}
   (raw/'jd'/'jd-rows-a.jsonl').write_text(json.dumps(row)+'\n')
   (raw/'jd'/'jd-rows-b.jsonl').write_text(json.dumps(row)+'\n'+json.dumps({**row,'id':'2'})+'\n')
   (raw/'jd'/'jd-orders-a.json').write_text('{"kind":"jd_pages","pages":[]}')  # the raw payload is evidence, not parsed
   db=connect(Path(t)/'i.db');ingest(db,raw)
   self.assertEqual(groups(db,'jd'),[('jd-order',3,2)])
 def test_alipay_statements_dedupe_by_trade_no_across_snapshots(self):
  with tempfile.TemporaryDirectory() as t:
   raw=Path(t)/'raw';(raw/'alipay').mkdir(parents=True)
   rec={'trade_no':'DEMO-0001','created_at':'2022-10-26T12:00:00+08:00','amount':'300.00','direction':'out'}
   write=lambda name,recs:(raw/'alipay'/name).write_text(''.join(json.dumps(r)+'\n' for r in alipay_rows({'kind':'alipay_bills','records':recs})))
   write('alipay-rows-a.jsonl',[rec]);write('alipay-rows-b.jsonl',[rec,{**rec,'trade_no':'DEMO-0001_000002'}])
   db=connect(Path(t)/'i.db');ingest(db,raw)
   self.assertEqual(groups(db,'alipay'),[('alipay-transaction',3,2)])
 def test_exchange_folder_also_reads_its_connector_rows(self):
  with tempfile.TemporaryDirectory() as t:
   raw=Path(t)/'raw';(raw/'okx').mkdir(parents=True)
   row={'source':'okx','record_type':'order','id':'okx:c2c:1','occurred':'2022-01-02 10:00:00','amount':'700.00','direction':'out','merchant':'李四','title':'OKX C2C 买入','status':'completed','extra':{}}
   (raw/'okx'/'okx-rows-a.jsonl').write_text(json.dumps({**row,'source':'okx-c2c'})+'\n')
   db=connect(Path(t)/'i.db');ingest(db,raw)
   self.assertEqual(groups(db,'okx'),[('okx-c2c-order',1,1)])
if __name__=='__main__':unittest.main()
