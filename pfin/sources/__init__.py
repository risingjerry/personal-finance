"""Source registry. A source is one module exposing SOURCE or SOURCES (pfin.model.Source); its raw files live in
raw/<name>/. To add one, write a module here and list it in MODULES, or name it in config.json "plugins"
(docs/SOURCES.md). A connector needs no source of its own: any raw folder holding its rows files is read by
pfin.sources.connectors (docs/CONNECTORS.md). Loaded on first use."""
from importlib import import_module

MODULES=['wechat','wechat_notices','bank_sms','phone','taobao_web','exchanges']
# Modules whose factory(name) builds a source for a folder no module names (a connector's output folder).
FACTORIES=['connectors']
_sources=None


def all_sources():
 global _sources
 if _sources is None:
  from ..config import cfg
  _sources={}
  for m in [f'{__name__}.{m}' for m in MODULES]+list(cfg().plugins):
   mod=import_module(m)
   for s in getattr(mod,'SOURCES',None) or [mod.SOURCE]:_sources[s.name]=s
 return _sources


def get(name):
 s=all_sources()
 if name not in s:
  for m in FACTORIES:
   f=import_module(f'{__name__}.{m}').factory(name)
   if f:s[name]=f;break
 return s.get(name)


def reset():
 """Forget loaded sources (after switching config)."""
 global _sources;_sources=None
