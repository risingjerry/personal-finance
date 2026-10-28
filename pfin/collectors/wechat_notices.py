"""Read-only WeChat Pay / bank notice cards through the local WeChat reader named in config.json "wechat_reader"
(not part of pfin); saves raw cards, never infers profit."""
import argparse,datetime,json,os,re,subprocess,sys,time
from pathlib import Path
from ..config import cfg
CHATS={'gh_3dfda90e39d6':'wechat_pay','gh_4496ecf4647e':'bocom_card'}
BJ=datetime.timezone(datetime.timedelta(hours=8))
def reader():
 r=cfg().wechat_reader
 if not r:sys.exit('config.json "wechat_reader" is not set: the command that reads the local WeChat database')
 return [r] if isinstance(r,str) else list(r)
def cli(*args):
 out=subprocess.run([*reader(),*args,'--client',cfg().wechat_client],capture_output=True,text=True,timeout=120)
 d=json.loads(out.stdout)
 if not d.get('ok'):e=d.get('error') or {};raise RuntimeError(f"wechat reader {args[0]} failed: {e.get('code')} {(e.get('details') or {}).get('diagnostic','')}")
 return d['data']
def yuan(text,label):
 m=re.search(label+r'[:：]?\s*[¥￥]?\s*([0-9][0-9,]*\.?[0-9]*)',text or '')
 return m[1].replace(',','') if m else None

def parse(channel,f):
 """Typed fields only from the card text; anything unrecognised stays kind=other with amount null."""
 title=f.get('title') or '';des=f.get('description') or '';r=f.get('pay_receipt') or {}
 if channel=='wechat_pay':
  if title.startswith('已支付'):
   m=re.match(r'使用(.+?)支付[¥￥]',des)
   return {'kind':'payment','amount':yuan(title,'已支付'),'method':m[1] if m else None,'merchant':r.get('merchant'),'trans_id':r.get('trans_id')}
  if title.startswith('已扣费'):
   # Password-free deductions (e.g. ride-hailing pay-after-ride) use their own receipt card.
   m=re.match(r'通过(.+?)扣款[¥￥]',des)
   return {'kind':'payment','amount':yuan(title,'已扣费'),'method':m[1] if m else None,'merchant':r.get('merchant'),'trans_id':r.get('trans_id'),'deduction':True}
  if title.startswith('昨日记账日报'):
   s=re.search(r'昨日支出[¥￥]([0-9.,]+)，共(\d+)笔',des);i=re.search(r'昨日入账[¥￥]([0-9.,]+)，共(\d+)笔',des)
   return {'kind':'daily_report','spent':s[1].replace(',','') if s else None,'spent_count':int(s[2]) if s else None,'received':i[1].replace(',','') if i else None,'received_count':int(i[2]) if i else None}
  if title=='零花钱使用通知':return {'kind':'allowance_spend','amount':yuan(des,'交易金额'),'merchant':(re.search(r'交易商户(\S+)',des) or [None,None])[1]}
  if title.startswith('零花钱'):return {'kind':'allowance_other','amount':yuan(des,'(?:充值|收款|交易)金额')}
  # The same cards in WeChat's English UI.
  if m:=re.fullmatch(r'[¥￥]([0-9.,]+) paid',title):
   p=re.match(r'Paid by (.+?)[¥￥]',des)
   return {'kind':'payment','amount':m[1].replace(',',''),'method':p[1].strip() if p else None,'merchant':r.get('merchant'),'trans_id':r.get('trans_id')}
  if m:=re.fullmatch(r'Deducted: [¥￥]([0-9.,]+)',title):
   p=re.match(r'Deducted by (.+?)[¥￥]',des)
   return {'kind':'payment','amount':m[1].replace(',',''),'method':p[1].strip() if p else None,'merchant':r.get('merchant'),'trans_id':r.get('trans_id'),'deduction':True}
  if m:=re.match(r'已续费[¥￥]([0-9.,]+)',title):
   p=re.match(r'通过(.+?)自动续费',des)
   return {'kind':'payment','amount':m[1].replace(',',''),'method':p[1] if p else None,'merchant':r.get('merchant'),'trans_id':r.get('trans_id'),'auto_renew':(re.search(r'续费项目(.+?) 开通账号',des) or [None,None])[1]}
  if title=='就医支付凭证':
   return {'kind':'payment','amount':yuan(des,'微信支付'),'order_amount':yuan(des,'订单金额'),'insurance':yuan(des,'医保统筹扣款'),'method':(re.search(r'微信支付：[¥￥][0-9.,]+\s*（(.+?)）',des) or [None,None])[1],'merchant':(re.search(r'商品详情：(\S+)',des) or [None,None])[1],'trans_id':(re.search(r'交易单号：(\d+)',des) or [None,None])[1]}
  if title=='微信支付凭证':
   return {'kind':'payment','amount':yuan(des,'付款金额'),'method':(re.search(r'付款方式(\S+)',des) or [None,None])[1],'merchant':r.get('merchant') or (re.search(r'商品名称(\S+)',des) or [None,None])[1],'trans_id':r.get('trans_id'),'device':(re.search(r'手表名称(.+?) 商品',des) or [None,None])[1]}
  # A Deduction Notice repeats a deduction that has its own payment card; keep it as a notice only.
  if title=='Deduction Notice':return {'kind':'deduction_notice','amount':yuan(des,'Amount'),'merchant':(re.search(r'Merchant(.+?) Time',des) or [None,None])[1]}
  # Wallet change → bank card: money moves between the owner's own accounts.
  if title in ('零钱提现发起','零钱提现已到账'):
   return {'kind':'withdrawal','stage':'started' if title.endswith('发起') else 'arrived','amount':yuan(des,'提现金额'),'bank':(re.search(r'提现银行(\S+)',des) or [None,None])[1],'withdraw_time':(re.search(r'提现时间(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)',des) or [None,None])[1]}
  if title in ('退款到账通知','Refund Processed Notification'):
   return {'kind':'refund','flow':'in','amount':yuan(des,'(?:退款金额|Refund Amount)'),'merchant':(re.search(r'(?:商户名称|Merchant Name)(.+?) (?:退款方式|Refund Method)',des) or [None,None])[1],'method':(re.search(r'(?:退款方式|Refund Method)(.+?) (?:到账时间|Processed Time)',des) or [None,None])[1]}
  if title.startswith('红包退款'):
   return {'kind':'red_packet_refund','flow':'in','stage':'arrived' if '到账' in title else 'started','amount':yuan(des,'退款金额'),'method':(re.search(r'退款方式(\S+)',des) or [None,None])[1]}
  if title in ('转账退款到账通知','Transfer Refund Alert'):
   return {'kind':'transfer_refund','flow':'in','amount':yuan(des,'(?:退款金额|Refund Amount)'),'method':(re.search(r'(?:退款方式|Refund method)\s*(.+?) (?:退款原因|Reason)',des) or [None,None])[1]}
  if title.startswith('个人收款码到账'):return {'kind':'income','flow':'in','amount':yuan(des,'收款金额'),'channel_detail':'收款码'}
  if title=='你收到一笔活动奖励':return {'kind':'income','flow':'in','amount':yuan(des,'收款金额'),'channel_detail':(re.search(r'转账备注(.+)',des) or [None,None])[1]}
  if title=='支付失败提醒':return {'kind':'payment_failed','amount':yuan(des,'尝试支付金额')}
  if title.startswith('上月记账月报'):
   s=re.search(r'上月支出[¥￥]([0-9.,]+)，共(\d+)笔',des);i=re.search(r'上月入账[¥￥]([0-9.,]+)，共(\d+)笔',des)
   return {'kind':'monthly_report','spent':s[1].replace(',','') if s else None,'spent_count':int(s[2]) if s else None,'received':i[1].replace(',','') if i else None,'received_count':int(i[2]) if i else None}
  if '先享后付' in title:return {'kind':'pay_later_notice','merchant':(re.search(r'服务商家(\S+)',des) or [None,None])[1],'state':title}
  # Saving notes ride on a payment that has its own card; never count them as spending.
  if title.startswith(('优惠已使用','多次优惠使用通知')):return {'kind':'discount_note','saved':yuan(title,'节省')}
  if re.search(r'优惠|券|活动|礼包|汇率|分付|自动续费|开通|启用|关闭|会员|积分|账单流水|交易明细证明|零钱通|Service|Deposit|Apple Watch|按时支付|银行卡交易提醒|提现免费',title):return {'kind':'info'}
 if channel=='bocom_card':
  if title=='还款提醒':return {'kind':'repayment_due','card':(re.search(r'尾号:?(\d{4})',des) or [None,None])[1],'amount':yuan(des,'人民币未还'),'due_date':(re.search(r'到期还款日:(\S+)',des) or [None,None])[1]}
  if title=='还款成功提醒':return {'kind':'credit_repayment','card':(re.search(r'尾号:?(\d{4})',des) or [None,None])[1],'amount':yuan(des,'实际还款金额'),'repay_time':(re.search(r'还款时间:(\d{4}年\d\d月\d\d日 \d\d:\d\d:\d\d)',des) or [None,None])[1],'remaining':yuan(des,'人民币未还')}
  if title in ('信用卡账单提醒','信用卡"微"账单'):return {'kind':'statement_notice','statement_date':(re.search(r'账单日期\s*[:：]\s*(\S+)',des) or [None,None])[1],'due_date':(re.search(r'到期还款日\s*[:：]\s*(\S+)',des) or [None,None])[1]}
  if title=='刷卡金成功到账提醒':return {'kind':'cashback','amount':None}
  if title=='业务审批通知':return {'kind':'info','card':(re.search(r'尾号(\d{4})',des) or [None,None])[1]}
  # Everything else on 买单吧 without a card number is marketing.
  if title!='交易提醒' and '周账单' not in title and not re.search(r'尾号|卡号',des):return {'kind':'info'}
 if channel=='bocom_card':
  if title=='交易提醒':
   t=re.search(r'交易类型[:：](\S+)',des);c=re.search(r'交易卡号[:：](\S+)',des)
   return {'kind':'card_alert','amount':yuan(des,'交易金额'),'txn_type':t[1] if t else None,'card':c[1][-4:] if c else None,'txn_time':(re.search(r'交易时间[:：](\d{4}年\d{2}月\d{2}日 \d{2}:\d{2})',des) or [None,None])[1]}
  if '周账单' in title:return {'kind':'weekly_statement','amount':yuan(des,'消费总金额')}
 if channel=='wechat_chat':
  out=f.get('direction')=='outgoing'
  if f.get('subtype')==2000 and f.get('transfer'):
   t=f['transfer']
   # Payer is debited when sending; payee is credited on accepting; a return refunds the payer.
   # The other card of each pair moves no money for us, so every transfer counts once.
   flow={('sent',True):'out',('accepted',True):'in',('returned',False):'in'}.get((t['stage'],out))
   return {'kind':'transfer','stage':t['stage'],'flow':flow,'amount':t.get('amount'),'transfer_id':t.get('transfer_id'),'memo':t.get('memo')}
  # Red packet cards carry no amount; only who sent it.
  if f.get('subtype')==2001:return {'kind':'red_packet','flow':'out' if out else 'unknown','amount':None}
 return {'kind':'other'}

def collect(chat,after,floor):
 """Page backwards until the saved boundary; partial pages never advance the cursor."""
 floor=floor if after is None else after['create_time']
 items=[];before=None
 while True:
  d=cli('history','--chat',chat,'--source','main','--limit','50',*(['--before',before] if before else []))
  page=d['items'];fresh=[i for i in page if i['create_time']>=floor and (after is None or (i['create_time'],i['server_id'])!=(after['create_time'],after['server_id']))]
  items.extend(fresh)
  if not page or len(fresh)<len(page) or not d['may_have_more'] or not d['next_before']:break
  before=d['next_before']
 return sorted({(i['create_time'],i['server_id']):i for i in items}.values(),key=lambda i:(i['create_time'],i['server_id']))

def card(chat,channel,ref):
 m=cli('message-get','--ref',ref)['message'];f=m['content'].get('fields',{})
 c={'chat':chat,'channel':channel,'server_id':m['server_id'],'create_time':m['create_time'],
    'occurred':datetime.datetime.fromtimestamp(m['create_time'],BJ).isoformat(),
    'title':f.get('title'),'description':f.get('description'),'pay_receipt':f.get('pay_receipt')}
 if channel=='wechat_chat':c.update(direction=m['direction'],subtype=m['subtype'],transfer=f.get('transfer'))
 return c

def active_chats(since):
 chats=[];offset=0
 while True:
  d=cli('inbox','--source','main','--include-hidden','--no-exclude-muted','--limit','200','--offset',str(offset))
  chats+=[i['chat'] for i in d['items'] if i['last_time']>=since and i['chat_class'] in ('direct','group') and i['chat'] not in CHATS]
  if not d.get('next_offset') or len(d['items'])<200:return chats
  offset=d['next_offset']

def run(days,raw=None,state_path=None):
 raw=raw or cfg().raw/'wechat_notices';state_path=state_path or cfg().home/'wechat-notices-state.json'
 state=json.loads(state_path.read_text()) if state_path.exists() else {}
 cards=[];cursors=dict(state);floor=time.time()-days*86400
 # Transfers and red packets live in ordinary chats; rescan a 10-minute overlap, identities dedupe it.
 started=int(time.time());since=state.get('_chat_scan',floor+600)-600
 failed=[]
 for chat in active_chats(since):
  try:
   found=[card(chat,'wechat_chat',i['message_ref']) for i in collect(chat,None,since) if i['kind']==49 and i['subtype'] in (2000,2001)]
  except (RuntimeError,KeyError,json.JSONDecodeError,subprocess.TimeoutExpired) as e:failed.append({'chat':chat,'error':str(e)[:200]});continue
  cards.extend(found)
 # One unreadable chat keeps the scan window open, so the next run retries the whole window.
 if not failed:cursors['_chat_scan']=started
 for chat,channel in CHATS.items():
  rows=collect(chat,state.get(chat),floor)
  for i in rows:
   if 'message_ref' not in i:raise RuntimeError('history row without message_ref; refusing partial save')
   cards.append(card(chat,channel,i['message_ref']))
  if rows:cursors[chat]={'create_time':rows[-1]['create_time'],'server_id':rows[-1]['server_id']}
 if cards or failed:
  raw.mkdir(parents=True,exist_ok=True,mode=0o700)
  path=raw/f"notices-{datetime.datetime.now(BJ).strftime('%Y%m%dT%H%M%S')}.json"
  path.write_text(json.dumps({'source':'wechat reader history+message-get','client':cfg().wechat_client,'count':len(cards),'cards':cards,'unread_chats':failed},ensure_ascii=False,indent=1));os.chmod(path,0o600)
 # Cursor advances only after the raw file is durably written.
 tmp=state_path.with_suffix('.tmp');tmp.write_text(json.dumps(cursors));os.replace(tmp,state_path)
 return {'new_cards':len(cards),'unread_chats':len(failed)}

if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--days',type=int,default=35,help='first-run backfill window');a=p.parse_args()
 print(json.dumps(run(a.days)))
