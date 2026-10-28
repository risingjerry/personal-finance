import json,unittest
import _env
from pfin.store import connect,add
from pfin.invest import summary


class InvestTests(unittest.TestCase):
 def test_flows_are_categorized_and_only_results_count(self):
  c=_env.isolated();db=connect(c.db)
  db.execute("INSERT INTO documents VALUES ('d','x','binance',1)")
  rows=[('binance','account_ledger',{'操作':'Deposit','币种':'USDT','变更':'1000','时间':'2026-01-01 10:00:00'}),
   ('binance','account_ledger',{'操作':'Realized Profit and Loss','币种':'USDT','变更':'-50','时间':'2026-01-02 10:00:00'}),
   ('binance','account_ledger',{'操作':'Fee','币种':'USDT','变更':'-2','时间':'2026-01-02 10:00:00'}),
   ('binance','account_ledger',{'操作':'Transfer Between Spot and Funding','币种':'USDT','变更':'300','时间':'2026-01-03 10:00:00'}),
   ('okx','funding_bills',{'类型':'完成订单','币种':'USDT','数量':'280','时间':'2026-01-04 10:00:00','id':'1'}),
   ('okx','trading_bills',{'账单类型':'永续合约','交易类型':'资金费支出','交易账户余额变动':'-1.5','交易账户余额单位':'USDT','时间':'2026-01-05 10:00:00','id':'2'})]
  for i,(s,d,r) in enumerate(rows):add(db,'d',f'r{i}',s,d,r)
  db.commit();out=summary(db)
  self.assertEqual(out['binance']['result_usd_stable'],'-52.00')
  self.assertEqual(out['binance']['stablecoins']['USDT']['internal'],'300')
  self.assertEqual((out['okx']['stablecoins']['USDT']['c2c'],out['okx']['result_usd_stable']),('280','-1.50'))
