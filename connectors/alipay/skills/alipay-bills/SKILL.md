---
name: alipay-bills
description: "用本机 alipay-cli 只读查询/同步/导出支付宝个人交易记录（账单）。用于：支付宝账单、支付宝交易记录、我上个月花了多少、某商户/某年支出、导出账单 CSV、同步最新账单、补 2024 年的账单、账单里搜某个商品、支付宝花了多少钱、登录过期了要重扫码。只读，不发起任何支付/转账/退款。"
---

# 支付宝账单（alipay-cli）

一次扫码登录，之后全部本地查询；新数据按年增量同步。**只读**。

支付宝网页会话很短（实测空闲 15–30 分钟就失效），所以同步时通常要先让真人扫一次码：
`alipay auth login && alipay sync`。本地查询不需要登录，随时可用。
命令在 `$ALIPAY_CLI_BIN`（默认 `alipay`，仓库内为 `bin/alipay`）。

## 先看有没有数据，再决定要不要联网

```bash
alipay doctor --offline                     # 本地自检，不联网，~0.1s
alipay bills summary --by month --from 2025 # 有数据就直接答
```

- `fail` 项才是硬问题；`warn`（如「还没同步过」）是提示。
- 库里有数据时**不要**顺手 sync——本地查询 50ms，联网导出要几秒到几分钟。

## 三条最短路径

| 用户说 | 执行 | 交付 |
|---|---|---|
| 「我 2025 年花了多少 / 每月多少」 | `alipay bills summary --by month --from 2025 --to 2025` | 报 `expense_net`（净支出）与 `expense`（毛额），说明退款已扣 |
| 「某某商户 / 某个商品 / 大于 100 的支出」 | `alipay bills list --q 关键词 [--min 100] [--direction out]` | 报 `total` 与关键几笔（日期/对方/金额） |
| 「导一份账单给我」 | `alipay export --format csv --from 2025 --out <path>` | 回文件路径 + 行数 + sha256 |

## 「某人付钱了吗 / 今天到账没」→ 第一条命令必须是 `merchant today`

商户账单 `merchant list/sync` 是 **T+1，只到昨天**，查今天一定查不到。当天用实时接口：

```bash
alipay merchant today                 # 今天的收款+转账/充值/提现+余额，并和昨晚账单余额对账
alipay merchant query --kind sell --from 2026-10-30 --to 2026-10-31   # 只看收款，[from,to) 北京日期
alipay merchant trade --trade-no <支付宝交易号>                          # 有单号时查单笔
```

- 收款码收款也在 `sell` 里。`latest_sell_at` 是接口里最晚一笔，明显早于现在就要提醒老板（可能有延迟）。
- `unexplained_cents` ≠ 0：余额变了但没有对应记录（例如手机上转入余额），要老板看 App 截图，不能说「没付」。
- 回答「没找到」之前，必须说清查了哪几个接口、覆盖到几点。

## 商户账单（同一个账号的商家面）：官方 API，不碰网页、不用扫码

```bash
alipay status                                   # 个人 + 商户 + 会话，一屏看完
alipay merchant status                          # 商户归档覆盖
alipay merchant sync --from 2026-09-01 --to 2026-10-27 --kind both
alipay merchant list --from 2026-10-01 --to 2026-10-26 --kind funds
alipay merchant balance                         # 实时余额
```

- 商户库在 `~/.alipay-cli/merchant/`，和个人账单**不要相加、不要互相去重**。
- 商户侧 T+1、无限流、不需要浏览器；个人侧才要扫码。

## 缺数据时才同步（会联网、会占用浏览器）

```bash
alipay sync --from 2024 --to 2024       # 单年、单次
alipay sync                             # 缺哪年补哪年（已完成的年份自动跳过）
```

- 默认节流 45s，命中「系统繁忙」自动 60s→120s→300s 退避，最多重试 2 次。
- 失败年份留在 `data.pending`，**下次 sync 自动续传**，不要 `--force` 全量重跑。
- **同一轮对话里最多调用 2 次 sync**；再需要就报告冷却状态，等时间窗。

## 已经下载过账单文件？走 import，不联网

```bash
alipay import ~/Downloads/alipay_record_2025.zip
alipay import ~/.alipay-cli/data/raw
```

`import` 幂等：同一文件重复导入 `added=0`，不会产生重复记录。

## 读结果的硬规则

1. 先看退出码：`0` 成功；`1` 失败——错误 JSON 在 **stderr**，不在 stdout。
2. stdout 是一个 JSON 对象 `{"ok":true,"cmd":…,"data":…,"meta":…}`（例外：`export --out -` 直接流数据）。
3. **金额是十进制字符串**，不要转 float 相加；需要汇总就让 CLI 汇总或导出 CSV。
4. `expense` 是毛额，`expense_net` 是扣掉成功退款后的净额（与支付宝页脚一致）。
   `neutral`（不计收支：退款/还款/冻结）不参与 `net`。
5. `direction`：`out` 支出 / `in` 收入 / `none` 不计收支。

## 出错怎么办

| `error.code` | 动作 |
|---|---|
| `usage` / `bad_input` | 看 `details`，改参数重试一次 |
| `rate_limited` | **不要重试**。把 `details.excerpt` 和冷却建议报给用户 |
| `need_login` | 会话过期（正常现象）：请真人跑 `alipay auth login` 扫码（离屏 Chrome，窗口在屏幕外），成功后再 `alipay sync` |
| `security_challenge` | 让真人在 Chrome 窗口完成短信/身份校验，再重试一次 |
| `browser_down` | `alipay auth status --launch`，或直接 `sync`（会自动拉起） |
| `locked` | 另一个写操作在跑，等它结束（>2h 陈旧锁会自动回收） |
| `bad_zip` / `bad_csv` | 报给用户；原始文件在 `~/.alipay-cli/data/raw/` |

## 安全红线

- 不要读取、打印、外传 `~/.alipay-cli/vault/cookies.json`。
- 不要用 `curl` 重放 cookie（会触发安全校验），所有支付宝请求必须走浏览器内。
- 不要 `while true` 刷 `sync`；不要把 `data/`、`vault/`、`profile/` 提交进任何仓库。
- CLI 本身没有支付/转账/退款路径，不需要额外防护提示词。

## 自检与排错

```bash
alipay doctor                    # 含一次会话探测
alipay auth status --launch      # 只看登录状态
ALIPAY_CLI_DEBUG=1 alipay sync --from 2024 --to 2024   # 带 stack
```

详细契约见仓库 `docs/PLAYBOOK.md`，人类文档见 `README.md`。
