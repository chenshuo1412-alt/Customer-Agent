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

`validate-mapping` 必须以非 0 退出，并标明 mapping 未验证。`demo` 只使用内置模拟会话，退出后进程结束，不会留在后台。

示例草稿见 `sample_output/mock_draft.example.json`。`draft_text` 固定为模拟句，不是模型写给买家的答案。

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

1. **登录。** 需要操作者在独立的有界面 Chrome/Chromium 里自己打开 `https://mms.pinduoduo.com/chat-merchant/index.html#/` 并完成登录。程序不填账号密码。遇到登录页、验证码或「账号在别处登录」只告警暂停。本版本的 `launch_independent_browser` 直接拒绝，不会启动浏览器。
2. **真实 DOM 核对。** `mappings/pinduoduo_chat_merchant.json` 的 `validated` 为 false，`selectors` 为空。必须在授权前提下核对稳定会话 key、message id、方向、带时区的消息时间、回复状态及其来源，并写成 mapping，且 `validation_status` 为 `verified_against_live_dom`。在此之前不能把 fixture 的 `data-*` 选择器拿去套真实页面。
3. **Webhook 授权。** 操作者自行提供 URL 和 secret，并打开上面的网络开关。还要定义回执里如何用 `event_id` 交回草稿。在那之前，投递成功不能当成已经拿到回复。
4. **模型。** 若不用 Webhook 而用独立模型，需要另一次明确开启；当前代码即使配了模型也不会发请求。
5. **数据授权。** 没有授权时不要把真实买家消息放进这个库。

即使以后补上 mapping，也还要删掉运行器里的 `real_mode_refused` / `real_store_not_authorized` 硬拒绝，并单独验证浏览器。在那之前，health 里的 `real_store_connected` 保持 false。
