"""Run a local demo or a custom web task with DeepSeek."""

import argparse

from e2e_web import AgentConfig, CacheConfig, Web

if __package__:
    from . import _example_support as support
else:
    import _example_support as support


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    support.add_model_arguments(parser, selectable=False)
    parser.add_argument("--url", help="Page URL (APP_URL). Defaults to the local demo.")
    parser.add_argument("--task", help="Action to perform on a custom page (TASK).")
    parser.add_argument("--expected", help="Result to verify on a custom page (EXPECTED).")
    args = parser.parse_args()
    _, model = support.configured_model(parser, args)
    try:
        task = support.resolve_task(args.url, args.task, args.expected)
    except ValueError as exc:
        parser.error(str(exc))

    with Web() as web, web.session(
        name="deepseek-task",
        app_identity=task.url or "example-profile",
        executor=model,
        agent=AgentConfig(timeout=120, judgment_timeout=60, max_actions=20, max_model_calls=25),
        cache=CacheConfig(mode="off"),
    ) as session:
        task.open(session.page)
        session.agent.act(task.instruction)
        session.agent.assert_(task.expected)
    print("Task completed and verified.")


if __name__ == "__main__":
    main()
