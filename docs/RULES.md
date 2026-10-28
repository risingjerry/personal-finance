# 私有配置与归属规则

所有私有内容都在一个目录里（环境变量 `PFIN_HOME`，默认 `~/Library/Application Support/personal-finance`），不进 git。

```
raw/                原始文件，每个数据源一个文件夹，永远不改
evidence.sqlite     证据库：原始文件的每一行（去重），以及合并结果 events / links 两张表
config.json         时区、本人名字、账户、卡号、采集器设置、插件
rules.json          归属规则（有顺序）
overrides.json      逐笔决定（按交易号 / 订单号 / 转账号）
contacts.json       微信聊天 ID → 名字
library/            按类别整理的浏览视图，可删可重建（pfin library）
recon/              每次合并的报告（pfin resolve）
sync-state.json     pfin sync 记下每个外部采集器上次运行的时间
```

## config.json

```json
{
 "timezone": "Asia/Shanghai",
 "owner": ["张三", "张三的店"],
 "accounts": {"alipay:work": {"book": "business", "label": "店铺收款支付宝"}},
 "cards": {"phone/bocom_bank-account": "1234", "phone/bocom-card": "5678"},
 "wechat_reader": "my-wechat-reader",
 "wechat_client": "main",
 "phone_out": "/path/to/phone/reads",
 "python": "/path/to/python-with-openpyxl",
 "plugins": ["mybiz.sources"],
 "plugin_paths": ["~/code/mybiz-pfin"],
 "connectors": {
  "alipay-work": {"connector": "alipay", "args": ["--home", "~/.alipay-cli-work"], "account": "alipay:work"}
 },
 "connector_paths": ["~/code/mybiz-connectors"],
 "sync": {
  "wechat_notices": false,
  "connectors": {
   "jd": {"fetch_hours": 24},
   "didi": {"fetch_hours": 168, "args": ["--browser", "--max-pages", "3"]},
   "binance": {"fetch_hours": 168, "keepalive": false}
  },
  "commands": [
   {"name": "my-script", "every_minutes": 60, "run": ["/path/to/script", "--out", "{raw}/mysource"]}
  ]
 }
}
```

- `timezone`：报告时区，全系统只在这里配。
- `owner`：银行和 App 上显示的本人名字（含本人名下店铺的显示名），用来认出"自己账户互转"。
- `accounts`：只为一本账服务的账户，事件默认归那本账。
- `cards`：读屏列表不显示卡号时，用哪个后四位。
- `plugins`：额外的数据源模块（如自己公司的系统）。
- `connectors`：用别的参数跑同一个连接器、输出到另一个文件夹（比如第二个支付宝账号），以及这个文件夹的记录记到哪个账户。文件夹名就是连接器名时不用写。
- `connector_paths`：框架之外的连接器（比如自己公司的支付通道）所在的目录。
- `sync`：`pfin sync` 每轮做什么。`connectors` 里每个文件夹：按连接器声明的间隔保活（`"keepalive": false` 关掉，比如要扫码的平台），`fetch_hours` 小时拉一次，`args` 是额外的拉取参数；登录失效会列在 `needs_login`，不算失败。`commands` 是其他外部命令，每个有自己的最小间隔（分钟）；`{raw}`、`{home}` 会换成私有目录。`wechat_notices` 为 true 时由 sync 读微信卡片（没开实时监听时用）。加新数据源只改这里，不新增定时任务。

## rules.json

```json
{"rules": [
 {"when": {"account": "alipay:work", "field": {"merchant_order_no": "^SHOP"}}, "book": "business", "project": "shop", "note": "店铺订单号前缀"},
 {"when": {"dataset": "okx-c2c-order"}, "book": "investing"},
 {"when": {"text": "某某借"}, "book": "personal", "kind": "internal",
  "loan": {"principal": "10000.00", "payments": ["880.00", "...", "880.12"], "first": "2026-10"}},
 {"when": {"text": "某客户"}, "book": "business", "project": "consulting"},
 {"when": {"account": "alipay:work", "text": "转账", "sign": -1, "round": 1000}, "book": "business", "project": "supplier"}
]}
```

条件（`when`，全部满足才算命中）：

| 条件 | 含义 |
|---|---|
| `text` | 对方或内容里包含这段文字（不分大小写） |
| `account` | 账户编号完全相同 |
| `sign` | -1 只管付出，1 只管收入 |
| `round` | 金额是它的整数倍（如整千） |
| `dataset` | 这条记录来自某数据集（如场外订单） |
| `field` | 原始字段匹配正则（如商户订单号前缀） |

没有 `dataset` / `field` 时只看事件的主记录；有的话，所有条件在挂上的**同一条**记录上检查
（例如从个人支付宝付款给自己的店，店铺那一侧的订单号命中规则）。

结果：`book`、`project`；可选 `kind`（强制事件类型，如还款记 `internal`）、`loan`（分期还款计划，拆出利息）、`note`（为什么）。

**第一条命中的规则生效**，窄的规则放前面。

## overrides.json

逐笔核实过的结论，按事件里任一单号（交易号、订单号、`wxtransfer:<转账号>`）对上，优先级最高：

```json
{"2026010122001400000000000001": {"book": "business", "project": "resale", "note": "转卖的手机", "evidence": "owner"},
 "wxtransfer:1000050001202601010000000000001": {"book": "personal", "kind": "internal", "note": "家里人临时周转，次日原数退回"}}
```
