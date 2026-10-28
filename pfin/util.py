"""Small helpers shared by every source: money, time, hashing."""
import datetime as dt,hashlib,re
from decimal import Decimal,InvalidOperation
from zoneinfo import ZoneInfo
from .config import cfg

BEIJING=ZoneInfo('Asia/Shanghai')


def sha(b):return hashlib.sha256(b).hexdigest()


_NOISE=re.compile(r'[¥￥,\s元]')
def money(v):
 """Decimal from '¥1,234.50' / '-28.80元' / 12; None when it is not a number."""
 if v is None:return None
 s=_NOISE.sub('',str(v))
 try:return Decimal(s)
 except InvalidOperation:return None


_FORMATS=(('%Y-%m-%dT%H:%M:%S%z',True),('%Y-%m-%d %H:%M:%S',True),('%Y-%m-%d %H:%M',True),('%Y-%m-%d',False))
# A stamp's character count usually settles which format it is, and strptime is the whole cost of a parse, so a stamp
# is tried against its likely format first and only then against the rest. The full list stays the fallback, so a
# stamp of an unexpected length (or one whose length lies) still parses exactly as before.
_ALL=tuple((f,has,25 if '%z' in f else len(dt.datetime(2000,1,1).strftime(f))) for f,has in _FORMATS)
_FAST={25:_ALL[:1],19:_ALL[1:2],16:_ALL[2:3],10:_ALL[3:4]}
for _n,_first in _FAST.items():_FAST[_n]=_first+_ALL  # the guess first, then the same fallback as before
def when(v,tz=None):
 """(aware datetime in the reporting timezone, has_time) or (None, False).
 Naive values are read in tz (the source's own zone; default Beijing, where Chinese apps print times)."""
 if not v:return None,False
 s=str(v).strip().replace('/','-')
 if 'T' in s:
  try:d=dt.datetime.fromisoformat(s.replace('Z','+00:00'))  # full ISO 8601: fractions, Z, offsets
  except ValueError:d=None
  if d:return (d if d.tzinfo else d.replace(tzinfo=tz or BEIJING)).astimezone(cfg().tz),True
 # A stamp's length picks the format: 10 chars is a date, 16 has minutes, 19 has seconds, longer may carry an offset.
 # strptime is the whole cost of this function, so a stamp is parsed once instead of up to four times.
 for f,has,keep in (_FAST.get(len(s)) or _ALL):
  try:d=dt.datetime.strptime(s[:keep],f)
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
