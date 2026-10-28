"""Investing book from exchange records (raw/binance, raw/okx, raw/bitget): flows per exchange and coin.

Every balance-change row is put in one category:
  deposit / withdraw   coins moved onto / off the exchange (on-chain)
  c2c                  coins bought or sold for fiat on the exchange's C2C market
  futures              realized result of perpetual / futures trading (and event contracts), fees included where netted
  funding              funding-rate payments on perpetuals
  fee                  trading fees the exchange lists separately
  earn                 interest and rewards (savings, staking, airdrop-style rewards)
  spot                 spot / Alpha trade legs (a buy is one coin out and another in; not valued)
  internal             moves between the exchange's own accounts, savings subscribe/redeem, freezes
  other                anything not recognised (kept, so totals stay complete)
Nothing is priced: amounts stay in their own coin. 'result' sums futures, funding, fee and earn in USD stablecoins.
"""
import collections,json
from decimal import Decimal,InvalidOperation
from .store import connect

STABLE={'USDT','USDC','FDUSD','USD'}


def dec(v):
 try:return Decimal(str(v).replace(',','').strip())
 except (InvalidOperation,AttributeError):return None


def binance(dataset,r):
 if dataset!='account_ledger':return
 op=r.get('操作') or '';acct=r.get('账户') or ''
 if op=='Deposit':c='deposit'
 elif op=='Withdraw':c='withdraw'
 elif op=='Realized Profit and Loss':c='futures'
 elif op=='Funding Fee':c='funding'
 elif op in ('Fee','Transaction Fee') or 'Fee' in op and 'Funding' not in op:c='fee'
 elif 'Interest' in op or 'Distribution' in op or 'Airdrop' in op or 'Reward' in op:c='earn'
 elif 'P2P' in op or 'C2C' in op:c='c2c'
 elif op.startswith('Event Contracts'):c='futures'  # prediction contracts: stake out, payout in
 elif op.startswith(('Transaction','Alpha','CEX Swap','DEX Swap','Sell Web3','Binance Convert','Small Assets')):c='spot'
 elif 'Transfer' in op or 'Subscription' in op or 'Redemption' in op or 'Freeze' in op or 'Unfreeze' in op or 'Refund' in op:c='internal'
 else:c='other'
 yield r.get('时间'),r.get('币种'),dec(r.get('变更')),c


BITGET_FUTURES={'contract_main_settle_fee':'funding','contract_margin_settle_fee':'funding'}
def bitget(dataset,r):
 d=dataset.strip();t=r.get('类型') or ''
 if d.endswith('合约资金流水'):
  if t.startswith(('trans_','risk_captital')):c='internal'
  else:c=BITGET_FUTURES.get(t,'futures' if t.startswith(('open_','close_','burst_')) else 'other')
  yield r.get('时间'),r.get('币种'),dec(r.get('总额')),c
 elif d=='现货资金流水':
  c={'Deposit':'deposit','Ordinary Withdrawal':'withdraw','Interest':'earn','Rebate':'earn','Rebate rewards':'earn','Gains':'earn',
     'Buy':'spot','Sell':'spot','Transfer in':'internal','Transfer out':'internal','Financial':'internal'}.get(t)
  if c is None:c='spot' if 'Onchain' in t and ('Buy' in t or 'Sell' in t) else 'fee' if 'fee' in t.lower() else 'other'
  yield r.get('时间'),r.get('币种'),dec(r.get('数量')),c
  if c in ('spot',) and dec(r.get('手续费')):yield r.get('时间'),r.get('币种'),-abs(dec(r.get('手续费'))),'fee'
 elif d in ('逐仓杠杆资金流水','统一账户资金流水','链上交易资金流水','币本位合约资金流水'):
  amt=dec(r.get('数量') or r.get('总额'))
  yield r.get('时间'),r.get('币种'),amt,'internal' if 'transfer' in t.lower() or t.startswith('trans_') else 'spot' if t in ('buy','sell') else 'other'


def okx(dataset,r):
 if dataset=='trading_bills':
  kind=r.get('账单类型') or '';amt=dec(r.get('交易账户余额变动'));coin=r.get('交易账户余额单位')
  if kind=='划转':c='internal'
  elif kind=='永续合约':c='funding' if '资金费' in (r.get('交易类型') or '') else 'futures'
  elif kind=='币币':c='spot'
  else:c='other'
  yield r.get('时间'),coin,amt,c
 elif dataset=='funding_bills':
  t=r.get('类型') or ''
  if t=='充币' or '充值' in t:c='deposit'
  elif t=='提币':c='withdraw'
  elif t in ('完成订单','创建订单'):c='c2c'
  elif '奖励' in t or '收益' in t or '返佣' in t or t=='已收取':c='earn'
  elif '统一交易账户' in t or '申购' in t or '赎回' in t or '锁仓' in t:c='internal'
  else:c='other'
  yield r.get('时间'),r.get('币种'),dec(r.get('数量')),c


EXCHANGES={'binance':binance,'bitget':bitget,'okx':okx}


def flows(db):
 seen=set()
 for src,ds,rk,payload in db.execute("SELECT source,dataset,record_key,payload FROM observations WHERE source IN ('binance','bitget','okx')"):
  if rk in seen:continue
  seen.add(rk);r=json.loads(payload)
  for at,coin,amt,cat in EXCHANGES[src](ds,r) or ():
   if amt is not None and coin:yield src,str(at or '')[:19],coin.strip().upper(),amt,cat


def summary(db=None):
 db=db or connect(readonly=True)
 tot=collections.defaultdict(lambda:collections.defaultdict(Decimal));span={};result=collections.defaultdict(Decimal)
 for ex,at,coin,amt,cat in flows(db):
  tot[(ex,coin)][cat]+=amt
  if at:a,b=span.get(ex,(at,at));span[ex]=(min(a,at),max(b,at))
  if coin in STABLE and cat in ('futures','funding','fee','earn'):result[ex]+=amt
 out={}
 for ex in sorted({e for e,_ in tot}):
  coins={c:{k:f'{v.normalize():f}' for k,v in sorted(d.items()) if v} for (e,c),d in sorted(tot.items()) if e==ex}
  stable={c:v for c,v in coins.items() if c in STABLE}
  out[ex]={'from':span[ex][0][:10],'to':span[ex][1][:10],'result_usd_stable':str(result[ex].quantize(Decimal('0.01'))),
   'stablecoins':stable,'other_coins':len(coins)-len(stable)}
 out['note']='result = futures + funding + fee + earn in USD stablecoins; spot trades are not valued (no prices); exchange data ends on each "to" date'
 return out
