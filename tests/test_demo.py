import contextlib,io,unittest
import _env
from pfin import demo
class DemoTests(unittest.TestCase):
 def test_six_records_become_four_events(self):
  with contextlib.redirect_stdout(io.StringIO()):events=demo.run()
  self.assertEqual(sorted((e['kind'],str(e['anchor']['amount']),len(e['links'])) for e in events),
   [('internal','300.00',1),('p2p','200.00',1),('purchase','129.00',2),('purchase','420.00',2)])
 def tearDown(self):_env.isolated()
if __name__=='__main__':unittest.main()
