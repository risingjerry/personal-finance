"""The ontology's vocabulary (docs/ONTOLOGY.md).

Observation (Record v1): one row of one original file, normalized. Many observations describe one real event.
  layer   money | detail | signal       does it move money, describe a purchase, or notify about one
  role    channel | funding | detail | signal
            channel  the ledger the payment went through (WeChat Pay, Alipay): one row per payment
            funding  the account the money really came from (bank card, credit card, 花呗, 月付)
            detail   what was bought (orders); never counted as money
            signal   a real-time notice (bank SMS, payment card); confirms a payment
  account  '<institution>:<last4>' or a wallet name ('wechat', 'alipay', 'huabei')
  at       aware datetime in the reporting timezone; has_time False when the source gives only a date
  amount   positive Decimal; sign -1 money out of the account, +1 money in, 0 unknown
  ids      trade / order / transfer numbers shared with other sources (exact joins)
  fields   source fields a rule may test (e.g. merchant_order_no)

Event: one real movement of money, anchored on its best observation, with every other observation linked.
  kind  purchase | refund | income | p2p | internal | fee        book  personal | business | investing
"""
from dataclasses import dataclass,field
from decimal import Decimal


def observation(row,**k):
 """Build an observation from an evidence row ({source, dataset, record_key}) and normalized values."""
 o=dict(key=row['record_key'],source=row['source'],dataset=row['dataset'],layer=None,role=None,account=None,at=None,has_time=False,
        amount=None,sign=0,counterparty=None,title=None,status=None,method=None,channel=None,card=None,ids=set(),
        evidence=None,kind_hint=None,book=None,project=None,fee=None,fields={})
 o.update(k);o['ids']=set(o['ids'])-{None,''};return o


@dataclass
class Dataset:
 """How one dataset of a source takes part in resolution."""
 layer:str
 role:str
 # Among money datasets of one account the highest priority covers its own date range; lower ones only
 # fill the days outside it (e.g. screen-read WeChat bills before the official export starts).
 priority:int=1
 # Largest amount difference still the same payment (OKX C2C payers often round up).
 tolerance:Decimal=Decimal(0)
 # How far (seconds) a detail looks for its payment (default an hour). Past the hour, a payment is only taken
 # as this detail's when the payee matches `payee` (the platform), or order and payee are the same kind of
 # merchant by `payee_alike` (a hotel booked there, paid at the desk).
 window:int=3600
 # A detail paid with a credit line that is not connected: when no payment is found it becomes the event.
 pay_later:str|None=None
 payee:str|None=None
 payee_alike:str|None=None
 # Kept out of resolution, with the reason (ablation; docs/ONTOLOGY.md).
 skip:str|None=None


@dataclass
class Source:
 """A data source plugin: parses its raw files into rows and normalizes rows into observations.
 See docs/SOURCES.md for the contract and pfin/sources/ for examples."""
 name:str
 datasets:dict=field(default_factory=dict)  # dataset name -> Dataset
 parse:object=None       # (path, data: bytes) -> iterable of (dataset, locator, row: dict, timezone label)
 identity:object=None    # (dataset, row) -> natural id string or None (rows without one dedupe per file)
 normalize:object=None   # (dataset, row, evidence_row) -> observation or None
 stamp_keys:tuple=()     # row keys holding the occurrence time, for indexing

 def spec(self,dataset):
  return self.datasets.get(dataset) or self.datasets.get('*')
