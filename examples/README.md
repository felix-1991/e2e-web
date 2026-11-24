# Web 示例

## 安装与运行

在仓库根目录执行一次安装，再把实际的 DeepSeek API Key 填入 `.env` 的 `DEEPSEEK_API_KEY`。已有 `.env` 时，在原文件里补齐配置即可。

```bash
python -m pip install -e '.[test]'
python -m playwright install chromium
cp -n examples/deepseek.env.example .env
```

模板把 `E2E_EXAMPLE_PROVIDER` 设为 `deepseek`，以下命令可共用一份配置。

| 示例 | 直接运行 | 场景输入 |
| --- | --- | --- |
| DeepSeek | `python examples/deepseek_example.py` | 默认在内置表单里填写并保存姓名。可传 `--url`、`--task`、`--expected`。 |
| pytest | `python examples/pytest_example.py` | 默认使用内置表单。可用 `APP_URL`、`TASK`、`EXPECTED` 换成自己的任务。 |
| unittest | `python examples/unittest_example.py` | 默认使用内置表单。自定义输入与 pytest 相同。 |
| 中文语义动作 | `python examples/semantic_web_example.py` | 内置配送设置页面和中文任务。 |
| 百度搜索 | `python examples/baidu_search_example.py --query 'macos 常用软件'` | 搜索词也可通过 `BAIDU_QUERY` 提供。需能访问百度并打开搜索结果。 |
| 公开站点 | `python examples/live_web_examples.py --case todo-keyboard` | `--case` 可重复传入。省略时运行下文列出的四个场景。 |

也可以使用原生测试命令 `python -m pytest examples/pytest_example.py` 和 `python -m unittest examples.unittest_example`。测试结果由对应框架报告，缺少配置时会显示缺失项。

### 模型与浏览器配置

- DeepSeek 必填 `DEEPSEEK_API_KEY`。`DEEPSEEK_MODEL` 默认使用 `deepseek-flash`，可通过 `DEEPSEEK_MODEL` 和 `DEEPSEEK_BASE_URL` 指定自己的模型和服务地址。
- 使用兼容的 Chat Completions 服务时，在 `.env` 设置 `E2E_EXAMPLE_PROVIDER=compatible`、`E2E_MODEL_ENDPOINT`（完整的 `/chat/completions` 地址）和 `E2E_MODEL`。服务要求鉴权时填入 `E2E_MODEL_API_KEY`。`deepseek_example.py` 使用 DeepSeek 配置。
- 省略 `E2E_EXAMPLE_PROVIDER` 时，百度、pytest 和 unittest 默认使用兼容接口，中文语义动作和公开站点默认使用 DeepSeek。百度、中文语义动作和公开站点可以用 `--provider` 覆盖默认值。
- 示例从当前工作目录的 `.env` 读取配置，进程环境变量优先。DeepSeek、中文语义动作、百度和公开站点也支持 `--env-file`，用文件补充尚未设置的变量。可以设置 `E2E_CHROMIUM_EXECUTABLE` 使用已有的 Chrome。

### 运行自定义任务

DeepSeek 示例可以直接接收站点地址、任务和预期。命令行参数优先于 `APP_URL`、`TASK` 和 `EXPECTED`。三个输入须同时提供。

```bash
python examples/deepseek_example.py \
  --url https://demo.playwright.dev/todomvc/ \
  --task 'Add a todo titled Review invoices and press Enter to save it.' \
  --expected 'The todo list shows Review invoices.'
```

pytest 和 unittest 从环境读取这三个输入。可以将同一组值写在 `.env` 中，再运行上表的命令。

## 本地语义动作示例

[`semantic_web_example.py`](semantic_web_example.py) 用中文指令打开配送设置，将配送区域改为「华南」，再用 `assert_(explore=True)` 检查页面顶部和页脚。

使用支持图像的模型时，可给这次 `act()` 加上 `vision="on_demand"`，在任务里要求 Agent 先请求截图，再用 `select_at` 选择区域。DeepSeek executor 需设置 `DEEPSEEK_SUPPORTS_VISION=1`。

## 公开站点回归

`live_web_examples.py` 用 `session.agent.act()` 执行自然语言任务。Agent 从页面观察中定位控件、执行操作并判断何时完成。TodoMVC 用 `session.agent.assert_()` 断言筛选结果，示例也用 `session.expect()` 独立核对关键状态。每个场景使用独立的浏览器上下文和演示数据。

TodoMVC 的重命名任务在一个 `act()` 中完成双击、编辑和保存。

```python
session.agent.act(
    "Rename the todo titled exactly {original} to {edited}. Double-click the original title, replace the text "
    "in the focused edit field, and press Enter to save. If the saved row already shows {edited}, finish immediately.",
    {"original": "Review invoices", "edited": "Review revised invoices"},
)
```

完成该待办并切换到 Completed 筛选后，语义断言核对唯一可见的待办及其勾选状态。

```python
session.agent.assert_(
    "The Completed view shows exactly one todo, titled {title}, with its checkbox checked.",
    {"title": "Review revised invoices"},
)
```

| 场景 | 演示内容 | 站点 |
| --- | --- | --- |
| `todo-keyboard` | 用 `act()` 创建、重命名和完成待办，再用 `assert_()` 验证 Completed 筛选结果。 | [Playwright TodoMVC](https://demo.playwright.dev/todomvc/) |
| `selenium-multiselect` | Agent 根据选项名称识别多选框，用 `act()` 替换整组选中项，再用 `act_once()` 清空。 | [Selenium 表单](https://www.selenium.dev/selenium/web/formPage.html) |
| `dynamic-controls` | Agent 请求启用文本框，用 `wait_for()` 等待字段可编辑，再用 `act()` 输入测试值。 | [The Internet 动态控件](https://the-internet.herokuapp.com/dynamic_controls) |
| `new-tab` | 用中文指令定位英文页面上的 `Click Here` 链接，打开并切换到新标签页。提取标题原文后关闭标签并回到原页。 | [The Internet 多窗口](https://the-internet.herokuapp.com/windows) |

```bash
python examples/live_web_examples.py
python examples/live_web_examples.py --case selenium-multiselect --case new-tab
```

每次运行都会在 `.e2e/live/examples/run-*` 写入 `report.json` 和按场景归档的 `steps.json`、截图。`--output` 可以指定证据的父目录。报告记录实际执行的动作和模型调用数，有任意场景失败时命令退出码为 1。

新增场景时，可以将场景函数和演示站点 URL 加到 `CASES`。在 `act()` 指令里给出控件线索和完成条件，通过 `params` 传入测试数据。用 `assert_()` 描述要验证的可观察结果，用 `wait_for()` 等待异步状态，用 `session.expect()` 或 `session.verify()` 精确核对控件值或 URL。需要执行一次动作就返回时，可以用 `act_once()`。

[百度搜索示例](baidu_search_example.py) 演示搜索、分页和打开结果。百度触发安全验证时，示例会报告阻塞。
