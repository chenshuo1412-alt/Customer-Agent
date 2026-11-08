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

当前已经打开的专用 Chrome 不要为了新代码重启。`diagnose-dom` 不会另开浏览器，也不会附着你原来的 Chrome。它只请求**正在运行的新版** `login-prep` 对当前页做一次采样。旧版只从 `body` 往下走 8 层、最多导出 300 个节点，并且不进入 iframe 和 shadow。节点没到 300 时 `truncated` 仍是 false，所以深度被截断也会看起来像“已经看完”。会话列表因此会被误当成消息区。

新版仍只读当前页。它会进入已经渲染、且能确认属于客服页的同源 frame（正式页只限 `mms.pinduoduo.com/chat-merchant` 及其同源客服 frame；本地测试只限 `127.0.0.1` / `localhost`）。读不到的 frame、登录/密码 frame、支付 frame，以及不能确认是客服区域的 frame，只记跳过原因，不绕过浏览器限制。open shadow 会进入。closed shadow 读不到，结果里会标明这个限制，但不会把它说成“页面没有消息”。已知的消息区 class（`content-box`、`reply-box`、`chat-item`）和会话列表 class 分开记录。`loadMoreMsg` 单独出现不算消息区。浅层列表不会被当成消息。

导出的仍是标签名、属性名、安全 class、frame 计数和来源分类、覆盖范围、真实截断原因，以及原来的值形状分类。`li.chat-item` 只算会话列表，不能单独让 `message_region` 为 true。消息 preflight 只在 `content-box` 里的 `li.onemsg` 上关联子孙时间节点和邻接日期头，`merchantMessage` / `buyer-item` 只记结构、不从名字判断方向，`isread` 只记 `read_unknown`。同一页会做两次观察比较可见行 id，但同页稳定不等于跨重启稳定。原始 id、正文、身份、日期原文和 URL 不会写出。生产 mapping 仍是 false。

`login-prep` 正常结束时会关闭它启动的 Chrome。再次打开会复用 `data/own-browser-profile`，已写在这个 profile 里的登录可以留下来。不要附着 `remote-debugging-pipe`。旧进程不会执行新的 `geometry-readonly`；未知 `op` 会被拒绝，不会再静默退回整页导出。换成这一版之后，重复诊断只写请求文件。

只读几何，不输入、不点击、不发送：

```bash
python -m inbox_collector diagnose-dom --data-dir data --timeout-seconds 5 --op geometry-readonly --probe-phrase '客服测试2006'
```

退出码 0 仅当 `status` 是 `structure_ready`（或旧的 `geometry_ready`）。短语不存在、不唯一、零矩形、几何不稳、选中项多于一个、高亮候选超过 8 个、或两边 currentuid 哈希不一致，退出码都是 2，字段仍然都在。`handler` 必须是 `geometry-readonly`。`samples` 为 8。

`structure` 只描述这句所在行、紧邻的下一行、消息面板和标题，以及当前可见的高亮节点。每处只有标签、安全 class、矩形和最多 4 层祖先。深度 4，高亮候选最多 8 个，写出的高亮节点最多 4 个。超出时 `truncated` 为 true，并写 `candidate_cap` 或 `node_cap`。没有看到角色原文时 `role_label` 是 `unknown`，不会从 `buyer-item`、左右位置或 class 名推断。角色原文如果在行外的父容器上，`role_source` 是 `ancestor_allowlist_label`。`id` / `currentuid` 会在该行、子孙和祖先上找，只报告 `format`、`sha256` 和位置（`on_row`、`on_descendant`、`on_ancestor`、`absent`）。短数字仍是 `short_index_not_uid`，哈希为空。

`conversation_link` 只有两边 currentuid 哈希都在且相等时才是 `hash_match`。没有共同标识是 `not_comparable`，`shared_identifier_refused` 为 true。多个高亮是 `selection_ambiguous`。这不是平台做不到，只是这次没拿到能绑定的证据。`session_pairing.durable` 恒为 false：`ephemeral_structure_sha256` 只给这次渲染里的人工对照，页面一变或进程重启就失效，不能当作平台 UID，也不能跨重启认同一会话。`selectors_inferred` 和 `production_send_allowed` 都是 false。未知 `op` 或多余字段仍直接拒绝。

正在运行的旧 login-prep 不会执行短句核验或测试发送。换成这一版之后，重复诊断和一次测试发送只写请求文件。

```bash
python -m inbox_collector diagnose-dom --data-dir data --timeout-seconds 5 --summary --probe-phrase '客服测试2006'
```

`phrase_probe.unique` 为 true 表示这句在当前可见消息区恰好一条。`following_row` 是它后面那一条可见行的 tag、安全 class 和结构角色，不带原文。`later_visible_rows` 不是 0 时仍然拒绝发送。`composer_report` 是这次要看的完整结构：从可见输入框向上，停在还没包住 `li.onemsg` 的那一层，并带上同层、不含消息行的相邻容器，再向下最多 6 层、80 个节点。节点只有 tag、安全 class、role、可见性、层级和标签形状。`allowlist_candidates` 只统计这个 composer 里面、可见、可点击、且文字精确是「发送」或「发送 + Enter」的节点。composer 外面的按钮在 `outside_buttons`，`inside_composer` 为 false，不能当成发送控件。`truncated` 为 true 表示这次采样不够，也不能当成已经找全。`label_kind` 为 `other_redacted` 是已经读过渲染文字但没有命中；不要把这种按钮升级成发送。结果不包含这句原文、姓名、id 或时间。

已批准的固定原文只走下面这条命令，不走 `test-send`，也不调用本机模型。授权只绑定这一次挑战、这一句原文、过期时间和当前 `login-prep` 窗口。页面上没有字面角色标签、或同时有两个 active，都不拿来当身份，也不因此拒绝。`currentuid` 不读取、不发送。输入框里已有草稿就拒绝，不覆盖。发送前再核对同一面板；面板或消息 id 变了就作废。ledger 一旦进入输入就封存，不确定或失败后不再自动重发。看见新的原文气泡只表示页面上出现了，不表示买家端已送达。

正在跑的旧 `login-prep` 不会执行这条命令。正常退出后重新打开才会发布本窗口标记；专用 profile 仍复用，不要热注入。

```bash
python -m inbox_collector confirmed-text-send --data-dir data --challenge '客服配对1109-A7' --reply '测试收到，客服回复链路验证中。' --buyer-source own-account --expires-seconds 600 --opt-in single-confirmed-text --timeout-seconds 60
```

退出码 0 仅当 `status` 是 `page_visible`，并且 `delivery_claimed` 和 `production_send_allowed` 都是 false。其它状态退出码 2。

只对这一句发送一次，不是全店托管：

```bash
python -m inbox_collector test-send --data-dir data --probe-phrase '客服测试2006' --opt-in single-test-conversation --timeout-seconds 60
```

成功时 `status` 是 `test_sent`，`receipt` 是 `matched`，`production_send_allowed` 和 `storewide_auto_reply` 都是 false。若这句后面已经有回复，结果是 `refused_newer_or_reply_present`。那时只需要买家号再发一条新的唯一短句，并改用那句做 `--probe-phrase`。

本机 Ollama 模拟发送用一条独立命令。它会自建临时浏览器 profile 和临时页面，只访问 `http://127.0.0.1:11434` 的 `qwen-kefu:latest`，不碰已经登录的 Chrome：

```bash
python -m inbox_collector auto-reply-sim
```

通过时 `status` 是 `simulated_sent`，`generator_transport` 是 `urllib_loopback`，`process_loopback_http_completed` 是 true，并且 `process_loopback_http` 里有对本机 `127.0.0.1:11434` 的 `GET /api/tags` 和 `POST /api/generate`，状态 200。`composer_commits_seen` 是 1。`real_buyer_send` 和 `customer_service_quality_verified` 都是 false。云端测试用的是注入传输和临时端口，`injected_test_transport` 为 true，不会去绑定 11434。冷启动先把当时可见的行记为历史，不回复；同文不同 id 的新行仍会各算一条。没有回执则 `paused_uncertain`，不会再发。`production_send_allowed` 仍是 false。

`page_acceptance` 里 `send_label_button_count` 为 0 不表示没有发送控件。同时看 `near_input` 的可见自定义节点、`allowlist_send_label_hits`，以及 `unique_row_classes`。`send_control_absence_not_concluded` 为 true 表示这次没有把“没看到 button”当成没有发送按钮。

一次采样不能证明 id 换消息后仍稳定，所以 `can_use_for_dedupe` 保持 false。格式说明不会被当成时间；形状像时间也只记为需要人工确认含义，`can_treat_time_as_message_time` 保持 false。其它文字只记为 `other_redacted`。隐藏行、重复 id、或 `aria-rowcount` 大于当前渲染行数时，证据记为不充分。结果里的 `human_checks` 是最低必要的人工核对，不是采集成功。生产 mapping 仍是 false。

没有进程在听时，结果是 `diagnostic_error` / `listener_not_running`，`nodes` 为 null。空结果不是“平台不支持”。字段不存在、会话未选中、诊断失败是三种不同状态。

```bash
python -m inbox_collector diagnose-dom --data-dir data --timeout-seconds 2
```

这次代码不会替你执行这条命令去碰已经登录的窗口。先在本机用测试看结构导出：

```bash
python -m unittest tests.test_dom_diagnose tests.test_browser_e2e.BrowserE2ETest.test_value_shapes_are_classified_without_raw_values tests.test_browser_e2e.BrowserE2ETest.test_message_region_coverage_is_not_the_shallow_list tests.test_browser_e2e.BrowserE2ETest.test_diagnose_uses_the_open_page_without_navigation -b
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
