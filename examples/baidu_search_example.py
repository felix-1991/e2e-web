"""用 AI 操作百度，打开第二页的第一条结果并输出新标签页标题。"""

import argparse
import os

from e2e_web import AgentConfig, CacheConfig, ObservationConfig, Web

if __package__:
    from . import _example_support as support
else:
    import _example_support as support


BAIDU_URL = "https://www.baidu.com/"
DEFAULT_QUERY = "macos 常用软件"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    support.add_model_arguments(parser)
    parser.add_argument("--query", help=f"搜索词，默认使用 BAIDU_QUERY 或「{DEFAULT_QUERY}」。")
    args = parser.parse_args()
    _, executor = support.configured_model(parser, args, default="compatible")
    query = (args.query if args.query is not None else os.getenv("BAIDU_QUERY") or DEFAULT_QUERY).strip()
    if not query:
        parser.error("--query / BAIDU_QUERY must contain a search term")

    with Web() as web:
        with web.session(
            name="baidu-ai-search",
            app_identity=BAIDU_URL,
            executor=executor,
            agent=AgentConfig(
                timeout=120,
                judgment_timeout=60,
                max_actions=16,
                max_model_calls=24,
                context="操作中遇到百度安全验证时，报告阻塞。",
            ),
            cache=CacheConfig(mode="off"),
            # 给结果列表下方的分页入口留出观察空间。
            observation=ObservationConfig(max_nodes=450, max_bytes=160_000),
        ) as session:
            page = session.page
            page.goto(BAIDU_URL, wait_until="domcontentloaded")

            session.agent.act(
                "使用百度网页搜索框搜索 {query}。",
                {"query": query},
            )
            session.agent.wait_for(
                "百度已显示查询 {query} 的网页搜索结果列表。",
                {"query": query},
            )

            session.agent.act_once("点击搜索结果分页区的「下一页」。")
            session.agent.wait_for("百度分页显示当前在第 2 页，且该页的搜索结果列表已经显示。")

            # 监听独立浏览器上下文里的新标签页，兼容普通链接和页面弹窗。
            with page.context.expect_page(timeout=30_000) as new_tab:
                session.agent.act_once(
                    "用 ControlOrMeta 修饰键点击第二页第一条搜索结果的标题链接，在新标签页打开它。",
                    timeout=60,
                )

            result_page = new_tab.value
            result_page.wait_for_load_state("domcontentloaded")
            print(result_page.title())


if __name__ == "__main__":
    main()
