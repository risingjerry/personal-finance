# 连接器（connectors）

一个连接器 = 一个平台的只读命令行工具。它只负责"从平台把本人的数据拿下来"，不管记账；框架（pfin）只通过下面的五个动作调用它、只读它输出的文件。任何一个连接器都可以脱离框架单独使用。

## 五个动作

```
python3 -m connectors.<名字> manifest                   这个连接器是什么（JSON）
python3 -m connectors.<名字> status [--online] [--json]  登录还在吗（--online 再发一次只读请求确认）
python3 -m connectors.<名字> login [--wait N] [--clear]  登录一次（扫码、验证码由本人完成）
python3 -m connectors.<名字> fetch --out DIR [...]       拉取：原始 JSON + 统一格式 rows
python3 -m connectors.<名字> keepalive [--json]          一次轻量只读请求，让登录会话保持活跃
```

输出（全部 0600）：
- `DIR/<prefix>-<时间>.json`：平台返回的原样内容（去掉了 cookie、签名这类值）
- `DIR/<名字>-rows-<时间>.jsonl`：一行一条统一记录（见下）

退出码：0 成功；2 用法错误；3 需要登录；4 被风控拦截；5 部分失败；6 网络错误；70 内部错误（不打印可能含敏感值的细节）。

## 统一记录（rows）

`connectors/kit/rows.py` 的 `make_record`：

| 字段 | 说明 |
|---|---|
| `source` | 记录来源，如 `taobao`、`jd-baitiao`、`ctrip-naquhua` |
| `record_type` | `order` 订单 / `bill` 账单（信用产品的消费）/ `transaction` 资金流水（还款、退款、充提）/ `trip` 行程 |
| `id` | 稳定编号（`stable_id`），同一条记录每次拉取都一样 |
| `occurred` | 北京时间 `YYYY-MM-DD HH:MM:SS` |
| `amount` / `direction` | 正数字符串，`out` 付出 / `in` 收入 |
| `merchant` / `title` / `status` | 对方、内容、状态，照原样 |
| `extra` | 平台特有的字段 |

## 接进框架

框架不导入连接器的代码，只做三件事：

1. **发现**：`connectors/` 下每个定义了 `CONNECTOR` 的包；`config.json` 的 `connector_paths` 可以再加目录（比如自己公司的支付通道，放在私有仓库）。`pfin connectors` 列出全部。
2. **调用**：`pfin sync` 按 `config.json` 的 `sync.connectors` 以子进程运行 `keepalive` 和 `fetch --out raw/<文件夹>`；`pfin status`、`pfin login` 直接转给连接器。
3. **读取**：`raw/<文件夹>/` 里的 `*-rows-*.jsonl` 由同一个解析器（`pfin/sources/connectors.py`）读进证据库，数据集名是 `<source>-<record_type>`（如 `ctrip-naquhua-bill`），身份是 `(source, record_type, id)`，所以同一条记录拉多少次都只算一次。原始 JSON 留作证据，不解析。

文件夹默认就是连接器名。同一个连接器用不同参数跑（比如第二个支付宝账号），在 `config.json` 里写：

```json
"connectors": {"alipay-work": {"connector": "alipay", "args": ["--home", "~/.alipay-cli-work"], "account": "alipay:work"}}
```

`account` 是这个文件夹里资金记录记到的账户。每种记录在合并里扮演什么角色（通道、资金、明细）、怎么归一，写在 `pfin/sources/connectors.py` 的 `DATASETS` 和 `NORMALIZE`；还没写归一规则的数据集会被导入、保存，但不参与合并（`pfin sources` 里能看到原因）。

## 写一个连接器

```
connectors/<名字>/
  __init__.py     CONNECTOR = Connector(...)：怎么拉、怎么汇总、怎么转 rows、怎么探活
  __main__.py     from . import CONNECTOR; from ..kit.runner import run; run(CONNECTOR)
  reader.py       读取平台的代码（分页、签名、解析）
  blocking.py     认出登录页、风控页、签名墙
  tests/          离线测试；需要真实抓包的测试从私有目录读，没有就跳过
```

参考 `connectors/taobao/`。要点：
- **只读**：只调用查询接口；不点击、不提交、不支付、不确认、不取消。
- **不碰验证码**：遇到滑块、验证码、风控签名就停下报错，不绕过、不伪造签名。
- **秘密不出内存**：cookie、令牌、API Key 不写进文件、日志、命令行参数或报错信息。浏览器登录从你的 Chromium 系浏览器读（只读副本；`PFIN_BROWSER` 指定 chrome、edge、brave、arc、dia），会话轮换存本机钥匙串；API Key 按名字从环境变量、钥匙串（服务名 `pfin`）或 `~/.config/pfin/secrets.json` 配置的密码管理命令读。
- **金额**用字符串或 Decimal，不用浮点数；拿不到就是 `None`，不填 0。
- **限速**：每个连接器设自己的最小请求间隔（`min_delay`）。

## 现有连接器

| 名字 | 平台 | 登录方式 |
|---|---|---|
| `taobao` | 淘宝「已买到的宝贝」 | 浏览器 |
| `jd` | 京东订单 + 白条 | 浏览器 |
| `ctrip` | 携程订单 + 拿去花 | 浏览器 |
| `dianping` | 大众点评新餐饮订单 | 浏览器 |
| `goofish` | 闲鱼买到 / 卖出 | 浏览器 |
| `meituan` | 美团月付账单与还款 | 浏览器 |
| `rail12306` | 12306 订单、电子发票 | 扫码 |
| `didi` | 滴滴行程 | 独立浏览器 |
| `okx` `binance` `bitget` | 交易所 | 扫码 / 只读 API Key |
| `alipay` | 支付宝个人账单、商户流水（Node） | 扫码 / 开放平台密钥 |
