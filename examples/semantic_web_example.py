"""用中文指令修改配送设置，再滚动页面验证结果。"""

import argparse

from e2e_web import AgentConfig, CacheConfig, Web

if __package__:
    from . import _example_support as support
else:
    import _example_support as support


PAGE = """
<!doctype html>
<html lang="zh-CN">
<meta charset="utf-8">
<h1>配送总览</h1>
<p id="summary">配送区域：华北</p>
<button onclick="document.querySelector('dialog').showModal()">配送设置</button>

<dialog aria-label="配送设置">
  <form method="dialog">
    <label>配送区域
      <select onchange="document.querySelector('#summary').textContent = '配送区域：' + this.value">
        <option>华北</option>
        <option>华南</option>
      </select>
    </label>
    <button>完成</button>
  </form>
</dialog>

<footer style="margin-top: 110vh">配送总览结束</footer>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    support.add_model_arguments(parser)
    args = parser.parse_args()
    _, model = support.configured_model(parser, args)
    with Web() as web, web.session(
        name="delivery-region", app_identity="delivery-demo", executor=model,
        agent=AgentConfig(timeout=90, judgment_timeout=60),
        cache=CacheConfig(mode="off"),
    ) as session:
        session.page.set_content(PAGE)
        session.agent.act("打开「配送设置」，将「配送区域」改为「华南」，然后点击「完成」。")
        session.agent.assert_(
            "确认页面顶部显示「配送区域：华南」，然后向下滚动，确认页脚显示「配送总览结束」。",
            explore=True,
        )
    print("配送区域已改为华南，页面验证通过。")


if __name__ == "__main__":
    main()
