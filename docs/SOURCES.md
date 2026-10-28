# 数据源

每个数据源是一个插件：把原始文件拆成行（parse），给行一个去重身份（identity），把行换成统一的记录（normalize）。
原始文件放在私有目录 `raw/<数据源名>/`，`pfin import` 只导入新文件，同一个文件导两次不会重复。
网页、App、交易所这类平台不用写数据源：写一个连接器（[CONNECTORS.md](CONNECTORS.md)），它的输出放进 `raw/<连接器名>/`，
框架用同一个解析器读所有连接器的统一记录。

## 已支持

| 数据源 | 放什么 | 怎么拿 | 角色 |
|---|---|---|---|
| `wechat` | 微信支付账单 `.xlsx` | 微信 → 我 → 服务 → 钱包 → 账单 → 下载账单 → 用于个人对账 | 通道（官方） |
| `wechat_notices` | 微信支付、银行公众号的通知卡片，聊天里的转账卡片 | `python3 -m pfin.collectors.wechat_notices`，或常驻 `wechat_watch` 实时采（都需要一个本机微信读取命令，写在 `config.json` 的 `wechat_reader`，不在本仓库） | 信号 |
| `bank_sms` | 银行短信 | `python3 -m pfin.collectors.bank_sms`（读 Mac 信息数据库，只存动账短信，验证码不落盘） | 信号 |
| `phone` | 手机 App 列表读屏：银行卡、信用卡、花呗、美团月付、抖音月付、微信账单、订单 | 手机自动化读屏后 `python3 -m pfin.collectors.phone_snapshot snapshot` | 资金 / 通道（低优先级） |
| 连接器的文件夹 | 连接器输出的统一记录 `*-rows-*.jsonl`：`alipay`（支付宝官方账单、商户流水）、`taobao`、`jd`（含白条）、`ctrip`（含拿去花）、`meituan`（月付）、`dianping`、`goofish`、`didi`、`rail12306`、`okx` | `pfin sync` 按配置定时拉，或 `python3 -m connectors.<名字> fetch --out raw/<文件夹>`（[CONNECTORS.md](CONNECTORS.md)） | 通道 / 资金 / 明细 |
| `okx` `binance` `bitget` | 交易所官方导出 CSV、只读 API 返回（同一文件夹也放 okx 连接器的记录） | 交易所后台导出，或 `python3 -m connectors.binance|bitget fetch`、`python3 -m connectors.okx api`（只读 Key） | 投资 |

`pfin sources` 列出每个数据集的角色，以及不进合并的原因。

## 加一个数据源

在 `pfin/sources/` 加一个模块，暴露 `SOURCE`（或 `SOURCES` 列表），再加进 `pfin/sources/__init__.py` 的 `MODULES`。
不想放进框架的（比如自己公司的系统），写在任何可导入的位置，在私有 `config.json` 的 `plugins` 里列出模块路径。

```python
from pfin.model import Dataset,Source,observation
from pfin.util import money,when

def parse(path,data):                      # 一个原始文件 -> (数据集, 位置, 行, 时区说明)
 if path.suffix!='.csv':return
 for i,row in rows_of(data):yield 'bill',f'row:{i}',row,'UTC+8'

def identity(dataset,row):                  # 跨文件去重用的原始单号；没有就返回 None
 return row.get('流水号') or None

def normalize(dataset,p,row):               # 一行 -> 统一记录；不是钱的移动就返回 None
 at,has=when(p['时间']);a=money(p['金额'])
 return observation(row,layer='money',role='funding',account='mybank:1234',at=at,has_time=has,
  amount=abs(a),sign=-1 if a<0 else 1,counterparty=p['对方'],title=p['摘要'],evidence='official_export')

SOURCE=Source('mybank',{'bill':Dataset('money','funding',priority=2)},parse,identity,normalize)
```

要点：
- **金额用 Decimal，永远是正数**，方向放在 `sign`。拿不到就是 `None`，不要填 0。
- **时间**：原件是什么时区就按什么时区读（`when(v, tz=...)`），框架统一换算到报告时区。
- **账户编号要和其他来源一致**（同一张卡的 App 和短信都叫 `mybank:1234`），否则覆盖规则和信号都对不上。
- **`ids` 里放能跨来源对号的单号**（交易单号、订单号），能对号就不靠金额+时间猜。
- **`Dataset.priority`**：同一账户有更权威的账单时，把它设高；读屏、接口这类不完整的设低。
- 加测试：`tests/` 下用 `_env.isolated()` 拿一个空的私有目录，不碰真实数据。

## 采集器

`pfin/collectors/` 负责把数据从设备或网页拿到 `raw/`，全部只读：不付款、不转账、不改设置。需要扫码、验证码、人脸的步骤由本人完成。
密钥只从密钥管理工具里按名字读，不写进代码、配置或日志。
