# 收件箱草稿采集（独立工具）

这是一个单独运行的本地程序：读取一份**已经结构化的收件箱**，去重，只生成待复核草稿。它不启动 PyQt，也不走现有客服应用的发送、备用回复、商品卡或转接。

**实店没有接入。** 商家页面的选择器尚未核对，本版本拒绝启动真实采集，也不会代填账号或自动重登。

## 现在就能验收

在 `tools/inbox_collector` 目录：

```bash
python -m inbox_collector check-config --config config.fixture.example.json
python -m inbox_collector validate-mapping --mapping mappings/pinduoduo_chat_merchant.json
python -m unittest discover -s tests -t . -v
python -m inbox_collector demo --data-dir /tmp/inbox-collector-demo
```

`validate-mapping` 对商家 mapping 必须以非 0 退出。`mappings/generic_local_inbox.json` 只验证本地通用页面，可以通过，但不能拿去开实店。`demo` 不启动浏览器。`config.browser-local.example.json` 的地址是本机 9 端口，用来说明配置形状；真正打开浏览器要另起一个本机页面再执行 `run`。商家网址在配置加载时就会被拒绝。

示例草稿见 `sample_output/mock_draft.example.json`。`draft_text` 固定为模拟句，不是模型写给买家的答案。

## 在 Mac mini 上安装

只在这个目录里操作，不要运行仓库根目录的 `app.py`。下面的命令不会打开商家页面。

```bash
cd tools/inbox_collector
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[browser]"
python -m playwright install chromium
python -m inbox_collector check-config --config config.login-prep.example.json
```

也可以执行 `sh scripts/install_mac.sh`，它只做上面的安装，不会启动浏览器。

首次登录必须人工完成。在同一个目录、同一个虚拟环境里：

```bash
source .venv/bin/activate
python -m inbox_collector login-prep --config config.login-prep.example.json
```

这会新建 `data/own-browser-profile`（权限仅本人可读写），并用有界面浏览器打开 `https://mms.pinduoduo.com/chat-merchant/index.html#/`。未登录时站点会跳到 `https://mms.pinduoduo.com/login/`，并带上只指回该客服页的 `redirectUrl`。这个跳转会留在窗口里。其它域名、发送接口、或指向站外的 `redirectUrl` 会关闭窗口。macOS 上如果装了 Chrome，会用这个新 profile 启动 `/Applications/Google Chrome.app`，不会打开现有 Chrome 用户目录，也不会附着已经在跑的窗口。请自己登录。关掉窗口后进程结束。

这一步不采集消息、不生成草稿、不调用 Webhook。`real_store_login_verified` 仍是 false，`production_running` 仍是 false。`run` 不能代替 `login-prep`，采集模式仍然拒绝这个商家地址。

Profile 里会留下登录态，只放在本机，不要复制、同步或提交。程序不读取 Cookie 文件。

当前已经打开的专用 Chrome 不要为了新代码重启。`diagnose-dom` 不会另开浏览器，也不会附着你原来的 Chrome。它只请求**正在运行的新版** `login-prep` 导出当前页的标签名、属性名和安全 class 结构。客户正文、身份、原始 id 和 URL 查询不会写出。`data-v-*` 和 `tabindex` 只记为不稳定，不会当成选择器。

没有进程在听时，结果是 `diagnostic_error` / `listener_not_running`，`nodes` 为 null。空结果不是“平台不支持”。字段不存在、会话未选中、诊断失败是三种不同状态。

```bash
python -m inbox_collector diagnose-dom --data-dir data --timeout-seconds 2
```

这次代码不会替你执行这条命令去碰已经登录的窗口。先在本机用测试看结构导出：

```bash
python -m unittest tests.test_dom_diagnose tests.test_browser_e2e.BrowserE2ETest.test_diagnose_uses_the_open_page_without_navigation -b
```

采集仍然要等真实 DOM 核对之后。在那之前：

```bash
python -m inbox_collector validate-mapping --mapping mappings/pinduoduo_chat_merchant.json
```

这条命令会失败。去重、离线告警和 BOT 接入都还要在你自己的机器上验收。Webhook 保持关闭，不要把别的 BOT 密钥写进配置。

## 采样间隔

`sample_interval_seconds` 默认 120，只允许 60–120。这是**本程序自己的前台循环参数**，不是 Grok Bot 的定时任务。不要把它拆成多次 5 分钟以内的机器人 routine，也不要接管 Grok Bot 的浏览器。

`run` 必须显式选择其一：

- `--once`：采集一轮后退出
- `--foreground --cycles N`：前台最多 10 轮，进程退出后不会自启

配置不完整、mapping 未验证或 `mode=real` 时，状态保持 `stopped`，不能报 `running`。

## 行为

- 第一次完整、可校验的收件箱只建立基线，不回补历史草稿。
- 身份是「会话 key + message id + 方向」。红点、未读数、待办任务、预览摘要都不算新消息。
- 消息时间必须带时区。列表上的「13:32」记在 `list_time_text`，不能当作完整消息时间。
- 没有稳定 message id，或买家消息不是秒级时间，或回复状态不可靠：不生成可发送草稿。不可靠时记 `needs_human_review`。整页对不上时暂停。
- 可靠的人工回复或平台机器人回复：跳过，不另起一份待发草稿。
- 草稿写出前若已有商家消息盖过该买家消息，草稿作废并记下原因。
- 同一会话本轮的多条新消息合成一条复核记录，消息本身仍逐条保存。
- 买家正文只当不可信数据。售前咨询和提示词注入都不会被执行。
- 基线之后才出现的消息才会进入草稿。手机上已读不会因为未读数变 0 而被滤掉；前提是列表里仍有稳定 id。列表如果根本没有这些字段，程序拒绝处理，而不是猜测。

`open_conversation` 默认 `false`。将来若打开会话去补字段，商家页面可能把会话标成已读。当前没有已验证的会话选择器，因此这个开关也不能启动实店。

## 草稿与 Webhook

| 配置 | 结果 |
| --- | --- |
| `draft_provider=none`（未写时的默认） | `pending_generation`，不编造回复 |
| `mock` | 本地模拟草稿，明确标注未发送 |
| `model` | 仍是待生成。本版本不调用云端模型 |
| `webhook` | 只入本地 outbox。不是买家消息发送 |

Webhook 要同时满足才可能发出 HTTPS POST：`webhook.enabled=true`、`https://` URL、`webhook_secret_env` 指向的环境变量、`allow_network=true`，以及进程环境 `INBOX_COLLECTOR_WEBHOOK_NETWORK=1`。密钥不能写进配置文件。本仓库没有、也不读取现有 Bot 密钥。

HTTP 200 只表示传输被接受，状态是 `webhook_transport_accepted_awaiting_receipt`。响应正文会丢掉，因为 Grok Bot 的回执格式还没有约定。传输失败保持 `webhook_transport_failed`，不会当成已完成。没有 `sent_to_buyer` 状态。

远端告警同样默认关闭，而且**本版本没有告警发送实现**。`claims_user_wakeup` 恒为 false。断网、登录页、验证码、账号在别处登录、解析失败会写入本地告警并暂停；相同告警只累加次数。不会自动重登，也不会绕过验证码。连续失败达到 `max_consecutive_failures` 后进入 `failure_ceiling`，需要 `resume --acknowledge-hard-stop` 才允许再跑。

## 本地数据

开发数据放在配置的 `data_dir`（示例是 `./data`，已忽略）。SQLite 和草稿目录权限是 owner-only。草稿和库里可能有客户正文，请只留在本机；默认 30 天后清空正文，但保留身份哈希，避免旧消息被重新生成。备份就是复制这个目录，过期后删除目录即清除。配置里禁止出现密码、cookie、token 或明文密钥。浏览器 profile 若将来启用，只使用 `data_dir/own-browser-profile`，不读取现有 Chrome 用户目录。

日志只记状态码和计数，不记正文、密钥或页面快照。

## 从「代码已写好」到「真实运行」还缺什么

代码能打开的是**新建的有界面 Chromium** 和一块空的 `data_dir/own-browser-profile`。这不等于实店已登录，也不是操作者原来的 Chrome 主账号。

1. **实店登录还没完成。** `login-prep` 只负责把专用浏览器打开到允许的登录页，等人自己登录。它不把登录标成已验证，也不开始采集。采集模式在 `real_store_login_verified=false` 时仍然不会把商家页当收件箱来跑。程序不填密码。登录页、验证码和「账号在别处登录」只写本地告警，不会自动重登，也不会远端叫醒。`claims_user_wakeup` 恒为 false，`alert_delivery` 是 `local_only`。
2. **真实 DOM。** `mappings/pinduoduo_chat_merchant.json` 仍然没有选择器。`live_dom_verified` 恒为 false。本地 mapping 的 `data-*` 只用于本仓库的通用收件箱，不能套到商家页面。
3. **BOT。** 没有 Webhook 密钥，也没有确认现有 BOT 只出草稿。默认 `bot_integration` 是 `blocked_not_connected`。即使以后配齐 URL、secret 和网络开关，状态也只是 `webhook_configured_unverified`。传输成功不是草稿已生成，更不是 BOT 的真实回复。mock 草稿带「模拟草稿」字样。
4. **调度。** 前台循环的间隔仍是 60–120 秒，由本程序自己 `sleep`。它不是 Bot routine，也不会去接管别的浏览器或调试端口。

`mode=real` 保持停止。不要把 `browser_local` 的测试浏览器当成实店已接通。
