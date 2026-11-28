# e2e-web

e2e-web 是基于 Python Playwright 的同步网页自动化 SDK。你可以直接用定位器填写表单、点击按钮和断言，也可以接入模型，用自然语言操作页面、等待状态或提取结构化数据。SDK 可在 Python 脚本、pytest 和 unittest 中使用。

## 快速开始

需要 Python 3.11 或更新版本，以及 uv。[uv 安装说明](https://docs.astral.sh/uv/getting-started/installation/)

以 `e2e-web` 为名的发行包在 PyPI 上架后，可在新建的工作目录运行以下命令。现有源码副本可进入「运行仓库示例」按源码安装。

```bash
uv venv --python '>=3.11'
source .venv/bin/activate
uv pip install e2e-web
python -m playwright install chromium
```

Windows PowerShell 使用 `.\.venv\Scripts\Activate.ps1` 激活虚拟环境。发行包名称为 `e2e-web`，Python 导入名称为 `e2e_web`。

### 运行第一个浏览器操作

把以下代码保存为 `quickstart.py`。它在本地 HTML 中填写姓名并断言输入值，运行时无需模型、API Key 或外部网站。

```python
from e2e_web import Web

with Web() as web, web.session(name="quickstart", app_identity="local-demo") as session:
    session.page.set_content('<label>Name <input type="text"></label>')
    name = session.page.get_by_label("Name")
    name.fill("Ada")
    session.expect(name).to_have_value("Ada")

print("浏览器操作通过")
```

运行 `python quickstart.py`，终端会输出「浏览器操作通过」。想看浏览器操作过程，可以把 `Web()` 改为 `Web(headless=False)`。

接入自己的站点时，把 `set_content()` 换成 `session.page.goto("https://your-app.example")`，再用页面上的标签、角色或测试 ID 定位控件。每次 `web.session()` 都会创建一个独立的浏览器上下文，退出 `with` 时自动清理。

## 用自然语言操作页面

下面的示例在一个有站内搜索功能的页面上搜索「分布式系统」，等待并验证搜索结果，然后提取标题和链接。

在运行脚本的目录新建 `.env`，填入你的服务商提供的完整 Chat Completions 地址、模型 ID 和 API Key。把 `APP_URL` 改成你自己的搜索页地址。[配置模板](.env.example)

```dotenv
APP_URL=https://your-app.example/search
E2E_MODEL_ENDPOINT=https://api.openai.com/v1/chat/completions
E2E_MODEL=your-model-id
E2E_MODEL_API_KEY=your-api-key
```

把以下代码保存为 `search.py`，在同一目录运行 `python search.py`。成功时会打印一个列表，每项包含 `title` 和 `url`。

```python
import os

from e2e_web import CacheConfig, ChatCompletionsExecutor, Web

model = ChatCompletionsExecutor()
search_params = {"query": "分布式系统"}
result_schema = {
    "type": "array",
    "maxItems": 3,
    "items": {
        "type": "object",
        "properties": {"title": {"type": "string"}, "url": {"type": "string"}},
        "required": ["title", "url"],
    },
}

with Web() as web, web.session(
    name="search",
    app_identity="my-app",
    executor=model,
    cache=CacheConfig(directory=".e2e/cache"),
    artifacts_dir=".e2e/evidence/search",
) as session:
    session.page.goto(os.environ["APP_URL"])
    session.agent.act("在站内搜索框输入 {query} 并提交搜索，直到打开搜索结果页。", search_params)
    session.agent.wait_for("结果列表加载完成", timeout=15, interval=1)
    session.agent.assert_("搜索结果列表至少显示一条与 {query} 相关的结果。", search_params)
    result = session.agent.extract("提取前三条结果", schema=result_schema)

print(result)
```

| 需求 | 接口 |
| --- | --- |
| 用定位器操作控件，精确核对值或文本 | `session.page`、`session.expect()` |
| 用自然语言描述操作和完成条件 | `session.agent.act()` |
| 等待异步加载，判断页面是否符合预期 | `session.agent.wait_for()`、`session.agent.assert_()` |
| 提取符合 JSON Schema 或 Pydantic 模型的数据 | `session.agent.extract()` |

SDK 的超时单位为秒，原生 Playwright 方法的超时单位为毫秒。示例在会话结束时把步骤记录写到 `.e2e/evidence/search/steps.json`。

使用 DeepSeek 时，在 `.env` 设置 `DEEPSEEK_API_KEY` 和服务商提供的 `DEEPSEEK_MODEL`。在 Python 示例中导入 `DeepSeekExecutor`，并设置 `model = DeepSeekExecutor()`。[DeepSeek 配置与视觉选项](docs/usage.md#deepseek-模型配置)

## 运行仓库示例

想先用内置演示页面体验模型操作，或要运行 pytest / unittest 示例，可以克隆仓库并安装可编辑版本。

```bash
git clone https://github.com/felix-1991/e2e-web.git
cd e2e-web
uv venv --python '>=3.11'
source .venv/bin/activate
uv pip install -e '.[test]'
python -m playwright install chromium
```

在仓库根目录运行以下命令，再编辑 `.env`，填入 `DEEPSEEK_API_KEY` 和实际可用的 `DEEPSEEK_MODEL`。如果已有 `.env`，直接补充配置即可。

```bash
cp -n examples/deepseek.env.example .env
```

| 想试的功能 | 运行命令 |
| --- | --- |
| 用模型填写内置表单并验证保存结果 | `python examples/deepseek_example.py` |
| 用中文指令修改配送设置 | `python examples/semantic_web_example.py` |
| 在 pytest 中运行同类任务 | `python -m pytest examples/pytest_example.py` |
| 在 unittest 中运行同类任务 | `python -m unittest examples.unittest_example` |

示例指南介绍兼容接口配置、如何传入自己的站点和任务，以及如何运行公开站点示例。[完整示例指南](examples/README.md)

## 接入现有 Playwright 页面

已有同步 Playwright `Page` 时，可以用 `Web.attach()` 借用它。以下代码可放在使用 `pytest-playwright` 的测试中，`page` 由该插件提供。

```python
from e2e_web import Web


def test_profile_form(page):
    with Web.attach(page, name="profile", app_identity="profile-app") as session:
        session.page.set_content('<label>Email <input type="email"></label>')
        email = session.page.get_by_label("Email")
        email.fill("demo@example.test")
        session.expect(email).to_have_value("demo@example.test")
```

原有 Page、Context 和 Browser 仍由调用方管理。需要自然语言操作时，给 `Web.attach()` 传入 `executor=ChatCompletionsExecutor()`。

## 常见问题

| 遇到的问题 | 检查项 |
| --- | --- |
| Chromium 找不到或无法启动 | 在当前虚拟环境重新运行 `python -m playwright install chromium`。Linux 缺少系统依赖时可运行 `python -m playwright install --with-deps chromium`。 |
| `No module named e2e_web` | 激活安装 SDK 时使用的虚拟环境，再运行脚本。 |
| 找不到模型配置 | 从存放 `.env` 的目录启动脚本。使用兼容接口时，确认 `E2E_MODEL_ENDPOINT` 包含完整的 `/chat/completions` 路径。 |

## 参考资料

- 浏览器和模型配置、Agent 接口、视觉、缓存、证据与错误处理。[使用参考](docs/usage.md)
- 上游能力对照和 executor 请求与响应协议。[Web 能力对照](ALIGNMENT.md)
- SDK 基于 Python Playwright 实现，并参考了上游 `@e2e-dev/web` 的浏览器与 Agent 语义。[上游 e2e 项目](https://github.com/e2e-inc/e2e)

## 运行项目测试

在已按「运行仓库示例」安装测试依赖的环境中执行。

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q
E2E_WEB_BROWSER_TESTS=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q
```

第二条命令启用 Chromium 集成测试，其中包括本页的快速开始和自然语言搜索示例。搜索示例测试使用本地模拟模型服务。
