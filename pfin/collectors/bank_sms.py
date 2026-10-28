"""Read-only bank SMS from the Mac Messages database; saves raw money-movement texts, never codes or ads."""
import argparse,datetime,json,os,re,sqlite3
from pathlib import Path
from ..config import cfg
CHAT_DB=Path.home()/'Library/Messages/chat.db'
BJ=datetime.timezone(datetime.timedelta(hours=8))
# Sender numbers end with the bank's service number (95559, 1069…95559, +86…95599).
BANKS={'95559':'bocom','95599':'abc','95555':'cmb'}
SECRET=re.compile(r'验证码|动态密码|短信密码|密码序号|校验码')
MONEY=re.compile(r'余额|可用额度|还款|退款|账单|扣款')

def body(text,blob):
 """Newer messages keep the text only in attributedBody (a typedstream holding one NSString)."""
 if text:return text
 if not blob:return None
 i=blob.find(b'NSString')
 if i<0 or (j:=blob.find(b'+',i))<0:return None
 n,k=blob[j+1],j+2
 if n==0x81:n,k=int.from_bytes(blob[k:k+2],'little'),k+2
 elif n==0x82:n,k=int.from_bytes(blob[k:k+3],'little'),k+3
 return blob[k:k+n].decode('utf-8','replace')

def bank_of(sender):return next((b for s,b in BANKS.items() if sender.endswith(s)),None)

def read(after=0,db_path=CHAT_DB):
 db=sqlite3.connect(f'file:{db_path}?mode=ro',uri=True)
 rows=db.execute('SELECT m.ROWID,m.guid,h.id,m.date,m.text,m.attributedBody FROM message m JOIN handle h ON m.handle_id=h.ROWID WHERE m.ROWID>? AND m.is_from_me=0 ORDER BY m.ROWID',(after,)).fetchall();db.close()
 out=[];last=after
 for rowid,guid,sender,date,text,blob in rows:
  last=max(last,rowid);bank=bank_of(sender)
  if not bank:continue
  t=body(text,blob)
  # Codes never leave chat.db; ads and notices without money movement are skipped.
  if not t or SECRET.search(t) or not MONEY.search(t):continue
  received=datetime.datetime.fromtimestamp(date/1e9+978307200,BJ).strftime('%Y-%m-%d %H:%M:%S')
  out.append({'guid':guid,'sender':sender,'bank':bank,'received':received,'text':t})
 return out,last

def _num(s):return s.replace(',','') if s else None
def _when(received,y,mo,d,hh,mi):
 """SMS often omits the year: take it from the receive time, one year back across New Year."""
 r=datetime.datetime.strptime(received,'%Y-%m-%d %H:%M:%S');mo=int(mo)
 y=int(y) if y else r.year-(1 if mo>r.month else 0)
 return f'{y:04d}-{mo:02d}-{int(d):02d} {int(hh):02d}:{int(mi):02d}'

def parse(m):
 """Typed fields only when the text matches a known bank format; anything else stays kind=other."""
 t=m['text'];r=m['received']
 d=re.search(r'(?:尾号\*?|贵(?:账户|卡)\*)(\d{4})(?:的卡)?(?:于)?(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日\s*(\d{1,2}):(\d{2})(.*?)([0-9,]+\.\d{2})(?:元|人民币)[,，。]?\s*(?:现余额为?|交易后(?:卡内)?余额为?)([0-9,]+\.\d{2})(?:元|人民币)',t)
 if d:
  # 摘要 (e.g. a lender's name) explains transfers out of the account.
  desc=d[7].removeprefix('在')+((' 摘要:'+a[1]) if (a:=re.search(r'摘要[:：]([^。]+)',t)) else '')
  # 还款/抹账 move money in either direction depending on the product; leave them unknown.
  flow='in' if re.search(r'转入|存入|退款|退货',d[7]) else None if re.search(r'抹账|转账还款',d[7]) else 'out' if re.search(r'支付|消费|转出|取款|取现|扣款|个贷还款|贷记卡还款',d[7]) else None
  row={'kind':'debit_txn','card':d[1],'occurred':_when(r,*d.groups()[1:6]),'desc':desc,'amount':_num(d[8]),'flow':flow,'balance':_num(d[9])}
  if c:=re.search(r'对方户名[:：]([^，,]+)',t):row['counterparty']=c[1]
  return row
 c=re.search(r'尾号(\d{4})交行信用卡(\d{1,2})日(\d{1,2})时(\d{1,2})分成功消费人民币([0-9,]+\.\d{2})元',t)
 if c:
  rv=datetime.datetime.strptime(r,'%Y-%m-%d %H:%M:%S');mo=rv.month if int(c[2])<=rv.day else (rv.month-1 or 12)
  return {'kind':'credit_txn','card':c[1],'occurred':_when(r,None,mo,c[2],c[3],c[4]),'amount':_num(c[5]),'flow':'out'}
 if c:=re.search(r'尾号(\d{4})(?:交通银行)?信用卡(?:收到退款)?人民币([0-9,]+\.\d{2})元(?:退款已到账|，详情)',t):return {'kind':'credit_refund','card':c[1],'amount':_num(c[2]),'flow':'in'}
 if c:=re.search(r'尾号(\d{4})信用卡还款人民币([0-9,]+\.\d{2})元',t):return {'kind':'credit_repayment','card':c[1],'amount':_num(c[2])}
 if c:=re.search(r'尾号(\d{4})(?:交行)?信用卡\s*(\d+/\d+-\d+/\d+)\s*账单',t):return {'kind':'statement','card':c[1],'period':c[2]}
 if '按揭贷款' in t and '已扣款成功' in t:return {'kind':'loan_payment','amount':_num((re.search(r'本息([0-9,]+\.\d{2})元',t) or [None,None])[1])}
 return {'kind':'other'}

def run(home=None,db_path=CHAT_DB):
 home=home or cfg().home
 state=home/'bank-sms-state.json';after=json.loads(state.read_text())['last_rowid'] if state.exists() else 0
 msgs,last=read(after,db_path)
 if msgs:
  raw=home/'raw'/'bank_sms';raw.mkdir(parents=True,exist_ok=True,mode=0o700)
  f=raw/f"sms-{datetime.datetime.now(BJ).strftime('%Y%m%dT%H%M%S')}.json"
  f.write_text(json.dumps({'source':'Mac Messages chat.db (read-only)','count':len(msgs),'messages':msgs},ensure_ascii=False,indent=1));os.chmod(f,0o600)
 tmp=state.with_suffix('.tmp');tmp.write_text(json.dumps({'last_rowid':last}));tmp.replace(state)
 return {'new_messages':len(msgs)}

if __name__=='__main__':
 argparse.ArgumentParser(description=__doc__).parse_args();print(json.dumps(run()))
