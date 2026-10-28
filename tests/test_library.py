import json,sys,tempfile,unittest
from pathlib import Path
import _env
from pfin.store import connect,ingest
from pfin.library import build,place
class LibraryTests(unittest.TestCase):
 def test_layout(self):
  self.assertEqual(place('phone','bocom-card'),'credit/bocom-credit/app-bill')
  self.assertEqual(place('phone','meituan-orders'),'orders/meituan/app-orders')
  self.assertEqual(place('okx','fills'),'investing/okx/fills')
  self.assertTrue(place('new_source','x').startswith('unsorted/'))
 def test_build_dedupes_and_keeps_provenance(self):
  with tempfile.TemporaryDirectory() as t:
   home=Path(t);(home/'raw'/'phone').mkdir(parents=True)
   row={'app':'alipay','section':'bill','date':'2026-01-02','time':'10:00','amount':'-1.00','raw':'a'}
   for n in (1,2):(home/'raw'/'phone'/f'alipay-bill-{n}.jsonl').write_text(json.dumps(row)+'\n'+('' if n==1 else json.dumps({**row,'time':'09:00','raw':'b'})+'\n'))
   db=connect(home/'evidence.sqlite');ingest(db,home/'raw');db.close()
   self.assertEqual(build(home)['records'],2)
   lines=[json.loads(l) for l in (home/'library/payments/alipay/app-bill.jsonl').read_text().splitlines()]
   self.assertEqual([l['occurred'] for l in lines],['2026-01-02 09:00','2026-01-02 10:00'])
   self.assertTrue(lines[1]['from'].startswith('raw/phone/alipay-bill-'))
if __name__=='__main__':unittest.main()
