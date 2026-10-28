"""WeChat Pay official bill export (微信支付账单, .xlsx), put in raw/wechat/.
Request it in WeChat: 我 → 服务 → 钱包 → 账单 → 常见问题 → 下载账单 → 用于个人对账. One row per 交易单号."""
import re
from ..model import Dataset,Source,observation
from ..util import money,when
from ._files import xlsx_rows


def parse(path,data):
 if path.suffix!='.xlsx':return
 for sheet,i,row in xlsx_rows(data,'交易单号'):yield 'payments',f'{sheet}:row:{i}',row,'未核实'


def identity(dataset,row):
 v=str(row.get('交易单号') or '').strip()
 return v if v not in ('','--','无','None') else None


def normalize(dataset,p,row):
 at,has=when(p.get('交易时间'));sign={'支出':-1,'收入':1}.get(p.get('收/支'),0)
 amt=money(p.get('金额(元)'));fee=money((re.search(r'服务费¥?([\d.]+)',str(p.get('备注'))) or [0,'0'])[1])
 cost=None
 if p.get('交易类型')=='零钱提现' and amt is not None:amt-=fee;cost=fee or None  # the bank receives the amount net of the fee
 return observation(row,layer='money',role='channel',account='wechat',at=at,has_time=has,amount=amt,fee=fee,sign=sign,counterparty=p.get('交易对方'),
  fee_cost=cost,title=p.get('商品'),method=p.get('支付方式'),status=p.get('当前状态'),ids={x for x in (p.get('交易单号'),p.get('商户单号')) if x and x!='/'},
  channel='wechat',evidence='official_export',kind_hint=p.get('交易类型'))


SOURCE=Source('wechat',{'payments':Dataset('money','channel',priority=2)},parse,identity,normalize)
