# 与 Node.js e2e 的核心能力对照

本文对照上游 e2e 的 Node.js 实现，记录本 SDK 在同步 Web 自动化中的对应能力与差异。 [上游 e2e 项目](https://github.com/e2e-inc/e2e)

## 2026-11-28 第五轮 Web SDK 复核（同级仓库 `3e7d19de`）

对照范围是同步 Web SDK 的缓存、模型请求、页面观察和控件派发。下文的 Web Agent 语义动作对照表列出指针、键盘、原生表单、滚动、导航、标签页、截图、上传和项目工具的入口。本轮重点核查这些入口组合后的行为。

| 优先级 | 上游依据 | 本轮修复及回归场景 |
| --- | --- | --- |
| 高 | core/cache/anchors、agent/step-cache | 告警倒计时在两侧先按形状抵消，同类告警数量增加仍会被发现。终态证据限定到录制的变化，避免屏幕上无关控件的变化替输入回显作证。修正叶节点排序，让有限的锚点名额优先保留结果。 |
| 高 | agent/pixels、agent/replay | 原先可以录制只有画布变化的步骤，但重放还要求 DOM 变化。现在起始截图匹配、最终像素发生变化并与录制一致时可确认回放。录制和重放也使用相同的标签页与来源变化证据。 |
| 高 | agent/screen-update、agent/transcript-compaction、agent/observation-budget | 增量基于模型实际收到的紧凑节点计算，焦点消失等变化通过 unset 传达；保留弹窗和下载事件。旧轮次计入适配器输入预算，长会话重新发送完整上下文和当前屏幕，后续只追加新增历史。 |
| 中 | web/observation、core/internal/urls | 外站 iframe 不消耗同站帧名额。主文档没有 HTTP(S) 站点时，只接受继承来源的空文档；国家域名短二级后缀使用与上游一致的近似判定。 |
| 中 | web/in-page/read-semantics、web/refs | 控件名称读取跨分支 aria-owns 选项和多选值，去除重复引用；跳过 content-visibility 隐藏的直接文本。观察提供 aria-describedby 或 aria-description 正文，重定位优先用描述区分同名控件，避免列表重排后按旧序号点击。 |

**请求大小抽查。** 用内置 DeepSeek 适配器构造 100 个 checkbox、8 轮决策，每轮改变一个选中状态，在本地截取发送给 Chat Completions 的消息。独立请求的新 user 消息从 4,898 增至 5,590 字节；有状态首轮同为 4,898 字节，后七轮各 256 字节。若每轮把重发的所有消息正文都算入，两种模式累计分别为 101,141 和 109,156 字节。增量减少每轮新增内容，实际 token 费用仍取决于供应商对历史前缀的计费和命中率。此抽查未调用线上模型。

**缓存迁移。** 浏览器缓存 policy 为 15，自定义引擎为 5。旧文件保留，读写模式按新身份重新录制。严格回放需要先完成一次经验证的录制。

**剩余差异。** 已执行部分动作后的接管继续保留旧录制，未合并模型完成的后缀。长步骤按预算重新开会话，上游支持选择性删除旧屏幕和图片。完整可访问名称算法、跨分支 ARIA 所有关系在整棵树上的投影、跨域托管浏览器的重连与远端文件仍有差异。内置模型沿用 JSON 决策协议；供应商原生 tool calling、统一 trace 等集成差异见下文“仍存在的 Web 能力差距”。

## 2026-11-28 第四轮四链路复核（上游 `3e7d19de`）

本轮再次按「缓存 → Agent 语义识别 → token 优化 → 元素/控件映射」四条链路逐项对照同级 `../e2e`，只修中高优先级缺口。浏览器缓存 policy 由 13 升为 **14**（截图降采样改变了坐标录制的图像身份），旧 key 失效需重录。

### 缓存与重放

| 优先级 | 上游对照 | 原缺口 | 当前行为 |
| --- | --- | --- | --- |
| 高 | `agent/derived.ts`、`action-dispatcher.ts` 的 `recordDerivedGap` | 模型从屏幕读到的验证码、编号、日期也照原值录制，回放时把上一轮的数据再输入一次，可能被应用接受而误判通过 | `Agent._derived_reason` 按「调用方字面量 / 整节点文本 / 数据形 token / 截图 / 日期时间 / one-time-code」判断值是否属于本轮。前面已有可录制动作时写入 `{"op":"gap","reason":...}`，回放执行到 gap 即交接给模型重新取值；没有前缀可缓存时整步不录制。`steps.cache.derived` 记录原因。 |
| 中 | `cache/anchors.ts` 的 `deltaEvidenced` 输入目标排除 | 只填值的回放把输入框自身回显当作「应用已变化」的证据，可能自确认 | `delta_evidenced` 接收录制动作的输入目标；同一控件同时出现在 appeared/gone（值或状态变化）时其锚点不算证据，只有路由变化或其他锚点才算。`act_once` 只有一个输入动作，仍以该输入为目标结果。 |
| 高 | `cache/template.ts` 的 uri/form/slug/flat 拼写 | `Unique` 只替换原样字符串；`?q=E2E+abc`、`/companies/e2e-abc` 或换行折叠后的屏幕文本每次都会 miss，slug 详情页也无法回放 | `template()` 为同一值生成原样、`uri`、`form`、`slug`、`flat` 五种拼写（最长优先、只替换字面段），marker 带编码，回放时按编码重建本轮值；`templates_collide` 用同一拼写表检测字面量或两个 Unique 之间的冲突。 |
| 中 | `cache/route.ts` 的 `appLocation` / `appNavigation` | 起点、终点和每个动作前都按绝对 origin 比较，换部署（端口、预览域名）后所有录制失效 | 会话在第一次语义观察时记住 `app_origin`，录制写入 `app_origin`。回放按 `same_origin` 比较：录制于应用自有 origin 的位置在另一个部署的同一 origin 上仍匹配，第三方 origin 仍要求完全相同。应用内 `navigate` 录制为路径，回放按当前 `base_url`（缺省时用当前页面）解析。 |
| 中 | `cache/anchors.ts` 的 `alertShape` | 带时间的 toast 按原文成为锚点，回放永远 `end-mismatch` | `matches_end` 将告警行按 `_alert_shape`（时刻、日期、时长等占位）比较，普通行仍精确匹配；多重性和意外告警检测保留。 |
| 中 | `agent/replay.ts`、`step-cache.ts` 的零执行前缀 | 回放一个动作都没执行也直接 handoff，从不重录，陈旧条目永远交给模型 | 未执行任何录制动作的 `target-not-found` / `action-failed` 视为 miss：模型从本步起点重新完成，收尾写出完整的新 trace 替换陈旧条目，`cache.replay_prefix=0`。 |
| 中 | `agent/replay.ts` 的缺失目标退避 | `strict` 与 `act_once` 不做重定位重试，慢渲染即判 `REPLAY_STALE` | 两种模式同样按 0.1/0.3/0.6/1.0/3.0 秒退避，重试耗尽后才按各自契约 fail-closed。 |
| 低 | `step-cache.ts` 的 `baselineScreen` | 先离开终点路由、之后又返回时，基线仍取第一次到达的屏幕 | `end_baseline` 取最后一次到达终点路由后的第一屏；没有返回时仍取首次到达，测试覆盖。 |
| 高 | `spec 09` 的 `ACTION_MAY_HAVE_COMMITTED` | 超时统一变可重试的 `ACTION_FAILED`；回放交接不说明哪个动作可能已生效 | 新增 `ACTION_MAY_HAVE_COMMITTED`：Playwright 调用日志出现「performing click action」「action done」等派发后行才判定。回放以 `action-uncertain` 交接并在 `cache.uncertain_action` 点名动作，模型被明确要求先核对当前屏幕再重复；实时执行同样把错误交回模型。 |

### Agent 语义识别

| 优先级 | 上游对照 | 原缺口 | 当前行为 |
| --- | --- | --- | --- |
| 高 | `read-semantics.ts` 的 `isHidden` 对 `display:contents` | 容器会出现在观察里，但 `visible()` 要求自身 box，任何 ref 目标都直接 `NODE_STALE` | `visible()` 与扫描共用同一判定：`display:contents` 有可见子元素或文本即视为可见；无自身 box 的容器不再一触即错。 |
| 中 | 同一实现的零尺寸 box、`isDrawnByLabel` | 零宽高元素被当作可操作目标；隐藏复选框则可能被漏掉 | 扫描与 ref 校验要求元素自身 box 有面积；被自身可见 label 绘制的 checkbox/radio 例外保留，点击仍走 label 替身。 |
| 中 | `read-semantics.ts` 的 `presentation` 与 landmark 命名 | 每个 `img alt=""`、无名 `section`/`form` 都占一个节点，消耗节点预算并让模型看到无意义容器 | 装饰性图片返回空角色不再列出；无名 `section`/`form` 不作为 region/form 地标，带 test id 的容器仍以 generic 列出。 |
| 中 | `observation.ts` 的同站帧过滤 | 广告、埋点、第三方嵌入的 iframe 文本进入观察和缓存锚点，消耗节点预算 | `frame_on_site` 按可注册域过滤子文档；`about:blank`/`srcdoc` 视为应用内容，`data:` 与站外文档保留为边界节点，不标记截断。 |
| 中 | `read-semantics.ts` 的 `th` 轴推断、shadow 内 `dialog:modal` | 无 scope 的 `th` 一律列头；shadow root 内的模态可能漏掉 | `th` 按 scope、相邻数据格和单行表推断 rowheader/columnheader；存在 dialog 时递归 open/closed shadow root 收集模态。 |

### token 优化

| 优先级 | 上游对照 | 原缺口 | 当前行为 |
| --- | --- | --- | --- |
| 高 | `agent/pixels.ts` 的图片计量 | `_fit_request` 把截图 base64 计入 `fixed`，又按图片 patch 计一次；开视觉且设 `max_input_tokens` 时观察被压到 1024 字节 | 截图 base64 不再计入文本预算，只按 PNG 尺寸估算图片 token；同页节点仍能进入请求。降采样后的 PNG 尺寸也是计费依据。 |
| 高 | `agent/screen-update.ts` 与 provider 前缀缓存 | 顶层字段按字母序序列化，`history` 紧随 `context` 之后，每轮前缀立刻分叉；且每轮重发完整屏幕 | 请求按「稳定字段在前、当前屏幕在最后」的顺序序列化，便于供应商自动前缀缓存。纯文本 `act` 的内置适配器默认保持一段会话：首轮完整屏幕，后续只发送 `changes`（added/changed/removed/unchanged），节点别名稳定；`AgentConfig(stateful=False)` 可关闭；视觉、iframe、探索断言和修复请求自动回退独立请求，并重置为下一次整屏。 |
| 高 | `transcript-compaction.ts` | 预算不足时只裁观察，最多 24 KiB 的历史原样发送，仍可能 overflow | `_fit_request` 与内置适配器都先丢 `prior_steps`，再按 8/2/0 条裁 history、截短单条结果，最后才裁观察并标记 `truncated`。 |
| 中 | `default-agent.ts` 的 `ASSERT_RULES` | 探索断言仍发送 6.7 KB 的 act 提示，而它只允许滚动和只读工具 | `explore=True` 使用独立只读提示（动作只含 scroll、scroll_to、scroll_find、只读工具与 verdict），不再携带提交类动作规则。 |
| 中 | `redaction.ts` 的“先脱敏再截断” | 项目工具结果先 `bound_output` 再脱敏，截断边界可能留下 secret 前缀 | 工具结果先 `Ledger.redact` 再限行/限字节；工具抛出的异常同样先脱敏再作为可恢复失败交给模型。 |
| 中 | `model/sdk.ts` 的 `Retry-After` | 429/503 只按指数退避，忽略供应商要求的等待时间 | HTTP 错误带 `Retry-After` 时以它为下限，仍受本步剩余 deadline 约束。 |

### 元素与控件映射

| 优先级 | 上游对照 | 原缺口 | 当前行为 |
| --- | --- | --- | --- |
| 高 | `actions.ts`、`support.ts` 的派发后失败分类 | 提交类点击在派发后超时仍报 `ACTION_FAILED`，模型可再点一次造成重复提交 | `BrowserEngine.translate` 读取 Playwright `_log`，区分派发前可操作性失败与派发后不确定；回放与实时执行都带明确提示，回放还会点名动作。 |
| 中 | `tool-loop.ts` 的失败回读 | `POLICY_DENIED`（上传越界、错误的 secret 目标、无截图坐标）直接终止整步；项目工具异常原样冒出 `act` | `POLICY_DENIED` 进入可恢复集合（受 5 次/同动作 2 次上限约束）；项目工具异常转成 `ACTION_FAILED` 写入 `history`，由模型改道。 |
| 中 | `surface.ts` 的 `typeText` 焦点校验 | `type_at` 点击空白处后仍 `insert_text`，“输入成功”但字没进任何控件 | 点击后走 `_type_focused`：焦点必须落在可编辑字段，否则 `ACTION_FAILED`；输入改用逐字键入，触发应用事件。 |
| 中 | `action-dispatcher.ts` 的 `NODE_STALE` 重定位 | 动作派发时节点被重渲染，整个动作回到模型重新决策，多花一轮 | 实时动作遇到 `NODE_STALE` 时，用最后观察到的 role/name/test_id 构造 semantic 描述符并重定位一次；安全/OTP 字段不重试。重定位失败才交回模型。 |
| 中 | `primitives.ts`、`support.ts` 的 `scroll` 方向与页数 | 只能给像素 x/y，模型必须猜一屏多高 | `scroll` 新增 `direction`（up/down/left/right）和可选 `times`（1..20），按视口或容器 client 尺寸滚动；`x/y` 保持兼容。 |
| 中 | `upload-paths.ts` 的文件根与隐藏路径 | 上传默认拒绝一切，调用方必须先配置 `upload_roots`；默认配置下 Agent 无法上传 | 未显式配置时默认允许当前工作目录；隐藏文件/目录和根外路径仍被拒绝。`upload_roots=(...)` 可收紧，空 tuple 禁止项目文件（内置样本仍可用）。内置 `sample:photo.png` 等样本按需写到临时目录，无需项目文件。 |
| 高 | `pixels.ts` 的截图尺寸 | 视觉截图按原始视口发送，大屏每轮都为图片付更多 token | 新增纯 Python PNG 解码/箱式降采样：截图遮罩后按长边 768 缩放，`visual` 带 `scale`；坐标动作按图像像素接收、映射回 CSS 视口再执行。无法解码的 PNG 原样返回。 |
| 中 | `geometry.ts` 的坐标 clamp | 截图边缘差 1 像素即 `POLICY_DENIED` 终止整步 | 坐标先 clamp 到 `[0,width-1]×[0,height-1]` 再执行；`drag_at` 两端同样处理。 |
| 中 | `support.ts` 的拖拽可操作性 | 源控件被遮挡时拖拽落空却报成功；两端不能同屏也照样拖动 | 拖拽前先 hover 源控件以复用 Playwright 可操作性检查；滚动后重读两端 box，任一端中心不在视口内报 `ACTION_FAILED`。 |

### 仍未覆盖的差异

- 回放已执行部分动作后发生的 handoff 仍不重录后缀（只有零执行 miss 会重录完整流程），避免用另一条应用分支覆盖仍可回放的旧录制。
- 有状态会话尚未做 transcript compaction：长步骤的 messages 不裁剪，状态更新仍随请求重发完整 history；provider 上下文溢出时回退独立请求，用户可用 `AgentConfig(stateful=False)` 关闭。
- `POLICY_DENIED` 现在可回读，但上传越界本身仍需调用方补充参数；secret 目标错误可由模型改选控件。
- 传输 stall 上限仍由 120 秒常量控制（`Retry-After` 已参与退避）。
- 跨分支 `aria-owns` 的嵌入值、`nameFromContent` 的重复引用去重、`content-visibility` 对直接文本的影响仍是简化实现。
- `delta()` 的告警形状只用于终态比较，录制锚点仍保留原文；同名同角色容器的跨分支碰撞仍按 fail-closed 拒绝。
- `grid`/`treegrid` 的行列结构、`aria-describedby` 正文、完整 ARIA 角色/名称算法仍是简化子集。

## 2026-11-27 第三轮 Web 复核（上游 `3e7d19de`）

按 `packages/web/src` 的公开入口、底层浏览器模块及其对接的 Agent 请求路径复核。优先处理能改变模型决策、控件身份、敏感值可见性或资源归属的差异。

| 上游模块组 | Python 入口或本轮修复 | 剩余差异 |
| --- | --- | --- |
| observation、in-page、read-node、frame-document、closed-shadow、locators、refs、selector-engines | **高**。本轮修复隐藏的引用目标、引用循环和原生 label 的图片与隐藏装饰。密码用途的 autocomplete 字段在语义与截图中遮罩。**中**。自定义测试 ID 同时用于结构化观察、定位和缓存身份。 | 跨分支 aria-owns、完整 ARIA 语义与声明式 closed root 仍有差异。 |
| observation-capture、capture-scope、screenshot-caret、Agent screen-update | **高**。无状态 HTTP 请求每轮携带独立可读的紧凑屏幕，保留未变化的控件、帧表及当轮事件。节点短编号在步骤内稳定。 | 有状态模型的历史前缀复用、长边降采样仍待支持。 |
| actions、support、modal-dialogs、dialogs、attempt-session、downloads | **高**。关闭借用会话、构造借用会话失败、将弹窗转交给独立会话时释放对应监听器。 | 对话框回调栈、异步失败归因、远端下载读取和文本下载脱敏仍有差异。 |
| Agent 回放、动作预算与结果轮询 | **高**。修复模型接管后误删暂时缺少目标的录制。**中**。输入目标解析和实际输入共享动作预算。页面无 DOM 变化时，当轮下载或对话框也能结束观察等待。 | 暂时缺失目标会交回模型。终态确认仍依赖有界的语义锚点。 |
| provider、browser-connection、cdp-recovery、provider-video、video | **中**。新增宿主级 CDP 租约，连接失败及宿主关闭时释放。支持 CDP 握手请求头，连接异常省略可能带令牌的地址和网关原文。本地 Chromium 的沙箱默认值与上游平台策略对齐。原生 Context 可录制视频。 | 按 attempt 获取租约、以原页面身份重连、云录屏仍待支持。 |
| browser、surface、evaluation、media、permissions、init-scripts、route-options、route-pattern、protected-app | 原生 Page/Context 提供导航、断言、网络、cookie、权限、媒体、初始化脚本和 JavaScript 求值。 | 受保护预览的跨来源凭据策略、路由回调错误归因尚无统一 SDK 实现。 |
| app-log、trace-feed、operation-budget、sweep、engine、fixtures、install、cli | SDK 记录步骤、模型用量及动作预算，可使用原生 Playwright trace。 | 页面异常与网络失败的统一应用日志、供应商诊断流仍待支持。runner 调度、安装命令和多设备引擎由宿主负责。 |

## 2026-11-27 第二轮复核（上游 `3e7d19de`）

本轮按「缓存 → 语义识别 → token 优化 → 控件映射」四条链路逐项复核同级项目，修掉中高优先级缺口。浏览器缓存 policy 由 11 升为 **12**，旧 key 失效需重录。

### 缓存与重放

| 优先级 | 上游对照 | 原缺口 | 当前行为 |
| --- | --- | --- | --- |
| 高 | `cache/anchors.ts` 的 `deltaEvidenced` 与 `already-showing` | 终态相对**步骤起点**校验：上一轮残留的目标行会被当作本轮的创建结果 | 回放记录每一步后的到达屏，取「终点路由上的第一屏」为基线（`cache.end_baseline`），并要求 `delta_evidenced` 证明有变化。路由不变的步骤仍以起点为基线。 |
| 高 | `cache/route.ts` 的 minted 段与 `sameRoute` | 路由按原文比较，`/orders/8f3c…` 之类新建/详情页永不相等，create 类步骤每次 `end-mismatch` | `route()` 把 uuid、hex、纯数字、长 token、`PROJ-016`、日期与含空白段读成 `:id`，查询参数按键值排序且同样归一；起点、终点与录制条件统一走 `same_route`。hash 路由（`#/…`）并入路径。 |
| 高 | `recorder.ts` 的 `verbatim` / `truncated` | 被脱敏改写的录制值照样写盘并回放，回放会执行与录制不同的输入 | 录制前检查每个可回放字符串（值、URL、选项标签、路径）：被脱敏改写或超过 4096 字符时整条录制标记 `truncated`，只保留供查证。`valid_entry` 拒绝任何 `truncated` 条目。 |
| 高 | `replay.ts` 的终态等待 | 终点只等「第一帧不同」，上限 350 毫秒；慢接口每次判 `end-mismatch` 并驱逐条目 | 录制时等待屏幕稳定（连续两次观察一致），把实测耗时 + 10 秒写成 `end_wait_ms`；回放按它等待终点，`valid_entry` 校验其范围。 |
| 高 | `cache/template.ts` 的模板冲突检测 | unique 值出现在其他字面量里时，key 与录制会在两次调用间串台 | `templates_collide` 检出「unique 之间互为子串」与「未被标记的字面量包含 unique 值」，命中则本步不录制（`cacheable=False`）。 |
| 中 | `cache/decide.ts` 的 `opensWithNavigate` | 以 `navigate` 开头的录制被起点页面绑死 | 首个非 tool/resize 动作是 `navigate` 时跳过起点路由校验，起点 origin 仍然校验。 |
| 中 | `cache/anchors.ts` 的 `VOLATILE_TEXT` / `COUNT_TEXT` / `alertShape` | 只识别独立时钟行 | 扩展为 ISO 时刻、日期、月日/日月、时长、minted token；告警正文里的易变部分按占位符比较；计数仅在一侧已有稳定锚点或跨路由时丢弃，`0 records imported` 这类结果仍然精确匹配。 |
| 中 | `cache/anchors.ts` 的排序与保底 | 按启发式（是否含 `status` 子串、长度）取前 8 | 按「公告 → 叶子 → 容器」排序，名额不足时先用公告；整侧无稳定锚点时按 `stable → 含计数 → 全部` 三层保底。 |
| 中 | `cache/relocate.ts` 的 `describePosition` | 同名同角色的多个控件（每张卡片一个「Set up」）无法录制 | 唯一祖先失败时记录 `position:{index,of}`（上限 1000）；回放要求 twins 数量与录制一致才按序号取，数量不同即视为未匹配。 |
| 中 | `replay.ts` 的定位退避与失败分类 | 一次找不到即交还模型；任何动作异常都驱逐录制 | `LOCATOR_NOT_FOUND` 按 0.1/0.3/0.6/1.0 秒重试（受步骤截止时间约束）；`LOCATOR_AMBIGUOUS` 保持 fail-closed（驱逐并 `REPLAY_STALE`），其余失败交还模型并记录 `cache["reason"]`（`target-not-found` / `action-failed`），不驱逐未证明失效的条目。 |

### token 优化

| 优先级 | 上游对照 | 原缺口 | 当前行为 |
| --- | --- | --- | --- |
| 高 | `agent/screen-update.ts`、`transcript-compaction.ts` | 模型需要在多轮操作中识别同一控件 | 节点别名在步骤内稳定。独立 HTTP 请求的当前行为见第三轮复核。 |
| 高 | `agent/ledger.ts` | `prior_steps` 先按字符截断再脱敏，跨截断边界的 secret 会留下明文前缀 | 先脱敏再按 UTF-8 字节截断（标签 256、交接 700），超出预算时插入 `[N earlier step(s) omitted]`。 |
| 高 | `agent/tool-output.ts` | 项目工具结果无上限，经 `history` 每轮重发 | 工具结果限 400 行 / 16 KiB，超出时加截断说明；结构化结果裁成 `{"truncated": true, "text": …}`。 |
| 中 | `agent/ledger.ts` 的省略声明 | `history` 无上限 | 历史限 24 条 / 24 KiB，单条结果限 2 KiB，被丢弃的动作数量写成一条 `notice`。 |
| 高 | `run/invocation.ts` 的 `ask` 重试 | 一次 JSON 解析失败或 refusal 直接终结整步 | `_ask` 对 `MODEL_OUTPUT_INVALID` / `MODEL_REFUSED` 重试至多两次，并在 `repair` 中引用被拒响应（≤2000 字符）；超过 1 MiB 的输出同样重试。 |
| 中 | `agent/model/overflow.ts` | provider 的「prompt too long」被当作普通故障 | 新增 `CONTEXT_OVERFLOW` 分类；`_ask` 收到后裁掉 `prior_steps`、截半历史与节点表，再重试一次。 |
| 中 | `agent/observation.ts` 的几何 | 每个节点发送带小数的坐标 | 整数坐标发送为整数，只有真分数保留两位小数。 |

### 语义识别

| 优先级 | 上游对照 | 原缺口 | 当前行为 |
| --- | --- | --- | --- |
| 高 | `in-page/read-semantics.ts` 的 `ACCNAME_CONTENT_ROLES` | `generic`/`tabpanel`/`grid`/`tree`/`toolbar` 等容器的 `name` 是整棵子树的文本（可达 512 字符），任何文案改动都会让 ref 失效并撑大观察 | 只有 ARIA 允许从内容取名的角色（含 `listitem`、`status`、`alert`）使用 name-from-content，其余只取 `aria-labelledby`、`aria-label`、关联 label、`alt`、naming child 与 `title`。 |
| 高 | 同一实现的 `textOf` / 行文本 | `<p>同意 <a>条款</a>。</p>` 除「条款」外整句丢失 | 新增节点 `text` 字段：直接文本加内联后代的运行文本，成块后代贡献空格；`<p>`、`<blockquote>`、`<pre>`、`<code>`、`<em>`、`<strong>`、`<del>`、`<ins>`、`<sub>`、`<sup>`、`<mark>`、`<time>`、`<dl>`/`<dt>`/`<dd>` 补上角色映射；混排文本容器成为 `text` 节点。树行渲染为 `- paragraph "" text="…"`。 |
| 高 | `observation.ts` 的帧失败处理 | 读不到的 iframe 不置 `truncated`，缓存可能把不可见当「已消失」 | 帧读取失败统一置 `truncated=True`。 |
| 中 | `read-node.ts` 的 `inputPurpose`、`observation.ts` 的 `states.secure` | 密码 / OTP / 用户名只能靠 name 猜 | 节点新增 `purpose`（`password` / `one-time-code` / `username`）、`secure`；`fill_secret` 的 password 用途改按 `autocomplete` 契约校验（`current-password` / `new-password` / `type=password`）。 |
| 中 | `observation.ts` 的 attributes | `aria-required` / `aria-invalid` / `aria-describedby` 完全不可见 | 节点按白名单暴露 `type`、`autocomplete`、`required`、`placeholder` 与全部 `aria-*`；树行对 `required` / `invalid` 加短标记。 |

### 元素与控件映射

| 优先级 | 上游对照 | 原缺口 | 当前行为 |
| --- | --- | --- | --- |
| 高 | `agent/primitives.ts` 的失败回读 | 只有 `NODE_STALE` 会回到模型，其余动作失败直接终结整步 | `NODE_STALE` / `LOCATOR_NOT_FOUND` / `LOCATOR_AMBIGUOUS` / `ACTION_FAILED` 作为可恢复失败写入 `history`（`dispatched: false` + 错误码）交给模型改道；上限 5 次、同一动作 2 次，可恢复失败使本步不可录制，并重新完整观察。 |
| 高 | `web/actions.ts` 的失败分类 | 所有 Playwright 超时都变 `STEP_TIMEOUT`，模型失去改道机会；一次动作占用整步预算 | `BrowserEngine.translate` 区分 detached → `NODE_STALE`、可操作性失败 → `ACTION_FAILED`（带阻塞原因）、只有步骤截止时间真的耗尽才 `STEP_TIMEOUT`。`AgentConfig.action_timeout`（默认 20 秒）给单个浏览器操作独立预算，步骤仍有时间时把超时改判为 `ACTION_FAILED`。 |
| 高 | `web/dialogs.ts` | 原生 `alert` / `confirm` / `prompt` / `beforeunload` 无人处理，Playwright 默认静默 dismiss：确认框被取消、`prompt` 返回 null、告警文案模型永远看不到 | 新增动作 `{"op":"dialog","action":"accept"\|"dismiss","text":"…"}` 设定后续对话框策略（默认 dismiss）；会话自己的页面注册 `dialog` 监听，按策略作答并把 `{type,message,action}` 放进 `observation.dialogs`（每次观察后消费）。 |
| 高 | `web/actions.ts` 的 `pickStandIn` | 被自身 label 装饰覆盖的 checkbox / radio 用 `click` 会报「intercepts pointer events」 | 单次 `click` 复用 label 替身：命中 label 的非交互内容时按控件中心点点击 label，修饰键透传。 |
| 高 | `web/support.ts` 的滚动 | 只对自身 `overflow` 可滚动的元素 `scrollBy`；选中外层 wrapper 时静默不动 | 从元素中心的实际命中节点向上找可滚动祖先并滚动，找不到时报 `ACTION_FAILED`，不再静默成功。 |
| 高 | `web/actions.ts` 的 `NOT_ACTIONABLE` | 勾选后状态不变会轮询到截止时间并报 `STEP_TIMEOUT` | 3 秒内未达到目标状态报 `ACTION_FAILED`（提示改用同组另一个 radio），交给模型改道。 |
| 中 | `web/attempt-session.ts` 的孤儿页收养 | `window.open(..., 'noopener')` 或先开页再关闭自身窗口的新标签永远不被发现 | 会话自己拥有的 Context 里，未登记且未排除的新页面一律视为应用打开的标签；借用 Context 仍只接纳本会话页面打开的标签。 |

### 继续补齐（同日第二轮）

| 优先级 | 上游对照 | 原缺口 | 当前行为 |
| --- | --- | --- | --- |
| 高 | `agent/model/stall.ts`、`model/sdk.ts` 的传输重试 | `urlopen` 单发，连接卡住就吃掉整步预算，5xx/429 直接失败 | 单次请求另有 120 秒 stall 上限；5xx、408/409/425/429 与连接超时按 0.5/1/2 秒退避重试至多 3 次，始终受调用方 deadline 约束，首发改用完整预算。 |
| 高 | `agent/model/overflow.ts` | provider 的「上下文过长」被当作普通故障，整步报废 | 移植 24 条 overflow 正则与 4 条反向排除（限流不算），HTTP 413 直接判定；命中时抛 `CONTEXT_OVERFLOW`，由 `_ask` 收缩 `prior_steps`、历史与节点表后重试一次。 |
| 高 | `agent/observation-budget.ts` 的 harness 层预算 | 预算只在内置适配器里生效，自定义 executor 拿到未裁剪的观察 | 新增 `AgentConfig.max_input_tokens`：请求交给任何 executor 之前先按上限裁剪观察（保留 url/title/tabs 与最靠前的节点，插入截断标记），元数据本身超限才报 `STEP_BUDGET_EXHAUSTED`。 |
| 中 | `agent/usage.ts` | 只有 prompt/completion/cache-hit 累加 | 增加 `peak_prompt_tokens`、可选 `price_per_mtok` 估算 `estimated_cost_usd`，以及跨步骤汇总的 `session.total_usage`。 |
| 中 | `cache/context.ts` 的 `replayEligible`（attempt 语义） | 测试重试时会重放上次尝试刚失败的录制 | `CacheConfig(attempt=N>0)` 时该次尝试不读录制（`cache["reason"] == "retry"`），仍可重新录制。 |
| 中 | `cache/rekeyed.ts` 的每 worker 一次列举 | strict 每次 miss 都全目录读取解析 | `FileCacheStore.entries()` 结果按 store 实例缓存，写入或删除时失效。 |
| 高 | `read-semantics.ts` 的 accname 2B/2C | `aria-labelledby` 取 `textContent`：并入隐藏装饰文本、丢失目标内的 `<img alt>`；控件值不进入名称 | 引用目标按其可访问名称取值（`aria-label` → `alt` → 子树 accname 遍历），隐藏子树被排除；name-from-content 会取文本框值、选择框选中项、范围控件 `aria-valuenow/valuetext`；引用链最多两跳，避免互相引用死循环。 |
| 中 | `read-semantics.ts` 的图标字形处理 | 纯图标字体字符会成为控件名称 | 私有区字形（`\p{Co}`）从名称中剥离；剥完为空则回退 `title`。 |
| 中 | `read-semantics.ts` 的 `display:contents` | 无自身盒的容器连同其 `data-testid` 一起消失 | 有可见内容且带 test id / aria 名称的 `display:contents` 容器仍然列出。 |
| 中 | `web/downloads.ts` | 应用触发的下载完全不可见 | 会话页面注册 `download` 监听，`observation.downloads` 给出 `{suggested_filename, url}`（每次观察后消费）；文件落盘仍由宿主通过原生 Playwright 处理。 |
| 中 | `read-semantics.ts` 的 `attributes` | 输入约束不可见 | `attributes` 白名单补充 `min`/`max`/`step`。 |

### 仍未覆盖的差异

- 跨分支 `aria-owns` 仍未重挂树。隐藏引用、嵌入控件和图标字形的最新实现见本页复核记录。
- `attributes` 为白名单子集，已含 `min`/`max`/`step`，尚无可访问描述（`aria-describedby` 内容）计算；`display:contents` 容器、零尺寸盒、`grid`/`treegrid` 行列结构仍按简化规则处理。
- 语义匹配的第三层（test id 变了但 role+name 仍唯一时回退）与现有 fail-closed 契约冲突（`tests/test_p1_browser.py` 明确要求拒绝对不同 test id 的目标），未启用；`position` 只处理同角色同名 twins，跨 DOM 分支的同名容器仍可能碰撞。
- 模型原生 tool calling、JSON Schema 结构化输出、截图降采样（长边 768）、下载落盘与断言、AI trace、reasoning 摘要、按应用 origin 相对化路由（换部署仍会 miss）未移植。
- `scroll_find` 未命中仍抛 `LOCATOR_NOT_FOUND`，但该错误现在会作为可恢复失败回到模型。

## 2026-11-27 本地 Web 复核（上游 `3e7d19de`）

### 缓存、控件与请求预算复核

| 优先级 | 上游对照 | Python 行为与回归条件 |
| --- | --- | --- |
| 高 | `cache/anchors.ts`、`agent/step-cache.ts` | 表格行参与子控件的状态锚点匹配。两行都有 Enable 时，另一行变为已选无法确认回放。ref 录制保存输入类型和标题层级，同名文本框换成密码框时重定位失败。浏览器缓存 policy 已升至 11。 |
| 高 | `web/src/in-page/read-semantics.ts` 的 offered option | 隐藏选项及隐藏组内的选项不参与候选和选择。单选、多选与坐标选择共用解析器，原始索引保留。一个选择框顺序检查至多 512 项，另取最多 32 个选中项，证据不全时标记截断。 |
| 中 | 同一读取器的 box、landmark、level、disabled | 可见且有自身绘制的空区域可用 ref 指向，匿名 box 不录制。支持页级 header/footer、SVG 的 title、标题层级，以及跨 Shadow DOM 的 ARIA 禁用继承和局部覆盖。 |
| 中 | `core/src/cache/anchors.ts` 的 volatile text | 当有其他终态变化时，独立的时钟文本不占用八个锚点名额。时钟是唯一变化时仍精确匹配。带数字的业务结果及告警保留。日期、倒计时、多语言计数和动态告警仍有差距。 |
| 中 | `core/src/agent/observation-budget.ts`、`model/adapter.ts` | 两种内置 HTTP executor 均可设置 `max_input_tokens`。请求携带历史、说明和可选截图时一并估算，先裁剪原生节点并标记截断，固定内容已超限则在 HTTP 前报错。32 项连续索引样本的模型观察由 859 降至 517 UTF-8 字节，仅计观察 JSON。 |
| 中 | 截图绑定的坐标操作 | Chrome 会在重绘时交替绘制原生输入框焦点轮廓的边角。截图期间给表单控件使用低优先级实线焦点轮廓，坐标执行和回放继续检查完整截图摘要。 |

本轮以同级项目的 Web benchmark 控件清单、浏览器语义读取器、缓存锚点、模型适配器和既有动作对照为依据。长会话的增量观察及历史压缩、跨分支 ARIA 所属关系、完整动态缓存匹配和原生模型工具协议仍有差异。远程浏览器和 runner 的边界见下表。

| 优先级 | 复核场景 | 修复与边界 |
| --- | --- | --- |
| 高 | 已命名的 alert 在保存后由“就绪”变为“失败”，名称仍相同，缓存可能确认成功 | alert/status 同时提供名称与有界的可见正文，正文和名称相同时只发送一次。alert 正文变化参与意外告警检测；status 正文可作为录制终态。参照上游 `read-semantics.ts` 区分 name 和 text。浏览器缓存 policy 升为 10，旧录制需重新验证。 |
| 中 | 原生 select 不同 optgroup 内有同名选项，按索引编写的选择依赖页面顺序 | 观察包含组名。模型可按 group 和 label 选择，单选、多选及回放共享解析器；选错组无法满足该选项的选中终态。 |
| 中 | 同一嵌套 iframe 内的每个节点重复发送完整选择器链 | 内置模型请求用 frame_index 引用当前 frames 表。帧表被裁剪时保留原路径。公开观察及缓存定位继续使用完整路径。 |
| 高 | 观察截断或 iframe 无法读取时，控件移出观察范围也可能满足“已消失”终态 | 对照上游 `packages/core/src/agent/step-cache.ts` 的 traceEligible 检查。命中缓存但起点证据不完整时，普通模式转实时，严格模式在派发前报错。动作依据或终态不完整时跳过录制，回放也要求完整终态。 |
| 高 | 同名链接分属不同目的地，链接重渲染后仍保留旧名称 | 对原生 HTTP(S) 链接提供目标来源和路径，支持相对地址。由 ref 录制的链接带目的地参与重定位，当前引用重新检查目的地。沿用上游 Web 对链接的投影边界，省略凭据、查询串和片段；仅这些部分有差异时仍需其他线索。旧缓存沿用已录制字段。 |
| 中 | 图标作为 aria-labelledby 引用、可聚焦按钮带 presentation、编辑器靠占位符命名 | 支持引用元素的 aria-label 和 alt、常用原生控件的呈现角色冲突处理、aria-placeholder，以及编辑器宿主或空内容块的 data-placeholder。参考 `packages/web/src/in-page/read-semantics.ts`。 |
| 中 | 每个模型节点重复发送坐标键，原生 select 的每个选项重复发送两个默认状态 | 内置适配器将坐标按 x、y、width、height 编成数组，选项只发送为真的状态。32 项选择框测试样本的观察 JSON 由 1955 字节降至 883 字节，含字段含义的提示在请求中另计。公开观察和自定义 executor 保留原格式。 |
| 高 | 两个 ARIA 列表、单选组或标签栏里各有同名选项，页面更新了错误的一组 | 回放终态将子控件与语义树中最近的对应容器关联，并保留 iframe 路径。覆盖 listbox、combobox、radiogroup、tablist、tree、menu 和 menubar 的相应子角色。仍依赖可见树的层级和容器语义，无法区分同名同角色的两个相同容器，也未解析跨 DOM 分支的 aria-owns。对照上游 `packages/core/src/agent/step-cache.ts` 的节点终态校验。 |
| 中 | 自定义滑块等范围控件通过键盘改变状态，Agent 缺少读回依据 | 滑块、数值微调框、滚动条、进度条及仪表在观察中暴露 aria-valuenow 与 aria-valuetext，两者同时存在时分别保留。无 value 的原生 progress 可与进度为零区分。参考上游 `packages/web/src/in-page/read-semantics.ts` 的范围控件值语义。 |
| 中 | 只读判断反复发送点击、上传、坐标等动作说明 | 内置 Chat Completions 适配器按任务发送提示。普通 assert、wait_for 和 extract 使用专用判断说明，探索断言保留工具协议。对照上游 `packages/core/src/agent/prompts.ts` 的判断请求。 |

本轮继续对照同一上游提交，补充以下 Web 差异。

| 优先级 | 场景 | 修复及影响 |
| --- | --- | --- |
| 高 | 折叠面板内的按钮、iframe 或 CSS 暂停渲染的区域仍被当作可操作内容 | 对照上游 `read-semantics.ts`。关闭的 details 仅遍历首个直接 summary；content-visibility:hidden 保留容器，跳过子树。插槽控件按渲染层级归属。重新使用 ref 或在 iframe 内重定位语义目标时同步检查可见性。 |
| 高 | 长 select 的采样选项发生替换，被挤出样本的选项可能被当作已删除 | 起点、动作观察或终态包含 options_truncated 时放弃录制；严格回放在起点证据不完整时停止。这也会使同页其他操作转为实时执行。浏览器缓存 policy 升为 9。 |
| 中 | landmarks 或原生结构缺少角色，模型须从相邻文本推断关系 | 命名 section 映射为 region，figure 读取直接 figcaption，menu 和 hr 分别映射为 list 和 separator。 |
| 中 | 超过节点或字节预算的页面重复发送语义树与结构化节点 | 内置适配器在有节点时省略由节点渲染的树文本，保留截断标记与已有 iframe 错误。公开观察格式不变。 |

## 2026-11-26 本地 Web 复核（上游 `3e7d19de`）

| 优先级 | Web 场景与原缺口 | 当前行为 |
| --- | --- | --- |
| 高 | 两个下拉框包含相同选项，错误控件上的选中变化也能满足回放终态 | 原生 select 的选项锚点包含其控件语义行及 iframe 路径。能区分不同名称的下拉框，同名同角色仍可能碰撞。禁用、只读、半选变化也进入终态候选锚点。 |
| 高 | 带样式的复选框和单选框被自己关联的标签内容盖住，`check` 直接点击 input 会超时 | 对原生 check/uncheck，在命中其关联 label 的非交互内容时通过 label 点击一次，再读取原控件状态。仍使用 Playwright 可操作性检查。对照 `packages/web/src/actions.ts`。 |
| 中 | 富文本编辑器已有内容无法作为控件值读取，部分网格单元格和原生选项状态与浏览器不符 | 编辑宿主提供有界文本值。grid/treegrid 中的 td 映射为 gridcell，rowgroup 表头映射为 rowheader。原生 option 优先取实际 selected 状态。对照 `packages/web/src/in-page/read-semantics.ts`。 |

## 2026-11-21 上游复核（`7d7d0e16` → `3e7d19de`）

`e2e-web` 以 Python Playwright 实现同步 Web SDK，直接依赖见 `pyproject.toml`。上游更新涉及 SDK 行为对齐，安装依赖仍由 Python 项目声明。

| 上游变更 | Python 对应行为 |
| --- | --- |
| `160cbfe7` 一次性验证码在运行时生成 | 对定向验证码输入跳过整步录制，重放已有条目时在向验证码字段写入前停止。Python 缓存按整步保存，运行时补位仍待支持。 |
| `e939a656` 减少重复表单提交 | 内置模型提示要求检查提交证据。仍在提交时等待，结果未知时阻塞，确认失败并修正原因后才可重试。自定义 executor 自行实现决策。 |
| `0a5ee163` 和 `dbe3dc54` 拆分 Node 核心包并公开会话接口 | 上游 agent 与缓存实现已迁至 `packages/core`。Python 继续由 `Web` 管理浏览器并通过 `WebSession` 提供单测试会话。 |
| `e88153b2` 元素 `blur`；`d4fb5ccd` 按角色断言 | 确定性操作可使用 `session.page` 的原生 Playwright 定位器和 `session.expect` 转发的断言。 |
| `25bde7a7` 模态背景语义裁剪；`705c0492` 可访问名称排除特定后代 | 语义观察裁剪模态背景和背景 iframe。按内容命名的角色排除隐藏内容和部分独立语义后代。 |
| `3e7d19de` 等动态计数与时间锚点；`0920ef5f` Agent 跨标签页 | 同步了标签页发现、切换和关闭。Python 的语义行 delta、动态参数及跨标签重放仍有差异。 |
| `a06b931b` Chromium 沙箱默认策略；`046bd08f` 认证及自定义请求头按来源限制 | 本地 Chromium 已采用同一平台沙箱默认值。`context_options` 可配置原生凭据，受保护预览的一组来源和请求头策略仍待支持。 |

Runner 的 `test.fixme`、报告协议、MCP 调试和移动端由既有范围约定处理。

## Web Agent 语义动作对照（上游 `3e7d19de`）

基准是上游 `packages/core/src/agent/primitives.ts` 实际给模型的工具，加上 `packages/web/src/engine.ts` 声明的浏览器能力。确定性 Locator 方法、移动端键盘工具和原生 Playwright 接口不计入 Agent 动作覆盖率。

| 能力 | Node.js Web Agent | Python Web Agent |
| --- | --- | --- |
| 指针和表单 | `tap`、`double_tap`、`right_click`、`long_press`、`hover`、`drag`、`select`、`check` | 对应 `click`、`double_click`、`right_click`、`long_press`、`hover`、`drag`、`select`、`check` / `uncheck`。两边支持修饰键点击。Python 另外支持原生多选批量选择。 |
| 键盘和 Secret | 定向 `type` 替换输入值，省略目标时向焦点输入。`press` 支持重复，观察包含文本选区。`type_secret` 在执行边界注入值。 | 定向替换用 `fill`，定向 `type` 逐字输入。无目标 `type` / `press` 对应焦点键盘，`fill_secret` 对应 Secret 注入。`press.times` 最多 20 次，后续按键跟随焦点。观察暴露非敏感文本选区。 |
| 滚动与定位 | 按方向和页数滚动，按节点或语义文本寻找布局内或虚拟列表中的目标。语义文本搜索上限 800 屏，可随加载追加继续查找。 | `scroll` 接受像素位移或方向与页数，`scroll_to` 用节点或定位器，`scroll_find` 用可重定位描述符，支持容器及四向搜索，最多 800 次。到达边缘时短暂等待列表追加。 |
| 导航和视口 | `navigate`、`back`、`resize_viewport` | 对应操作已可用，另有 `forward` / `reload`。调整视口在下次观察中反馈新尺寸，含该操作的步骤实时执行。 |
| 多标签页 | 页面新开标签时给模型反馈，`switch_tab`、`close_tab` 支持在同一步继续动作并记录重放。 | 模型从观察读取标签编号，可在同一次 `act` 切换和关闭。宿主仍可用 `open_popup` 打开独立会话。跨标签步骤可按标签编号、完整 URL 和打开关系校验并回放。 |
| 截图和坐标 | 可按需 `screenshot`，支持 `tap_at`、`hover_at`、`drag_at`、`type_at`、`press_at`、`select_at`，可重放符合条件的坐标动作。 | `vision=True` 始终附图，`vision="on_demand"` 支持模型请求截图。支持 `click_at`、`hover_at`、`drag_at`、`type_at`、`press_at`、`select_at`。坐标绑定最新截图并校验像素，回放时核对视口尺寸和截图摘要。 |
| 上传和扩展工具 | 上传支持项目文件与内置 `sample:`，拒绝项目外和隐藏路径。内置工具、项目工具和动作结果共用工具循环。 | 文件输入和文件选择器已覆盖，默认使用项目根，可用 `upload_roots` 限定。`Tool` 接入自定义操作，参数受 Schema 校验。支持内置 `sample:`，项目文件受根目录和隐藏路径检查。 |
| 执行后的观察和容错 | 工具结果返回等待后的语义变化，支持 `observe` 与失败反馈，并检测动作循环。 | 动作后短暂等候语义变化。模型可用只读 `observe` 检查异步提交结果，也可指定有界 `wait`。可恢复的动作错误进入历史，供模型重新决策。 |
| 语义断言 | 默认只读当前快照。`explore: true` 允许用只读工具跨视口取证。 | `assert_(explore=True)` 可用滚动和只读项目工具跨视口取证，使用独立预算。`wait_for`、`extract` 保留实时判断。 |

Python 语义观察仍是有界子集。完整可访问名称算法、动态缓存锚点、跨标签及坐标重放、失败接管仍有差异。标签页以会话页面的弹窗继承关系发现，借用场景保护调用方已有页面。

## 与 Python 原型的 Web 行为

同级 `../e2e/prototypes/python-mini-e2e` 的重放以包含完整 URL 的快照为起点。当前 SDK 校验主页面起止来源和路由、每个录制动作执行前的来源，以及 Playwright 导航的目的地来源。终态按语义行的数量变化校验，包含控件的 checked、selected、expanded 和 pressed 状态，同时检查已观察 iframe 的新增告警。

原型读取 Playwright ARIA 快照。当前 SDK 的有界语义树保留节点引用，并覆盖原生搜索框、提交按钮、下拉框及列表框的角色和名称，以及选择框的选项标签与选中状态。

## 2026-11-06 复核结论

当前目录已有 Python 实现。核心思想已经落地：浏览器宿主与单测试隔离会话分离、模型只做决策、宿主管理执行与预算、后续验证才确认缓存、判断始终实时执行、Secret 在执行边界解析。pytest/unittest 继续负责测试组织。

本次补齐：

| 源码依据（相对 `../e2e`） | 原缺口 | Python 落地 |
| --- | --- | --- |
| `packages/e2e/src/internal/urls.ts` | navigate 允许本地 file URL | 只允许有效 http/https 和精确 about:blank；执行前校验 |
| `packages/e2e/src/agent/secrets.ts` | 已声明 secret 可填入任意目标 | 检查 editable/enabled，password 用途只能填密码框；重放重新授权 |
| `packages/e2e/src/agent/ledger.ts` | act 之间没有步骤交接 | 有界、脱敏 prior_steps，包含重放标记及摘要；judgment 不读取 |
| `packages/e2e/src/agent/tool-loop.ts`、`loop-guards.ts` | 非法动作/工具参数立即终止 | 未执行拒绝反馈给模型，连续三次停止，共享模型预算 |
| `packages/web/src/observation.ts`、`locators.ts` | 只观察/操作直接 iframe | 广度优先观察，20 个总量、8 层深度限制；选择器链定位 |
| `packages/web/src/init-scripts.ts` | 宿主创建页面前没有脚本注入入口 | session.init_scripts 对应 Context.add_init_script，覆盖弹窗与 iframe |

没有改动 Node 仓库。缓存升级要求见下文。

## P1 实现与边界

三项 P1 已有 Python 实现及真实 Chrome 回归：

| 能力 | 实现 | 验证与边界 |
| --- | --- | --- |
| 结构化节点、引用、新鲜度、重定位与预算 | `semantic.js`、`semantics.py`、`browser.py`；`ObservationConfig` | 节点保留已列出的父节点、图标的 test id、展开/按下/焦点/半选/只读状态及滚动轴；ref 必须匹配最新 observation.id。缓存可用唯一语义祖先定位同名控件，重排后重新扫描，歧义或截断时停止。默认全局 200 节点、10000 元素、64 KiB、20 iframe、8 层 |
| 视觉动作和滚动定位 | 坐标工具、`scroll_to`、`scroll_find` | 坐标绑定当前截图，执行前检查 URL、视口范围及像素一致性。持续动画可能导致拒绝。虚拟列表支持四向搜索，最多 800 次，可指定要滚动和搜索的容器，目标位于容器所在文档。坐标回放校验截图摘要。滚动搜索可录制并在原容器内回放 |
| 精细元素操作 | `actions.py`、`browser.py` | 元素右键、长按、修饰键点击，以及通过可见控件打开文件选择器上传。上传路径仍受 upload_roots 限制 |
| 表单和键盘 | `actions.py`、`browser.py`、`semantic.js` | 参考 Node 的单项选择描述符、焦点键盘操作、开关状态轮询。Python 的 `select` 另外支持原生多选批量设置和清空；Node 的 `Locator.selectOption` 只接受一项。自定义组件可组合键盘操作，并观察 `active_descendant`。无目标键盘动作依赖当前焦点，不生成录制 |
| closed shadow DOM | Context 初始化时跟踪 attachShadow 返回的 closed root；结构化采集遍历 open/closed 嵌套根，ref/semantic 用 ElementHandle 操作 | 支持被跟踪根中的点击、填充及 Secret 字段校验，保持 `.shadowRoot === null`。不强制把 closed 改成 open；不修改调用方的原生 Playwright selector 行为。声明式 closed root、安装跟踪前已创建的根无法回溯；closed 根内部 iframe 的自动遍历仍有限 |

结构化读取是有界的实用语义子集，尚未复刻 Node 的完整浏览器可访问性算法和 CDP/axe 对照。缓存语义匹配使用 role/name/test_id/frame 及 iframe 身份检查，普通页面重排可恢复；语义改名、重复目标、iframe 顺序变化或扫描截断应重新录制，不能承诺任意 DOM 改动都恢复。借用 Page 可在首次观察时安装当前文档跟踪，但已有 closed root 无法回溯；若要捕获后续导航创建的根，请在导航前使用 `page.add_init_script(SEMANTIC_INIT)`（见 README）。

当前 Playwright 缓存身份 policy 为 15，旧 key 需重新录制。strict 会检测可枚举存储中同一步骤的旧 key。

## 仍存在的 Web 能力差距

这些是剩余能力或集成差异，不属于 runner 开发要求：

| 优先级 | 能力 | Node 源码依据 | Python 当前边界与后续方向 |
| --- | --- | --- | --- |
| P2 | 云浏览器生命周期、断线恢复与远程文件 | `packages/web/src/provider.ts`、`cdp-recovery.ts`、`downloads.ts` | 支持静态 cdp_url、借用 Page 和宿主级 provider；重连身份校验、远端下载读取待补齐 |
| P2 | 更完整的重放与失败接管 | `packages/core/src/agent/replay.ts`、`step-cache.ts` | 已有唯一语义锚点重定位；支持动态 test id 形状唯一匹配、直接语义目标消失及终态不符时接管；其余失败分类及复杂动态参数推断仍有差异 |
| P2 | 模型原生 tool calling、usage 与可追踪性 | `packages/core/src/agent/model/`、`usage.ts` | 目前 JSON 决策协议，可接函数 executor；内置 HTTP 适配器按步骤汇总供应商返回的输入、输出与缓存命中 token；可配置单价估算费用；跨 executor 统一计量及完整 AI trace 待补齐 |
| P2 | 有 Secret 时的图像及下载处理 | `packages/core/src/agent/pixels.ts`、`packages/web/src/downloads.ts` | 原生密码和一次性验证码字段支持截图遮罩，已登记 Secret 的会话禁用视觉输入。下载由宿主处理，尚无文本下载重写 |
| P2 | 诊断及事件路由 | `app-log.ts`、`trace-feed.ts`、`dialogs.ts`、`surface.ts` | 可在原生 Page 注册回调。统一的应用异常与失败请求日志、回调失败在步骤边界报告、对话框处理器栈待补齐。 |
| P2 | 受保护环境的凭据范围 | `protected-app.ts` | 原生 Playwright 可为 HTTP 凭据指定 origin，SDK 仍缺统一的应用来源白名单与请求头配置。 |
| P2 | 复杂页面的语义树 | `in-page/read-semantics.ts`、`cdp-selectors.ts` | 跨 DOM 分支的 ARIA 所有关系、特殊 Shadow DOM 及完整角色与名称计算仍有差异。 |
| P3 | 原生 async Python API | Python 接入需求 | 同步 SDK 支持 pytest/unittest；异步宿主需要独立 async 适配，不能直接传 async Page |

cookies、路由、storage_state、下载/弹窗/对话框、键盘鼠标、浏览器上下文参数、trace/video 已可使用原生 Playwright，因此不记作“完全缺失”；它们尚未统一进入 Agent 协议、预算和证据链。测试收集、调度、重试、reporter 继续由第三方框架提供。

## 模块对应

下表中以 `src/agent` 和 `src/cache` 开头的路径相对于上游 `packages/core`。

| Node 源码 | Python 实现 | 当前能力 |
| --- | --- | --- |
| `packages/e2e/src/run/open-session.ts` | `sdk.py` / `session.py` | 浏览器宿主与会话分离；owned Context / borrowed Page；幂等释放 |
| `src/agent/act.ts` / `executor.ts` | `session.Agent` / `types.Executor` | harness 管理观察、动作、预算和结果；executor 可替换 |
| `src/agent/default-agent.ts` | `models.py` | 通用 Web action/decision JSON 循环；兼容 HTTP 适配和函数适配 |
| `src/agent` judgment methods | `Agent.assert_ / wait_for / extract` | 实时判断、轮询、JSON Schema / Pydantic 校验、一次格式修复 |
| `src/agent/secrets.ts` / ledger | `_utils.Ledger` | 参数 secret 句柄、执行边界填充、观察/历史/证据脱敏 |
| `src/cache/identity.ts` | `Agent.act` key 构造 | 指令/参数签名、同签名序号、应用/agent/context 身份、Unique 模板 |
| `src/agent/step-cache.ts` | `Agent.act` replay 分支 | 重放、起始 route、终态 delta、严格失效、终态偏离交还模型 |
| `src/cache/context.ts` | `WebSession.close` | 后续验证确认；保存/保留/删除；只读模式；中断不修改 |
| `src/cache/store.ts` | `FileCacheStore` | 文件原子写、上限、校验、自定义 store、strict 重键扫描 |
| `packages/web/src/browser.ts` | `BrowserEngine` + `session.page` | Agent 动作 + 完整原生 Playwright 确定性 API |
| runner progress/report | `StepRecord` / `on_step` / `steps.json` | 可由宿主接入报告，不创建测试报告协议或执行器 |

## executor 请求与响应

所有请求包含：

```python
{
    "kind": "act",  # 或 assert / extract
    "instruction": "调用方指令",
    "params": {},
    "context": "agent context",
    "observation": {"url": "...", "title": "...", "tree": "bounded semantic text", "nodes": [], "id": "observation-id", "frames": []},
    # act 时额外包含 history、prior_steps 与 tools；extract 时额外包含 schema。
}
```

Executor 实现 `complete(request, *, timeout: float) -> dict`。应使用收到的剩余 timeout 限制自己的网络请求。

```python
# 动作：从当前观察选择目标，不使用站点预设 selector。
{"action": {"op": "fill", "target": {"by": "role", "value": "textbox", "name": "Observed label"}, "value": "task input"}}
# act 完成：不能代替后续验证。
{"done": True, "summary": "observed outcome"}
# act 阻塞：由 harness 映射为 AUTOMATION_UNSUPPORTED。
{"blocked": "reason"}
# assert / wait_for 的判断响应：只判断，不执行动作。
{"passed": True, "reason": "current evidence"}
# extract：必须满足调用方 schema。
{"value": {"field": "data"}}
```

`fill_secret` 使用 `secret: "declared-name"`；`tool` 使用 `name` 和 JSON `arguments`。详见 `actions.py` 的封闭验证规则。非法动作、未知工具、未声明 secret 和越界上传不会执行。

模型/判断接口有意不引入 AI SDK 特定类型。内置 HTTP 适配器在 StepRecord.model_usage 中记录观察字节数及供应商返回的 token 计数。可用 `price_per_mtok` 估算费用，完整 AI trace 仍待补齐。宿主可以在自定义 executor 中观测模型指标，但不得把 secret 原值写入日志。

## 有意保留的差异

- Python API 使用秒和 snake_case；Node 使用毫秒和 camelCase。
- Python 使用有界结构化语义子集、文档内节点 ID、最新观察引用和唯一语义锚点重定位；仍不等价于 Node 的 CDP/axe 对照及模糊重定位层。
- 观察包括主页面和至多 20 个 iframe，最深 8 层；特殊控件仍可能需要宿主通过原生 Page 操作。
- 缓存终态锚点为有上限的语义文本行 multiset delta，包含已观察的 iframe；不是 Node 的结构化 semantic node anchors。无效、无差异或包含项目工具的动作流程不新建录制。Playwright 上传支持录制本步 params 提供的文件路径，回放按当前 upload_roots 重新校验。
- 普通多动作模式下，直接语义目标在派发前消失或来源一致时的语义终态失配可交还模型，提供已执行前缀且该次不重录。重放途中或终点发生来源失配时直接停止。未移植 Node 全部 relocation/handoff 分类。
- `Unique` 是显式字符串参数的子串模板，没有 Node 的完整数据来源推断与复杂模板冲突分析。使用足够独特的业务输入值。
- 原生 Playwright 调用不自动纳入 SDK 步骤/预算；通过 `expect` 或 `verify` 显式声明确定性验证边界。
- 支持截图绑定的坐标点击、悬停、拖动、输入、按键及选择。按容器搜索基于可重定位描述符，支持四个方向，最多滚动 800 次，受动作截止时间约束。
- browser 原生 API 提供 route、cookies、state、downloads、popup、dialog、video 等；不另做一套 Node 同名 wrapper 或统一这些原生调用的错误。
- 同步自定义函数不能被安全强制抢占。deadline/cancellation 在操作前后检查；内置 HTTP 和 Playwright 接收剩余 timeout。
- session artifacts 是本 SDK 的步骤文件，不是 Node `report-1`；缓存是 `e2e-web-python-trace-1`，不是 Node `trace-1`。
- `CacheStore` 自定义服务必须自负身份隔离与信任边界；本 SDK 校验 wire shape，不给缓存文件做签名。
- Secret 模型不能阻止可信用户代码读取值；启用原生 trace/video/storage_state 前宿主需自行处理敏感数据。

## 不属于 SDK 范围

测试发现、测试注册、hook runner、worker 调度、测试重试、CI workflow、应用进程管理、测试结果 reporter、MCP server、OAuth 登录 CLI、npm scaffold、iOS/Android 与云设备 lease 均不移植。上层框架负责测试生命周期、并发隔离与失败后的环境重置。

## 后续可扩展点

原生 async Playwright 接口、供应商原生工具调用适配、更完整的语义算法及 relocation、完整 usage/AI trace。当前公开的 Engine / Executor / CacheStore 接口让这些扩展不依赖某个测试框架。
