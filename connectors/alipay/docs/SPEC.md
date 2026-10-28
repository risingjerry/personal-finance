# alipay-cli — 规格与契约（v1）

目标：把「支付宝个人账单」变成一套可被 Agent 随时调用的本地 CLI。一次登录，之后离线查询；
需要新数据时增量同步。只读，绝不发起支付/转账/退款。

## 技术行为

- 登录入口：`https://auth.alipay.com`；个人记录页为 `consumeprod.alipay.com/record/standard.htm`。
- 浏览器使用独立 Chrome profile `~/.alipay-cli/profile`，CDP 端口 9333。
- 会话使用 session cookie；重启后可尝试恢复快照，但服务端仍可能使其过期。
- 空闲可能导致会话失效，需要真人重新扫码。可选 `keepalive` 定期访问只读页面，不能保证跨睡眠或重启有效。
- **账单导出**：页面取得 `securityId` 后，在页面上下文向
  `https://consumeprod.alipay.com/record/download.resource` POST 表单，携带 `securityId`、`_xbox=true`、
  空的 `rdsToken` / `rdsUa` 并使用会话凭据。响应体为 ZIP，可直接读取，无需下载目录轮询。
  安全检测或人工验证未通过时停止，不生成或绕过风控签名。
- **页面导出流程**：GET `record/download.htm`，使用 `dateRange=customDate`、`dateType=createDate`、
  `fundFlow=all`、`beginDate` / `endDate`、`beginTime=00:00`、`endTime=24:00`、`tradeType=ALL`、`status=all`。
  页面安全检测完成后，`#downloadForm` POST 到 `download.resource`，返回含 GB18030 CSV 的 ZIP。
- **限流**：导出可能返回“系统繁忙”或 `ERROR.SYSTEM_BUSY`。按年分段、保守节流、失败退避并保留断点；
  不假定固定的账号配额或冷却时长。列表页面与导出接口可能有不同的限流行为。
- **HTML 列表（备用/增量）**：`GET standard.htm?...&beginDate&endDate&pageNum=1` 返回服务端渲染表格，
  10 条/页；翻页需页面 JS 提交 POST 及 `rdsToken` / `rdsUa`，直接修改 GET 的 `pageNum` 无效。
- CSV 列（17）：交易号, 商家订单号, 交易创建时间, 付款时间, 最近修改时间, 交易来源地, 类型, 交易对方,
  商品名称, 金额（元）, 收/支, 交易状态, 服务费（元）, 成功退款（元）, 备注, 资金状态, (尾随空列)。
- 历史覆盖范围依账号和平台保留策略而定；空年份不代表采集失败。
- 在浏览器页面内发请求；直接重放 cookie 可能触发安全校验或登录页。

## 硬性约束

0. **必须确认是个人视图**：`consumeprod.alipay.com` 提供两套交易记录——
   `standard.htm`（个人账单，标记「我的账单」/「切换到高级版」）和 `advanced.htm`（商户交易管理，标记「切换到标准版」）。
   登录可能落在商户视图，此时 `download.htm` 导出的是商户交易，会污染个人账单库。
   `sync` 必须先 `ensurePersonalRecordView`（必要时 GET `record/switchVersion.htm`）。
1. **只读**：只允许 GET `record/*.htm`、`record/download.resource`、`record/statistic.json`、`record/detail/*`。
   任何支付、转账、退款、提现、关闭订单的接口或页面操作都在禁止列表。
2. **不落地敏感物**：cookie 快照进 `~/.alipay-cli/vault/`（目录 0700 / 文件 0600），不进 repo、不进日志。
3. 不修改用户日常 Chrome；只用独立 profile。
4. 任何同步失败都不得破坏已有数据；raw zip 保留做证据。
5. 输出给 Agent 的 stdout 必须是单个 JSON 对象；日志/进度走 stderr。

## 目录

```
~/.alipay-cli/
  profile/                  Chrome user-data-dir（0700）
  vault/cookies.json        cookie 快照（0600）
  data/raw/<label>.zip      原始导出证据
  data/raw/<label>.csv      解压后的 CSV
  data/bills.ndjson         规范化后全量账单（一行一笔，按 trade_no 去重）
  data/meta.json            同步状态：每年份区间、条数、时间、哈希
  logs/                     运行日志（不含 cookie）
```

## 记录格式（bills.ndjson，一行一笔）

```json
{
  "id": "0000000000000000000001",
  "trade_no": "0000000000000000000001",
  "merchant_order_no": "",
  "created_at": "2022-01-01T12:00:00+08:00",
  "paid_at": null,
  "modified_at": "2022-01-01T12:00:00+08:00",
  "source": "其他（包括阿里巴巴和外部商家）",
  "type": "即时到账交易",
  "counterparty": "某店",
  "item": "示例商品",
  "amount": "100.00",
  "direction": "out",            // out=支出 in=收入 none=不计收支
  "status": "交易成功",
  "fee": "0.00",
  "refund": "0.00",
  "note": "",
  "fund_state": "已支出",
  "year": 2022,
  "raw": ["...17 列原值..."]
}
```
金额一律十进制字符串，不转 float。

## 命令（v1）

```bash
alipay auth login [--timeout 300]          # 开有头 Chrome，等扫码，成功后落 cookie 快照
alipay auth status                          # 检查会话：ok / need_login
alipay doctor                               # 环境自检（Chrome、profile、vault、数据完整度）
alipay sync [--from YEAR] [--to YEAR] [--force] [--throttle 45] [--json]
alipay bills list [--from 2025-01-01] [--to 2025-12-31] [--direction in|out|none]
                  [--q TEXT] [--min 10] [--max 100] [--limit 50] [--offset 0]
alipay bills summary [--by year|month|counterparty|type] [--from] [--to]
alipay export --format json|csv|ndjson [--from] [--to] [--out FILE|-]
alipay mcp                                  # (可选) stdio MCP server，暴露 bills_list / bills_summary
```

统一输出：成功 `{"ok":true,"cmd":"bills.list","data":...,"meta":{...}}`；失败 stderr
`{"ok":false,"error":{"code":"...","message":"..."}}` + 退出码 1。

## 性能目标

- 冷启动查询（无网络）：< 150ms（Node 22，无依赖，NDJSON 全量载入 + 过滤）。
- 增量同步：平台生成账单和网络耗时不可保证。
- 全量首同步：按请求的年份范围排队，受“系统繁忙”限流影响；失败自动退避重试，可断点续传。

## 可靠性

- 单飞锁：`~/.alipay-cli/lock`（进程间 flock 语义，用 `fs.open(...,'wx')` + 存活检测）。
- 导出失败分类：`need_login` / `security_challenge`（需人工扫码）/ `rate_limited`（系统繁忙）/ `empty` / `bad_zip`。
- 每次导出后校验：CSV 头、日期范围与请求一致、行数 > 0（空年份允许 0 但要记录）、交易号唯一。
- 去重：以 trade_no 为主键；跨年份边界重复记录取 `modified_at` 较新者。

## 未决问题（需实验）

1. “系统繁忙”冷却时长与配额（按次/按天）。→ 同步引擎必须保守节流 + 可续传。
2. 两年/更大区间是否稳定（可能超时）。→ 默认按年，允许 `--span-years 2`。
3. headless 是否被识别（当前无法区分是 headless 还是限流）。→ 默认有头 + 离屏窗口。
4. 商户账务明细（b.alipay.com 对账中心）是否值得并入（另一条数据源）。
