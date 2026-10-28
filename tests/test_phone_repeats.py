# All record values below are invented test data.
import json,sys,tempfile,unittest
from pathlib import Path
import _env
from pfin.store import connect,ingest,summary
class PhoneRepeatTests(unittest.TestCase):
 def test_identical_rows_stay_separate_but_rereads_dedupe(self):
  with tempfile.TemporaryDirectory() as t:
   raw=Path(t)/'raw';(raw/'phone').mkdir(parents=True)
   r={'app':'wechat','section':'bill','date':'2021-08-22','time':'12:00','amount':'+1.00','raw':'微信红包-来自张三,8月22日12点00分,收入1.00元'}
   other={**r,'time':'12:01','raw':'x'}
   body=''.join(json.dumps(x,ensure_ascii=False)+'\n' for x in (r,r,r,other))
   (raw/'phone'/'wechat-bill-1.jsonl').write_text(body)
   # A later snapshot of the same list (one more row appended) must not double the repeats.
   (raw/'phone'/'wechat-bill-2.jsonl').write_text(body+json.dumps({**r,'time':'12:02','raw':'y'})+'\n')
   db=connect(Path(t)/'i.db');ingest(db,raw)
   g=[(x['observations'],x['identities']) for x in summary(db)['groups'] if x['source']=='phone']
   self.assertEqual(g,[(9,5)])
if __name__=='__main__':unittest.main()
