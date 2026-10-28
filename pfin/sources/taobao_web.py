"""Taobao web order list (mtop.taobao.order.queryboughtlistv2 pages, saved read-only from the logged-in page) -> one row per order."""
import datetime,json
from ..model import Dataset,Source
BJ=datetime.timezone(datetime.timedelta(hours=8))

def _yuan(s):
 s=(s or '').replace('¥','').replace(',','').strip()
 return s or None

def orders(pages):
 """Orders are keyed by component id (<Type>_<orderId>[...]); a repeated order across pages stays one row."""
 out={}
 for p in pages:
  data=p.get('data') or {}
  for k,v in data.items():
   if not k.startswith('MainGroup_'):continue
   f=v.get('fields') or {};oid=f.get('orderId')
   if not oid or oid in out:continue
   seller=(data.get(f'sellerInfo_{oid}') or {}).get('fields') or {}
   items=[(data[x].get('fields') or {}) for x in sorted(data) if x.startswith((f'item_{oid}_',f'itemFliggy_{oid}_'))]
   # The pay component id is pay_<orderId> or pay_<orderId>/<n>.
   pay=next(((data[x].get('fields') or {}) for x in data if x==f'pay_{oid}' or x.startswith(f'pay_{oid}/')),{})
   # Multi-item orders carry the paid total in pay_<id>; single-item ones in the item's itemPay.
   fees=[(pay.get('actualFee') or {}).get('value')]+[((i.get('itemPay') or {}).get('actualFee') or {}).get('value') for i in items]
   paid=next((x for x in fees if x),None)
   s=seller.get('seller') or {}
   ts=f.get('createOrderTime') or seller.get('orderCreateTime')
   when=datetime.datetime.fromtimestamp(int(ts)/1000,BJ) if ts else None
   out[oid]={'order_id':oid,'occurred':when.strftime('%Y-%m-%d %H:%M:%S') if when else None,
             'shop':' '.join(x for x in (s.get('shopName'),s.get('shopSubName')) if x) or None,
             'items':[{'title':(i.get('item') or {}).get('title'),'sku':(i.get('item') or {}).get('skuText'),'quantity':(i.get('item') or {}).get('quantity'),
                       'paid':_yuan(((i.get('item') or {}).get('priceInfo') or {}).get('actualTotalFee'))} for i in items],
             'paid':_yuan(paid),'status':(seller.get('status') or {}).get('text'),'order_status_code':f.get('orderStatus'),
             'order_type':(data.get(f'Main_{oid}') or {}).get('fields',{}).get('orderType'),'archived':f.get('archive')=='true'}
 return list(out.values())



def parse(path,data):
 if path.suffix!='.json':return
 d=json.loads(data)
 if d.get('kind')!='boughtlist_mtop':return
 # Orders describe what was bought; the money is counted from the payment layer.
 for i,row in enumerate(orders(d['pages'])):yield 'orders',f'order:{i}',row,'UTC+8'


SOURCE=Source('taobao_web',{'*':Dataset('detail','detail',skip='same orders as the taobao connector\'s taobao-order rows')},parse,
 lambda d,r:str(r['order_id']) if r.get('order_id') else None)
