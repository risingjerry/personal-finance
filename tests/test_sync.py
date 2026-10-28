import json,sys,unittest
from pathlib import Path
from unittest import mock
import _env
from pfin import sync


class SyncTests(unittest.TestCase):
 def setUp(self):
  self.c=_env.isolated(sync={'commands':[{'name':'a','every_minutes':30,'run':[sys.executable,'-c','print(1)','{raw}']}]})

 def steps(self,**kw):
  with mock.patch.object(sync,'_step',side_effect=lambda name,argv,timeout=0:{'step':name,'ok':True,'argv':argv}):
   return sync.run(**kw)['steps']

 def test_commands_run_once_per_interval_and_expand_paths(self):
  first=[s for s in self.steps() if s['step']=='a']
  self.assertEqual(first[0]['argv'][-1],str(self.c.raw))
  self.assertEqual([s for s in self.steps() if s['step']=='a'],[])
  self.assertEqual(len([s for s in self.steps(now=True) if s['step']=='a']),1)

 def test_round_always_imports_resolves_and_refreshes(self):
  self.assertEqual([s['step'] for s in self.steps()][-3:],['import','resolve','library'])

 def test_lock_prevents_overlap(self):
  (self.c.home/'.ingest.lock').mkdir()
  self.assertIn('skipped',sync.run())

 def test_one_job_retires_the_old_ones(self):
  self.assertEqual(sync.plist()['ProgramArguments'][-2:],['sync','--json'])


FOUND={'jd':{'module':'connectors.jd','path':'/x','manifest':{'keepalive_hours':1}},'alipay':{'module':'connectors.alipay','path':'/x','manifest':{'keepalive_hours':24}}}
class ConnectorScheduleTests(unittest.TestCase):
 def setUp(self):
  self.c=_env.isolated(sync={'connectors':{'jd':{'fetch_hours':24},'alipay-work':{'keepalive':False,'fetch_hours':24}}},
   connectors={'alipay-work':{'connector':'alipay','args':['--home','~/w'],'account':'alipay:work'}})
  self.codes={}

 def steps(self,**kw):
  def step(name,argv,timeout=0,env=None):
   code=self.codes.get(name,0);return {'step':name,'ok':code==0,'code':code,'argv':argv}
  with mock.patch.object(sync,'_step',side_effect=step),mock.patch.object(sync.connect,'discover',return_value=FOUND):
   return [s for s in sync.run(**kw)['steps'] if s['step'] not in ('bank_sms','import','resolve','library')]

 def test_keepalive_then_fetch_into_the_folder_with_its_arguments(self):
  got=self.steps()
  self.assertEqual([s['step'] for s in got],['jd keepalive','jd fetch','alipay-work fetch'])
  self.assertEqual(got[1]['argv'][-2:],['--out',str(self.c.raw/'jd')])
  self.assertEqual(got[2]['argv'][2:],['connectors.alipay','fetch','--out',str(self.c.raw/'alipay-work'),'--home',str(Path.home()/'w')])
  self.assertEqual(self.steps(),[])  # nothing due again within the hour

 def test_lost_login_is_reported_and_stops_the_fetch_until_keepalive_sees_it_back(self):
  self.codes={'jd keepalive':3}
  got=self.steps()
  self.assertEqual([s['step'] for s in got if s['step'].startswith('jd')],['jd keepalive'])
  self.assertTrue(got[0]['ok'] and got[0]['needs_login'])
  self.codes={};self.assertEqual([s['step'] for s in self.steps(now=True) if s['step'].startswith('jd')],['jd keepalive','jd fetch'])

 def test_failed_fetch_waits_an_hour_before_retrying(self):
  self.codes={'jd fetch':6};self.steps()
  self.assertEqual([s['step'] for s in self.steps() if s['step']=='jd fetch'],[])
