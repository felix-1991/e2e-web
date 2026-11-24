# 与 Node.js e2e 的核心能力对照

本文对照上游 e2e 的 Node.js 实现，记录本 SDK 在同步 Web 自动化中的对应能力与差异。 [上游 e2e 项目](https://github.com/e2e-inc/e2e)

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
| `a06b931b` Chromium 沙箱默认策略；`046bd08f` 认证及自定义请求头按来源限制 | Python 通过 Playwright 的 `launch_options` 和 `context_options` 配置浏览器。Node 新增的默认策略和按来源绑定仍待评估。 |

Runner 的 `test.fixme`、报告协议、MCP 调试和移动端由既有范围约定处理。

## Web Agent 语义动作对照（上游 `3e7d19de`）

基准是上游 `packages/core/src/agent/primitives.ts` 实际给模型的工具，加上 `packages/web/src/engine.ts` 声明的浏览器能力。确定性 Locator 方法、移动端键盘工具和原生 Playwright 接口不计入 Agent 动作覆盖率。

| 能力 | Node.js Web Agent | Python Web Agent |
| --- | --- | --- |
| 指针和表单 | `tap`、`double_tap`、`right_click`、`long_press`、`hover`、`drag`、`select`、`check` | 对应 `click`、`double_click`、`right_click`、`long_press`、`hover`、`drag`、`select`、`check` / `uncheck`。两边支持修饰键点击。Python 另外支持原生多选批量选择。 |
| 键盘和 Secret | 定向 `type` 替换输入值，省略目标时向焦点输入。`press` 支持重复，观察包含文本选区。`type_secret` 在执行边界注入值。 | 定向替换用 `fill`，定向 `type` 逐字输入。无目标 `type` / `press` 对应焦点键盘，`fill_secret` 对应 Secret 注入。`press.times` 最多 20 次，后续按键跟随焦点。观察暴露非敏感文本选区。 |
| 滚动与定位 | 按方向和页数滚动，按节点或语义文本寻找布局内或虚拟列表中的目标。语义文本搜索上限 800 屏，可随加载追加继续查找。 | `scroll` 用像素位移，`scroll_to` 用节点或定位器，`scroll_find` 用可重定位描述符，支持容器及四向搜索，最多 800 次。到达边缘时短暂等待列表追加。 |
| 导航和视口 | `navigate`、`back`、`resize_viewport` | 对应操作已可用，另有 `forward` / `reload`。调整视口在下次观察中反馈新尺寸，含该操作的步骤实时执行。 |
| 多标签页 | 页面新开标签时给模型反馈，`switch_tab`、`close_tab` 支持在同一步继续动作并记录重放。 | 模型从观察读取标签编号，可在同一次 `act` 切换和关闭。宿主仍可用 `open_popup` 打开独立会话。跨标签动作和非初始标签步骤实时执行。 |
| 截图和坐标 | 可按需 `screenshot`，支持 `tap_at`、`hover_at`、`drag_at`、`type_at`、`press_at`、`select_at`，可重放符合条件的坐标动作。 | `vision=True` 始终附图，`vision="on_demand"` 支持模型请求截图。支持 `click_at`、`hover_at`、`drag_at`、`type_at`、`press_at`、`select_at`。坐标绑定最新截图并校验像素，包含坐标动作的步骤实时执行。 |
| 上传和扩展工具 | 上传支持项目文件与内置 `sample:`，拒绝项目外和隐藏路径。内置工具、项目工具和动作结果共用工具循环。 | 文件输入和文件选择器已覆盖，上传路径由显式 `upload_roots` 限定。`Tool` 接入自定义操作，参数受 Schema 校验。项目根、隐藏路径及 `sample:` 的规则有差异。 |
| 执行后的观察和容错 | 工具结果返回等待后的语义变化，支持 `observe` 与失败反馈，并检测动作循环。 | 多数动作后短暂等候语义变化，且保留有界 `wait`。格式拒绝和过期引用可反馈给模型，大多数动作执行错误会结束该步。 |
| 语义断言 | 默认只读当前快照。`explore: true` 允许用只读工具跨视口取证。 | `assert_(explore=True)` 可用滚动和只读项目工具跨视口取证，使用独立预算。`wait_for`、`extract` 保留实时判断。 |

Python 语义观察仍是有界子集。完整可访问名称算法、动态缓存锚点、跨标签及坐标重放、失败接管仍有差异。标签页以会话页面的弹窗继承关系发现，借用场景保护调用方已有页面。

## 与 Python 原型的 Web 行为

同级 `../e2e/prototypes/python-mini-e2e` 的重放以包含完整 URL 的快照为起点。当前 SDK 校验主页面起止来源和路由、每个录制动作执行前的来源，以及 Playwright 导航的目的地来源。终态按语义行的数量变化校验，同时检查已观察 iframe 的新增告警。

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
| 视觉动作和滚动定位 | 坐标工具、`scroll_to`、`scroll_find` | 坐标绑定当前截图，执行前检查 URL、视口范围及像素一致性。持续动画可能导致拒绝。虚拟列表支持四向搜索，最多 800 次，可指定要滚动和搜索的容器，目标位于容器所在文档。坐标和滚动搜索流程实时执行 |
| 精细元素操作 | `actions.py`、`browser.py` | 元素右键、长按、修饰键点击，以及通过可见控件打开文件选择器上传。上传路径仍受 upload_roots 限制 |
| 表单和键盘 | `actions.py`、`browser.py`、`semantic.js` | 参考 Node 的单项选择描述符、焦点键盘操作、开关状态轮询。Python 的 `select` 另外支持原生多选批量设置和清空；Node 的 `Locator.selectOption` 只接受一项。自定义组件可组合键盘操作，并观察 `active_descendant`。无目标键盘动作依赖当前焦点，不生成录制 |
| closed shadow DOM | Context 初始化时跟踪 attachShadow 返回的 closed root；结构化采集遍历 open/closed 嵌套根，ref/semantic 用 ElementHandle 操作 | 支持被跟踪根中的点击、填充及 Secret 字段校验，保持 `.shadowRoot === null`。不强制把 closed 改成 open；不修改调用方的原生 Playwright selector 行为。声明式 closed root、安装跟踪前已创建的根无法回溯；closed 根内部 iframe 的自动遍历仍有限 |

结构化读取是有界的实用语义子集，尚未复刻 Node 的完整浏览器可访问性算法和 CDP/axe 对照。缓存语义匹配使用 role/name/test_id/frame 及 iframe 身份检查，普通页面重排可恢复；语义改名、重复目标、iframe 顺序变化或扫描截断应重新录制，不能承诺任意 DOM 改动都恢复。借用 Page 可在首次观察时安装当前文档跟踪，但已有 closed root 无法回溯；若要捕获后续导航创建的根，请在导航前使用 `page.add_init_script(SEMANTIC_INIT)`（见 README）。

当前 Playwright 缓存身份 policy 为 5，旧 key 需重新录制。strict 会检测可枚举存储中同一步骤的旧 key。

## 仍存在的 Web 能力差距

这些是剩余能力或集成差异，不属于 runner 开发要求：

| 优先级 | 能力 | Node 源码依据 | Python 当前边界与后续方向 |
| --- | --- | --- | --- |
| P2 | 云浏览器生命周期、断线恢复与远程文件 | `packages/web/src/provider.ts`、`cdp-recovery.ts`、`downloads.ts` | 目前只有静态 cdp_url/借用 Page；没有 acquire/release provider、重连身份校验、远端下载读取 |
| P2 | 更完整的重放与失败接管 | `packages/core/src/agent/replay.ts`、`step-cache.ts` | 已有唯一语义锚点重定位；当前只在终态不符时接管，缺少模糊重定位、完整失败分类与复杂动态参数推断 |
| P2 | 模型原生 tool calling、usage 与可追踪性 | `packages/core/src/agent/model/`、`usage.ts` | 目前 JSON 决策协议，可接函数 executor；没有统一 token/费用指标和完整 AI trace |
| P2 | Agent 跨标签页重放 | `packages/web/src/surface.ts`、`app-log.ts` | Agent 可在一个步骤切换和关闭标签。跨标签动作和非初始标签步骤实时执行。 |
| P2 | 有 Secret 时的图像及下载处理 | `packages/core/src/agent/pixels.ts`、`packages/web/src/downloads.ts` | SDK 禁用敏感截图，原生下载由宿主处理；尚无字段遮罩与文本下载重写 |
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

模型/判断接口有意不引入 AI SDK 特定类型。供应商 usage、token 账单、完整 AI trace 尚未实现。宿主可以在自定义 executor 中观测模型指标，但不得把 secret 原值写入日志。

## 有意保留的差异

- Python API 使用秒和 snake_case；Node 使用毫秒和 camelCase。
- Python 使用有界结构化语义子集、文档内节点 ID、最新观察引用和唯一语义锚点重定位；仍不等价于 Node 的 CDP/axe 对照及模糊重定位层。
- 观察包括主页面和至多 20 个 iframe，最深 8 层；特殊控件仍可能需要宿主通过原生 Page 操作。
- 缓存终态锚点为有上限的语义文本行 multiset delta，包含已观察的 iframe；不是 Node 的结构化 semantic node anchors。无效、无差异或包含工具/上传的动作流程不新建录制。
- 普通模式下，来源一致时的语义终态失配可交还模型，提供已执行前缀且该次不重录。重放途中或终点发生来源失配时直接停止。未移植 Node 全部 relocation/handoff 分类。
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
