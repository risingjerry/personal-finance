import sys,unittest
from pathlib import Path
import _env
import pfin.collectors.wechat_watch as W
def msg(chat,kind=1,subtype=0):return {'event':'message','data':{'chat':chat,'message':{'kind':kind,'subtype':subtype}}}
class WatchFilterTests(unittest.TestCase):
 def test_notice_chats_transfers_and_gaps_trigger(self):
  self.assertTrue(W.relevant(msg('gh_3dfda90e39d6')))          # 微信支付
  self.assertTrue(W.relevant(msg('gh_4496ecf4647e')))          # 买单吧
  self.assertTrue(W.relevant(msg('wxid_x',49,2000)))           # transfer card in a chat
  self.assertTrue(W.relevant({'event':'gap','data':{'chat':'gh_3dfda90e39d6'}}))
 def test_ordinary_chat_and_group_messages_do_not(self):
  self.assertFalse(W.relevant(msg('wxid_x')))
  self.assertFalse(W.relevant(msg('123@chatroom',3)))
  self.assertFalse(W.relevant({'event':'gap','data':{'chat':'wxid_x'}}))
if __name__=='__main__':unittest.main()
