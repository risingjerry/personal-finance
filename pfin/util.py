"""Small helpers shared by every source: money, time, hashing."""
import datetime as dt,hashlib,re
from decimal import Decimal,InvalidOperation
from zoneinfo import ZoneInfo
from .config import cfg

BEIJING=ZoneInfo('Asia/Shanghai')


def sha(b):return hashlib.sha256(b).hexdigest()


def money(v):
 """Decimal from '¥1,234.50' / '-28.80元' / 12; None when it is not a number."""
 if v is None:return None
 s=re.sub(r'[¥￥,\s元]','',str(v))
 try:return Decimal(s)
 except InvalidOperation:return None


_FORMATS=(('%Y-%m-%dT%H:%M:%S%z',True),('%Y-%m-%d %H:%M:%S',True),('%Y-%m-%d %H:%M',True),('%Y-%m-%d',False))
def when(v,tz=None):
 """(aware datetime in the reporting timezone, has_time) or (None, False).
 Naive values are read in tz (the source's own zone; default Beijing, where Chinese apps print times)."""
 if not v:return None,False
 s=str(v).strip().replace('/','-')
 if 'T' in s:
  try:d=dt.datetime.fromisoformat(s.replace('Z','+00:00'))  # full ISO 8601: fractions, Z, offsets
  except ValueError:d=None
  if d:return (d if d.tzinfo else d.replace(tzinfo=tz or BEIJING)).astimezone(cfg().tz),True
 for f,has in _FORMATS:
  try:d=dt.datetime.strptime(s[:25] if '%z' in f else s[:len(dt.datetime(2000,1,1).strftime(f))],f)
  except ValueError:continue
  return (d if d.tzinfo else d.replace(tzinfo=tz or BEIJING)).astimezone(cfg().tz),has
 return None,False


def channel_of(text):
 """Which payment channel a bank / credit-card line went through, from its description."""
 t=str(text or '')
 if '财付通' in t or '微信' in t:return 'wechat'
 if '支付宝' in t:return 'alipay'
 if '美团' in t:return 'meituan'
 return None
