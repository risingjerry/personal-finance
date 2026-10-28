"""Where the data lives and the owner's private settings. The code holds nothing personal.

Everything private sits in one directory (PFIN_HOME, default ~/Library/Application Support/personal-finance):

  raw/                 original files, one folder per source; never modified
  evidence.sqlite      every row of every original file, deduplicated (documents, observations)
                       plus the resolved ontology (events, links) after `pfin resolve --write`
  config.json          {"timezone": "Asia/Shanghai", "owner": [...], "accounts": {...}, "cards": {...}, "connectors": {...}}
  rules.json           ordered rules: which book / project an event belongs to (see docs/RULES.md)
  overrides.json       per-transaction decisions keyed by trade / order / transfer id
  contacts.json        WeChat chat id -> display name (read once from the client's contact cache)
"""
import json,os,sys
from pathlib import Path
from zoneinfo import ZoneInfo


def default_home():
 return Path(os.environ.get('PFIN_HOME') or Path.home()/'Library/Application Support/personal-finance')


class Config:
 def __init__(self,home=None,**override):
  self.home=Path(home or default_home())
  c=self._load('config.json',{})
  self.tz=ZoneInfo(c.get('timezone','Asia/Shanghai'))
  # Names the owner appears under (as banks and apps print them): marks moves between own accounts.
  self.owner=c.get('owner') or self._load('owner.json',{}).get('names',[])
  # Account id -> {"book": "business", "label": "..."}: accounts that only serve one book default to it.
  self.accounts=c.get('accounts',{})
  # Screen-read app lists that do not print the card number: dataset -> last four digits.
  self.cards=c.get('cards',{})
  # Collectors: which WeChat client to read, where phone screen reads land, a Python with openpyxl for .xlsx.
  self.wechat_reader=c.get('wechat_reader')  # command (or argv list) that reads the local WeChat DB; not part of pfin
  self.wechat_client=c.get('wechat_client')
  self.phone_out=c.get('phone_out')
  self.python=c.get('python')
  # Raw folders filled by a connector run with other arguments: folder -> {"connector", "args", "account"}
  # (e.g. "alipay-work": the alipay connector on a second account, its rows booked to account alipay:work).
  self.connectors=c.get('connectors',{})
  # Extra source modules (e.g. a business's own systems), importable dotted paths; "plugin_paths" are folders
  # put on sys.path first, so a private plugin can live in its own repository.
  self.plugins=c.get('plugins',[])
  for d in c.get('plugin_paths',[]):
   d=os.path.expanduser(d)
   if d not in sys.path:sys.path.append(d)
  # What `pfin sync` runs besides the built-in collectors (see pfin/sync.py).
  self.sync=c.get('sync',{})
  self.rules=self._load('rules.json',{}).get('rules',[])
  self.overrides=self._load('overrides.json',{})
  self.contacts=self._load('contacts.json',{})
  for k,v in override.items():setattr(self,k,v)

 def _load(self,name,default):
  p=self.home/name
  return json.loads(p.read_text()) if p.exists() else default

 @property
 def raw(self):return self.home/'raw'
 @property
 def db(self):return self.home/'evidence.sqlite'


_current=None
def cfg():
 global _current
 if _current is None:_current=Config()
 return _current
def use(c):
 """Swap the active config (tests, or a second data directory)."""
 global _current;_current=c;return c
