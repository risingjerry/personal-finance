"""`rules._compiled` caches prepared rules under `id(config)`, which Python can hand to a different object once the
original is collected. The guard is an identity check on the stored object plus a clear-on-miss; this test plants the
exact collision so the guard cannot be dropped without a failure."""
import unittest
import _env
from pfin import config
from pfin import rules as R


def cfg(book):
 return config.Config(home=_env.isolated().home,rules=[{'when':{'account':'x'},'book':book}])


class RuleCache(unittest.TestCase):
 def setUp(self):
  R._RULES.clear()

 def test_reused_id_recompiles_instead_of_serving_the_old_rules(self):
  old=cfg('OLD')
  self.assertEqual(R._compiled(old)[0][2]['book'],'OLD')
  fresh=cfg('NEW')
  # Plant what a collected `old` leaves behind: a cache entry whose key is a reused id, still holding a *different*
  # object. Without the identity check, `fresh` would be served `old`'s compiled rules.
  R._RULES.clear()
  R._RULES[id(fresh)]=(old,R._compiled(old))
  self.assertEqual(R._compiled(fresh)[0][2]['book'],'NEW')
  self.assertIs(R._compiled(fresh)[0][2]['book'],'NEW')

 def test_same_object_hits_the_cache(self):
  c=cfg('SAME')
  first=R._compiled(c)
  self.assertIs(R._compiled(c),first)

 def test_second_config_does_not_inherit_the_first(self):
  self.assertEqual(R._compiled(cfg('A'))[0][2]['book'],'A')
  self.assertEqual(R._compiled(cfg('B'))[0][2]['book'],'B')


if __name__=='__main__':unittest.main()
