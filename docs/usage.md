# e2e-web 使用参考

第一次使用可从 [README 的快速开始](../README.md#快速开始) 运行本地表单示例。可运行的集成示例见 [示例指南](../examples/README.md)。

SDK 面向浏览器中的网页和 Web 应用。测试发现、编排和重试由调用方使用 pytest、unittest 或自己的测试框架管理。

## 环境变量与优先级

SDK 从进程环境和当前工作目录的 `.env` 读取配置。显式参数优先，其次是进程环境、`.env` 和 SDK 默认值。完整模板见 [环境变量示例](../.env.example)。

仓库的 `.gitignore` 会忽略本地 `.env` 和 `.e2e/`。`.e2e/` 可能包含浏览器登录态、缓存、步骤记录和截图。

| `.env` 配置 | 内置默认值 | 生效范围 |
| --- | --- | --- |
| `E2E_HEADLESS=false` | `true` | `Web()` 启动的本地浏览器。设为 `false` 时显示窗口。 |
| `E2E_CHROMIUM_EXECUTABLE=/path/to/chrome` | 使用 Playwright 安装的 Chromium | `Web()` 启动的本地 Chromium。 |
| `E2E_VISION=true` | `false` | `act`、`assert_`、`wait_for`、`extract` 的会话默认值。启用后，执行器的模型请求可附带页面截图。 |
| `E2E_OBSERVATION_MAX_NODES=1500` | `200` | 单次结构化观察的语义节点数上限。 |
| `E2E_OBSERVATION_MAX_BYTES=1048576` | `65536`（64 KiB） | 单次结构化观察序列化后的字节数上限。 |
| `DEEPSEEK_SUPPORTS_VISION=true` | `false` | 为确实支持图像的 DeepSeek 模型或网关声明图像能力。 |

浏览器启动选项依次采用 `Web` 显式参数、`launch_options` 中的同名选项、环境配置和默认值。`Web(executable_path="")` 可跳过环境中的 Chromium 路径。会话可用 `AgentConfig(vision=False)` 覆盖 `E2E_VISION`，单次调用仍可指定 `vision=True` 或 `vision=False`。布尔配置支持 `true` / `false`、`1` / `0`，留空使用默认值。

`.env` 只从当前工作目录加载。需要指定文件时，在创建 SDK 对象前调用 `load_env("config/test.env")`。传入 `override=True` 可覆盖已有环境变量。

## 语义断言


内置 Chat Completions/DeepSeek 适配器在纯文本 `act` 步骤默认保持一段会话：首轮发送完整屏幕，后续只发送 `changes`（added/changed/removed/unchanged），节点别名在整个步骤内稳定。`AgentConfig(stateful=False)` 可关闭；启用视觉、包含 iframe、探索断言和修复请求时自动回退到独立请求，并在下一次纯文本轮次重新发送整屏。已有节点换序或新节点插入列表中间时，适配器会发送完整节点列表，保留当前页面的顺序。增量轮次只发新增动作历史。节点变更里的 `unset` 列出已消失的字段，本轮对话框和下载事件也会随屏幕发送。历史消息超过 64 KiB，或继续附带旧消息会超过已配置的输入预算时，适配器用当前整屏和有界历史重新开始会话，沿用已有节点别名。

`session.agent.assert_()` 接收自然语言预期和可选的 `params`，调用模型根据当前页面的结构化观察判断结果。预期可以描述可见文本、列表内容、控件是否可编辑或复选框是否勾选。启用 `vision=True` 时，支持图像的模型还可以使用页面截图。方法名末尾的下划线用于避开 Python 关键字 `assert`。

用 `session.agent.assert_("列表顶部和底部分别显示开始和结束标记", explore=True)` 可滚动取证。探索允许滚动、查找目标和调用显式注册的只读工具，默认最多 10 次滚动和 6 次模型调用，受会话上限约束。证据不足返回 `ASSERTION_INCONCLUSIVE`，已看到反例返回 `ASSERTION_FAILED`。探索结束时保留当前滚动位置。

模型判定不通过时抛出 `AssertionFailed`。预期状态需要等待时，用 `session.agent.wait_for()` 轮询同一条件。控件值、URL 等需要精确比较时，可以用 `session.expect()` 或 `session.verify()` 进行确定性断言。

## 确定性操作与验证

`session.page` / `session.browser` 是原生 Playwright Page，因此可直接使用 role/text/label/test-id/CSS 定位器、frame_locator、键盘鼠标、路由、cookies、storage_state、下载、弹窗和多标签页 API。明确知道某个动作会打开新标签时，可以使用 `open_popup()`。

```python
with Web() as web:
    session = web.session(name="catalog", app_identity="shop", executor=model)
    session.page.goto("https://your-app.example/catalog")
    popup = session.open_popup("点击第一条商品链接，在新标签页打开详情。", name="product")
    print(popup.page.title())
```

`open_popup()` 先监听原页面，再执行一个动作，等待新页面的 `DOMContentLoaded` 后返回会话。动作和等待共用同一个超时，默认沿用 `AgentConfig.timeout`。子会话继承模型、Agent 和缓存配置，父会话关闭时一起清理。单独关闭子会话也会关闭它打开的标签页。

也可以在同一个 `act` 中打开、切换和关闭弹窗。`observation.tabs` 给出会话内的标签编号，`active_tab` 标识当前页。`{"op":"switch_tab","tab":2}` 切换后，`session.page` 指向活动页，旧节点和截图引用失效。`{"op":"close_tab","tab":2}` 关闭活动页后通常返回打开它的标签。最后一个标签和借用的初始页受保护。`Web.attach` 只接纳附加后由本会话页面打开的标签。跨标签步骤回放前会核对当前标签、各标签的完整 URL 和打开关系。

```python
session.page.get_by_label("Email").fill("demo@example.test")
session.expect(session.page.get_by_role("status")).to_have_text("Saved")

# 已有的 pytest / unittest / Playwright 断言可通过 verify 纳入缓存验证。
session.verify(lambda: existing_assertion(), label="Saved state")
```

只有 `session.expect`、`session.verify` 或通过的 agent 判断会确认之前的 act 录制。SDK 无法自动识别在外部执行的普通 `assert`。原生 Page 调用不进入 SDK 的动作预算和步骤事件；`verify` 内的断言应使用原生 Page 或已有数据，不要重入 `session.observe()` 等 SDK 步骤。

## Agent 与模型

`AgentConfig` 设置 `timeout`、`judgment_timeout`、`action_timeout`、`max_input_tokens`、`max_actions`、`max_model_calls`、`context`、`name`、`stateful`、`on_before_action`、`on_action` 和 `vision`。`action_timeout` 是单个浏览器操作的预算（默认 20 秒）：控件未在预算内变为可操作时该动作失败并交还模型，不会耗尽整步时间。`max_input_tokens` 设置模型上下文上限后，SDK 在把请求交给任何 executor 之前先按该上限裁剪观察（保留 URL、标题、标签页和最靠前的节点，并标记 `truncated`），自定义 executor 同样受保护。会话还可指定总 `timeout` 和 `Cancellation`。同一会话不允许步骤重入或跨线程调用；并行测试应各自创建宿主/会话。

- `act(instruction, params=None, timeout=None, max_actions=None, max_model_calls=None, vision=None)` 返回 `ActResult(summary, actions, model_calls, cache)`。
- `act_once(instruction, params=None, timeout=None, max_model_calls=None, vision=None)` 执行一个动作后返回 `ActResult`。动作成功后直接结束，适合再用 Playwright 断言验证结果。`act` 的 `max_actions` 仍表示预算上限。
- `assert_(instruction, params=None, timeout=None, vision=None, explore=False)` 执行实时语义断言。默认判断对象格式不符时至多请求修复一次。探索模式可指定 `max_actions` 和 `max_model_calls`。
- `wait_for(instruction, params=None, timeout=None, interval=1, max_model_calls=None, vision=None)` 轮询实时判断。
- `extract(instruction, schema=..., timeout=None, vision=None)` 接受 JSON Schema 或 Pydantic v2 模型类。数据须通过 Schema 校验，失败时至多修复一次。提取不确认录制。

`judge=` 可单独配置判断模型。`ChatCompletionsExecutor` 使用兼容 HTTP messages 接口；`FunctionExecutor` 接入你已有的 Python 模型 SDK。内置 HTTP executor 对 5xx、429 和连接超时按指数退避重试（单次请求另有 120 秒 stall 上限），provider 报上下文过长时归类为 `CONTEXT_OVERFLOW` 并触发一次收缩重试。`price_per_mtok=(输入, 输出)` 可按百万 token 单价估算费用，`session.total_usage` 汇总整场用量（含 `peak_prompt_tokens` 和 `estimated_cost_usd`）：

```python
from e2e_web import FunctionExecutor

def decide(request, *, timeout):
    # 接入现有 SDK；必须遵守 timeout，并返回 JSON 字典。
    return your_model_sdk.decide(request, timeout=timeout)

executor = FunctionExecutor(decide)
```

`ChatCompletionsExecutor` 向完整的 `/chat/completions` 地址发送非流式请求，从 `choices[0].message.content` 解析 JSON 对象。API Key 留空时省略鉴权头，适用于允许匿名访问的兼容服务。服务支持 JSON 模式时，可用 `json_mode=True` 发送 `response_format={"type": "json_object"}`。

请求含 `kind`、脱敏 `instruction/params/context/observation`；act 还含历史动作、最多 8 KiB 的前序步骤摘要 `prior_steps` 与 project tools。判断/提取不接收历史摘要。返回协议、动作和目标格式在 [ALIGNMENT.md](../ALIGNMENT.md) 中。SDK 负责动作校验、预算、记录与验证；executor 只决策。结构不合法的 act 响应、未知工具及工具参数错误会作为 `dispatched=False` 历史反馈，连续三次拒绝后停止；它们消耗模型调用预算，不执行动作。策略拒绝或动作执行失败仍直接停止。模型网络错误不会被当作应用断言失败。没有凭据时可配置脚本 executor 做确定性验证；会话仍使用调用方提供的 executor。

`ChatCompletionsExecutor` 和 `DeepSeekExecutor` 的内置提示要求模型在保存、发送或提交后检查当前页面证据。仍在处理时等待，结果无法判断时返回阻塞，看到明确失败并修正原因后才可重试。

## Web 动作与扩展工具

模型可在操作步骤或探索断言中返回 `{"action":{"op":"observe"}}`，重新读取正在变化的页面。该请求占用模型轮次，浏览器动作计数保持不变。

Agent 支持 navigate/back/forward/reload、click/double_click/right_click/long_press、fill/type/press、check/uncheck/select、hover/drag、scroll/wait、upload 和 fill_secret。click、double_click 和 right_click 可传 `modifiers`，如 `["Shift"]` 或 `["ControlOrMeta"]`。long_press 按住鼠标 500 毫秒。目标支持 role/name、text、label、placeholder、test_id、CSS，及 `nth` 和 iframe scope。`frame` 可为单个选择器或从外到内的选择器列表。观察按广度优先读取最多 20 个 iframe、最深 8 层，返回可直接用于定位的 `frame` 列表。

- `select` 支持标签字符串或单项描述符 `{"label":"North"}`、`{"value":"N"}`、`{"index":0}`。选项分组出现同名项时可用 `{"group":"South","label":"Office"}`，回放时会重新核对组名及标签。索引从 0 开始。原生 `<select multiple>` 可传数组，例如 `["North", {"value":"S"}, {"index":3}]`。数组表示完整选择，`[]` 清空选择。批量操作先验证选项，缺失、歧义或禁用的选项会让整次选择失败。
- `press` 和 `type` 可省略 `target` 操作当前键盘焦点。可在点击自定义组合框后输入，再用方向键和 Enter 选择。焦点输入可进入 iframe 和已跟踪的 closed Shadow DOM。省略 `target` 的 `type` 可加 `"replace":true` 替换可编辑字段。含无目标键盘动作的步骤不生成缓存录制。
- `press` 可用 `times` 连按 1 到 20 次，第一个按键使用指定目标，后续按键跟随焦点。每次按键计入动作预算，连按步骤实时执行。当前焦点内的文本选区通过 `selection` 暴露，密码、验证码及登记 Secret 的会话会隐藏选区。
- `check` / `uncheck` 对已处于目标状态的控件直接返回。需要变更时点击一次，并在动作超时内等待状态更新，适用于延迟提交状态的 ARIA 开关。
- `{"op":"resize_viewport","width":390,"height":720}` 将当前 Page 调整到指定尺寸。每边接受 200 到 4096 的整数 CSS 像素。后续观察返回 `viewport` 和调整后的语义树，旧截图和节点引用失效。包含视口调整的步骤实时执行。

Agent navigate 只允许有效 http/https URL 和精确的 `about:blank`，相对路径基于 `base_url` 解析。原生 `session.page.goto()` 是调用方自行管理的可信接口。自建会话的 `init_scripts=("JavaScript source", Path("init.js"))` 在创建页面之前安装到 Context，作用于后续页面、弹窗和 iframe；借用 Page 时由调用方配置初始化脚本。

上传可指向文件输入框，或点击后打开文件选择器的控件。上传默认允许当前工作目录（项目根），拒绝隐藏路径和根外路径；`upload_roots=(...)` 可把允许范围收紧，传空 tuple 可禁止项目文件（内置 `sample:` 仍可用）。内置样本可用 `sample:photo.png`、`sample:photo.jpg`、`sample:document.pdf`、`sample:notes.txt`、`sample:table.csv`、`sample:empty.txt`，无需项目文件。`Tool(name, description, schema, handler, mutates=False)` 可注册受信任的业务工具；参数按 JSON Schema 校验。变更型工具占动作预算。含工具的 `act` 实时执行。要录制上传，请在本次 `act` 或 `act_once` 的 `params` 中逐个传入文件路径。回放会在执行首个动作前核对路径和当前会话的 `upload_roots`，并在实际上传时再次解析文件。`assert_(explore=True)` 只会调用标记为只读的项目工具。

## 结构化观察、视觉与 closed shadow DOM

`Web.session()` 和 `Web.attach()` 在创建会话时自动采用 `E2E_OBSERVATION_MAX_NODES` 和 `E2E_OBSERVATION_MAX_BYTES`。节点数可设为 1 到 100000，字节数可设为 4096 到 1048576。需要单独配置某个会话时，可以传入 `observation=ObservationConfig(...)`，该对象优先于环境配置。

```python
session = web.session(name="checkout", app_identity="shop", executor=model)
observation = session.observe()
# observation: id / nodes / tree / frames / truncated / visited / url / title / viewport
# active_tab / tabs / tab_count
# nodes: id / parent_id / role / name / text / test_id / value / disabled / editable /
#        readonly / checked / indeterminate / selected / expanded / pressed /
#        focused / secure / purpose / attributes / active_descendant / scrollable /
#        input_type / bounds / in_viewport / frame
# dialogs: 本步出现过的原生对话框 {type, message, action}
# downloads: 本步应用启动的下载 {suggested_filename, url}
```

无原生角色的可见图标可通过 `data-testid` 等属性进入观察。`parent_id` 和缩进文本树显示表格行、单元格、列表项及其他已采集的祖先关系，可用来区分同名按钮。`scrollable` 表示可滚动的轴（`x`、`y`、`xy` 或空字符串）。半选单独记录在 `indeterminate`，只读或禁用输入的 `editable` 为 false。

原生 `<dialog>` 通过 `showModal()` 打开时，观察只读取最上层模态框的活动内容和其中的 iframe。动作执行后 SDK 会按动作种类等待屏幕变化（点击、导航约 2 秒，输入约 0.4 秒），让延迟渲染的结果有机会进入下一次模型决策。

原生 `alert` / `confirm` / `prompt` / `beforeunload` 默认被 dismiss。需要确认时先返回 `{"op":"dialog","action":"accept"}`，`prompt` 可带 `text`（至多 1024 字符）。对话框出现后会列在 `observation.dialogs` 中，包含类型、消息和实际处理方式，因此「点了删除但其实被取消」不会再被当成成功。

动作失败（目标未出现、控件不可操作、引用失效）会把错误码写回模型历史并让模型改道，而不是终结整步；同一动作连续失败两次或整步失败五次才停止。`text` 是节点显示的行内文本（直接文本加内联后代），`secure` 表示密码或一次性验证码字段，`purpose` 取 `password` / `one-time-code` / `username`，`attributes` 按白名单给出 `type`、`autocomplete`、`required`、`placeholder` 与全部 `aria-*`。

Agent 优先使用 `{"by":"ref","value":"节点 id","observation":"本次 observation.id"}`。节点 id 在当前文档内稳定，动作只接受最新观察中的引用。目标脱离、隐藏或身份变化时反馈 NODE_STALE，重新观察后再决策，连续三次失效停止。录制自动转换为 role/name/test_id/frame 的语义锚点。同名控件可用唯一祖先缩小范围，例如 `{"by":"semantic","value":"button","name":"编辑","within":{"role":"row","test_id":"account-lin"}}`。观察截断或匹配仍有歧义时，引用动作不会录制。`truncated=True` 表示预算内只有部分页面证据，必要时增大预算或滚动。

原生搜索框、提交按钮和选择框提供对应的角色与名称。选择框节点提供 `multiple`，其 `options` 包含标签、索引、选中和禁用状态。每个选择框最多保留 32 项，优先保留已选项。节点的 `value` 提供表单内部值。自定义组件可通过 `active_descendant` 暴露键盘当前高亮的选项名称，通过 `multiselectable` 暴露 ARIA 多选状态。

内置 HTTP 适配器的每个请求都带有当前预算内的紧凑节点表及本次观察中的对话框、下载和 iframe 信息。同一步内复用节点短编号，便于将动作历史对应到当前控件。

`agent.act(..., vision=True)` 随模型调用附上截图。`vision="on_demand"` 先发语义观察，模型可返回 `{"action":{"op":"screenshot"}}`，此后的请求附上当前截图。截图请求占用一次模型调用预算，不占浏览器动作预算。坐标动作包括 `click_at`、`hover_at`、`drag_at`、`type_at`、`press_at` 和 `select_at`。`press_at` 的 `value` 是按键，`select_at` 的 `value` 是原生选择框的可见选项标签。`type_at` 可用 `replace:true` 替换可编辑字段。截图参数必须匹配当前 `visual.id`。SDK 截图后按长边 768 降采样，坐标是图像像素；执行前按 `visual.scale` 映射回视口，并检查 URL、范围和像素变化。坐标回放要求启用视觉，并逐个核对操作前截图及最终画面。SDK 创建的浏览器会话在语义观察超时时可向模型提供遮罩截图和 tree_unavailable 标记。已登记 Secret 时禁用 vision。

`scroll_to(target)` 把已知节点滚入视口。带 `target` 的 `scroll` 从节点的实际命中点向上找可滚动祖先并滚动，选中外层 wrapper 也能生效；容器及其祖先都不能滚动时返回 `ACTION_FAILED`。`scroll_find(target,direction,steps,container?)` 支持上下左右，在动作截止时间内最多滚动 800 次。到达边缘时会短暂等待列表追加。搜索目标使用可重定位描述符，可选的 `container` 可以使用当前 ref，搜索只匹配同一 iframe 内该容器的后代。容器参数省略时使用页面滚轮。搜索产生可验证的终态变化且后续断言通过时可以录制；回放会重新定位容器，再按原方向和步数上限搜索。仅改变滚动位置的单步搜索没有语义终态差异。

自建 Context 在文档脚本前跟踪动态 closed shadow root，结构化观察和 ref/semantic 操作可访问其中的控件；原生 Page 定位器保持 Playwright 行为。借用 Page 的已有 closed root 无法回溯，声明式 closed root 同样不保证可访问。对借用页面，可在导航前显式安装跟踪：

```python
from e2e_web.semantics import SEMANTIC_INIT
page.add_init_script(script=SEMANTIC_INIT)
page.goto(APP_URL)
with Web.attach(page, name="borrowed", app_identity="shop", executor=model) as session:
    session.agent.act("操作组件内的控件")
```

这套语义读取是有界子集。完整的能力对照见 [ALIGNMENT.md](../ALIGNMENT.md)。

## DeepSeek 模型配置

DeepSeek 官方 [API 快速开始](https://api-docs.deepseek.com/)和[视觉说明](https://api-docs.deepseek.com/guides/vision/)使用 `deepseek-flash`，该模型支持图像输入。JSON 响应配置见 [DeepSeek JSON 输出文档](https://api-docs.deepseek.com/guides/json_mode/)。模型名称以服务商提供的 ID 为准，自部署服务也可通过通用 ChatCompletionsExecutor 接入。

```bash
export DEEPSEEK_API_KEY='你的 API Key'
export DEEPSEEK_MODEL='deepseek-flash'
```

```python
from e2e_web import AgentConfig, DeepSeekExecutor, Web

model = DeepSeekExecutor(request_options={"max_tokens": 4096})
with Web() as web:
    with web.session(name="search", app_identity="my-app", executor=model,
                     agent=AgentConfig(timeout=120, judgment_timeout=60)) as session:
        session.page.goto("http://localhost:3000")
        session.agent.act("搜索 Python 文档")
        session.agent.assert_("显示相关搜索结果")
```

`DeepSeekExecutor` 默认开启 JSON 输出并关闭 thinking。模型名和 API Key 来自构造参数或环境配置。确认模型能接收图像后，可在 `.env` 同时设置 `DEEPSEEK_SUPPORTS_VISION=true` 和 `E2E_VISION=true` 来发送截图。模型 HTTP timeout 与 Agent 的步骤 deadline 分别配置，实际请求取 HTTP 上限与步骤剩余时间的较小值。默认判断复用 executor，也可通过 `judge=model2` 单独配置。

通用兼容服务使用 `ChatCompletionsExecutor(endpoint="https://你的网关/v1/chat/completions", model="服务商模型名", api_key=..., json_mode=True, request_options={"max_tokens":4096})`；endpoint 是完整路径。`request_options` 可传供应商参数，但不能覆盖 model/messages/stream。SDK 默认加载当前工作目录的 `.env`。可运行示例与环境变量清单见 [examples/deepseek_example.py](../examples/deepseek_example.py) 和 [examples/deepseek.env.example](../examples/deepseek.env.example)。

使用 DeepSeek API 在公开站点验证了 Python 官网导航、Python 文档搜索、PyPI 发行历史、公开下拉框和视觉坐标点击。可通过 `--env-file` 指定现有配置并用脚本复跑，报告默认写入本地 `.e2e/live/`。[在线验证脚本](../scripts/live_web_validation.py)

内置 HTTP 适配器可通过 `max_input_tokens=64000` 设置单次请求的输入预算，默认不设置；`AgentConfig(max_input_tokens=...)` 则在 harness 层约束所有 executor。SDK 按文本的 UTF-8 字节数和截图的 28×28 像素块估算，实际 token 用量以供应商返回值为准。超出预算时按观察顺序保留能放入请求的节点，并设置 `truncated`。指令、历史和图片等固定内容无法容纳有效观察时，SDK 在调用服务前抛出 `STEP_BUDGET_EXHAUSTED`；provider 自己在 400 响应里报上下文过长时归类为 `CONTEXT_OVERFLOW`，先收缩历史与节点再重试一次。

## Secret、动态参数与隐私

```python
from e2e_web import Secret, Unique

session.agent.act("填写凭据", {
    "password": Secret("password", os.environ["TEST_PASSWORD"]),
})
session.agent.act("创建记录", {"name": Unique(run_specific_name)})
```

Secret 向 executor 仅暴露名字和用途，通过 `fill_secret` 在执行边界解析。填充前检查目标可编辑且启用；`Secret(..., purpose="password")` 只允许密码输入框。默认 `purpose="credential"` 是通用敏感值，不按名字推断用途。每次 `act` 都需要在本次参数中传入对应的 `Secret` 对象才能填入其值。缓存重放也重新授权；自定义 Engine 必须实现 `fill_secret(descriptor, secret, timeout=...)` 并执行相同检查，否则拒绝填充。已注册值从观察、历史、错误和 SDK 步骤文件中脱敏；缓存只保存 secret 名称。创建会话时还可通过 `secrets=(...)` 提前登记页面已有敏感值。存在任何已登记 secret 时，SDK screenshot 和 vision 都禁用，避免像素泄漏。

Unique 将本次字符串替换为参数位置的模板槽，使新值复用录制。重叠或重复的 Unique 值拒绝使用。请使用完整且足够独特的值，避免与页面无关文本发生子串碰撞。

这不是第三方 Python 代码沙箱。自定义工具、executor、直接 Page API、storage_state 和 Playwright 原生 trace/video 由宿主管理，不受 SDK 脱敏机制保护。未登记的敏感信息也不会自动识别。

## 缓存、CI 和证据

- 本地默认 read-write；`CI=true` 且未明确指定 mode 时 read-only。支持 `off`、`read-only`、`read-write`；strict 强制只读。
- 默认 `.e2e/cache` 相对当前工作目录；通过 `CacheConfig(directory=...)` 改变。SDK 不读取 CLI 或 Node 配置文件。测试重试时传 `CacheConfig(attempt=1)`：该次尝试不读取上次的录制（那份录制刚刚失败过），只重新录制。
- key 含 project、会话 name、app_identity、引擎协议、agent、脱敏上下文、指令、模板参数和同签名出现序号；secret 明文不进 key。
- 新录制先暂存；后续通过的实时验证确认后，session.close 才保存。未确认条目在读写模式下删除。提取与 act 自己的 done 都不算确认。先前已验证的录制不因后续独立失败被丢弃。
- Playwright Agent 对 `autocomplete="one-time-code"` 字段执行定向 `fill`、`type` 或 `press` 时，整步实时执行并跳过录制。语义观察、该动作的回调和后续模型历史会遮蔽该值。已有缓存命中这类字段会在输入前抛出 `REPLAY_STALE`，读写模式会移除失效条目。调用方可用 `Secret` 传入敏感参数。
- 重放检查主页面起止时的来源和路由，并在每个录制动作前核对来源。来源由协议、主机和有效端口决定。Playwright 的导航动作也核对解析后的目的地来源。录制路径相对地址时会保存当次解析出的目标路径。跨路由后继续操作的步骤，录制和回放都以到达目标页面时的观察为基准，核对随后发生的变化。
- 终态校验通过语义行的出现和消失数量验证变化，并检测主页面和已观察 iframe 新增的非预期告警。无关页面文本可随运行变化。
- 起始位置失配时，普通模式重新进行实时决策，strict 抛出 REPLAY_STALE。重放途中或终点发生来源失配时，两种模式都会停止。普通多动作 `act` 回放时，若直接操作的语义目标在派发前已消失，executor 会收到最新观察、已执行动作和标记为 `dispatched=false` 的缺失动作，可用剩余预算继续。语义终态失配也可交接。读写模式下，如果回放的首个动作未找到目标，且尚未执行任何录制动作，SDK 可从当前页面重新录制。若动作可能已生效，普通多动作步骤会提示 executor 先检查页面。严格回放、`act_once`、定位歧义和步骤超时会停止当前回放。滚动搜索和密钥填入保留各自的执行检查。
- 新录制的 ref 可以识别 test id 中的 UUID、长十六进制片段和时间戳。录制时先确认同角色、名称和容器内的标识形状唯一。回放优先使用精确 test id，精确值消失后才尝试同形状的唯一目标。旧录制沿用精确匹配。
- Playwright 缓存 policy 已升级为 15，录制会核对输入类型、标题层级及表格行内的控件状态。请在读写模式下重新运行并验证用例，生成新录制后再用于严格模式。strict 会检测可枚举存储中同一步骤的旧 key。自定义 store 可提供 `entries()` 供扫描。
- 支持自定义 `CacheStore`。本地文件上限 1 MiB，校验后使用，0600 原子写入。Python 缓存使用独立格式，不兼容 Node 的 trace-1 文件。
- 指定 `artifacts_dir` 时关闭会话写 `steps.json`。`session.steps` 和 `on_step` 可接你自己的报告/日志系统。`session.screenshot(path)` 按需截图。

CI 可 checkout 已提交录制或恢复共享文件，再使用 `CacheConfig(strict=True)`；预热任务显式 read-write。缓存目录应与 steps、视频等证据分开。模型判断始终实时执行，因此缓存不是“零模型费用”。

## 结构化错误与生命周期

捕获 `E2EError` 并检查 `.code`；断言异常 `AssertionFailed` 同时属于 `AssertionError`，超时异常 `StepTimeout` 同时属于 `TimeoutError`。常见代码：MODEL_UNAVAILABLE、MODEL_PROVIDER_FAILED、MODEL_OUTPUT_INVALID、ASSERTION_FAILED、STEP_TIMEOUT、STEP_BUDGET_EXHAUSTED、POLICY_DENIED、REPLAY_STALE、CANCELLED、SESSION_CLOSED、CONCURRENT_OPERATION。

同步回调与取消是协作式的：SDK 会在操作前后检查截止时间；Playwright/内置 HTTP 请求接收剩余 timeout。Python 不能安全强制打断不合作的自定义 executor 或 callback。session.close 幂等，资源清理即使证据写入失败也会执行。


### 托管 Chromium 与测试 ID

`Web(provider=lease)` 接受无参数的同步上下文管理器工厂。进入管理器时取得 CDP 地址，退出时由管理器释放租约。一个 Web 宿主的所有会话共享租约，每个会话创建独立的浏览器上下文。连接失败也会退出管理器。已有固定地址可继续使用 `Web(cdp_url=...)`。`cdp_headers` 只传给 CDP 握手，可用于需要认证的网关。

本地 Chromium 在 macOS 和 Windows 默认启用浏览器沙箱，Linux 沿用 Playwright 的默认关闭设置。宿主可通过 `launch_options={"chromium_sandbox": True}` 显式设置。

站点用 `data-qa` 标记控件时，创建 `Web(test_id_attribute="data-qa")`。该宿主创建的上下文中，Agent 观察、回放、iframe 与原生 `page.get_by_test_id()` 使用同一属性。自定义属性进入会话缓存身份。
