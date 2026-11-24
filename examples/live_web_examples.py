"""Run opt-in browser and model smoke tests against public demo sites."""

import argparse
from contextlib import suppress
import json
from pathlib import Path
import tempfile
import time

from e2e_web import (
    AgentConfig, CacheConfig, E2EError, FunctionExecutor, Web, WebSession,
)

if __package__:
    from . import _example_support as support
else:
    import _example_support as support


def selenium_multiselect(session: WebSession) -> None:
    """Set the entire selection and then clear it without submitting the form."""
    agent = session.agent
    snacks = session.page.locator("#multi")
    session.expect(snacks).to_have_values(["eggs", "sausages"])
    agent.act(
        "Find the multiple-selection list containing Eggs, Ham, Sausages, and Onion gravy. "
        "Set its selection to exactly {choices}. Finish as soon as those are the only options marked selected "
        "in that list. Leave the form open.",
        {"choices": ["Ham", "Onion gravy"]},
    )
    session.expect(snacks).to_have_values(["ham", "onion gravy"])
    agent.act_once(
        "Clear the entire selection in the multiple-selection list containing Eggs, Ham, Sausages, and Onion gravy. "
        "Leave the form open."
    )
    session.expect(snacks).to_have_values([])


def todo_keyboard(session: WebSession) -> None:
    """Add, edit, and complete one task while keeping a second task active."""
    titles = {"original": "Review invoices", "edited": "Review revised invoices", "other": "Back up photos"}
    agent = session.agent
    page = session.page
    rows = page.get_by_test_id("todo-item")
    original_titles = [titles["original"], titles["other"]]

    for title in original_titles:
        agent.act(
            "If the list already shows a saved todo titled {title}, finish immediately. "
            "Otherwise, enter {title} in the new todo field and press Enter to add it once.",
            {"title": title},
        )
    session.expect(rows).to_have_text(original_titles)

    agent.act(
        "Rename the todo titled exactly {original} to {edited}. Double-click the original title, replace the text "
        "in the focused edit field, and press Enter to save. If the saved row already shows {edited}, finish immediately.",
        {"original": titles["original"], "edited": titles["edited"]},
    )
    session.expect(rows).to_have_text([titles["edited"], titles["other"]])

    edited_row = rows.filter(has=page.get_by_text(titles["edited"], exact=True))
    agent.act(
        "Mark only the todo titled {edited} complete by checking the checkbox in its row. "
        "Leave {other} unchecked. Finish when {edited} is checked and {other} is unchecked.",
        {"edited": titles["edited"], "other": titles["other"]},
    )
    session.expect(edited_row.get_by_role("checkbox")).to_be_checked()

    agent.act_once("Open the Completed filter in the todo footer.")
    agent.assert_(
        "The Completed view shows exactly one todo, titled {title}, with its checkbox checked.",
        {"title": titles["edited"]},
    )
    session.expect(page.get_by_role("link", name="Completed", exact=True)).to_have_class("selected")
    session.expect(rows).to_have_text([titles["edited"]])
    session.expect(page.get_by_test_id("todo-count")).to_have_text("1 item left")


def dynamic_controls(session: WebSession) -> None:
    """Wait for the application to enable an input before filling it."""
    agent = session.agent
    field = session.page.get_by_role("textbox")
    sample = "Public demo"
    session.expect(field).to_be_disabled()
    agent.act_once("Click Enable in the Enable/disable section to enable its textbox.")
    agent.wait_for("The textbox in the Enable/disable section is enabled and editable.", timeout=45, max_model_calls=5)
    agent.act(
        "Set the editable textbox in the Enable/disable section to {sample}. Finish when its value is {sample}.",
        {"sample": sample},
    )
    session.expect(field).to_have_value(sample)


def new_tab(session: WebSession) -> None:
    """在同一会话内切换到新标签页、提取标题并返回。"""
    opener_url = session.page.url
    agent = session.agent
    agent.act("点击一次「Click Here」链接。等新标签页出现后切换过去，并保持该标签页打开。")
    heading = agent.extract("提取当前标签页的主标题，保留网页原文。", schema={"type": "string"})
    print(f"新标签页标题：{heading}")
    if session.artifacts_dir is not None:
        session.screenshot(session.artifacts_dir / "new-tab.png")

    agent.act_once("关闭当前显示「New Window」的标签页，保留原标签页。")
    session.expect(session.page).to_have_url(opener_url)


CASES = {
    "selenium-multiselect": ("https://www.selenium.dev/selenium/web/formPage.html", selenium_multiselect),
    "todo-keyboard": ("https://demo.playwright.dev/todomvc/", todo_keyboard),
    "dynamic-controls": ("https://the-internet.herokuapp.com/dynamic_controls", dynamic_controls),
    "new-tab": ("https://the-internet.herokuapp.com/windows", new_tab),
}


def run_case(web, model, name, output):
    url, scenario = CASES[name]
    row = {"case": name, "url": url, "model_calls": 0, "actions": []}
    started = time.monotonic()

    def complete(request, *, timeout):
        row["model_calls"] += 1
        return model.complete(request, timeout=timeout)

    def record_action(event):
        row["actions"].append(event["action"])

    def record_attempt(event):
        row["last_attempted_action"] = event["action"]

    print(f"Running {name}", flush=True)
    try:
        with web.session(
            name=name, app_identity=url, executor=FunctionExecutor(complete),
            agent=AgentConfig(timeout=75, judgment_timeout=45, max_actions=10, max_model_calls=12,
                              vision=False, on_before_action=record_attempt, on_action=record_action),
            cache=CacheConfig(mode="off"), artifacts_dir=output / name,
        ) as session:
            try:
                session.page.goto(url, wait_until="domcontentloaded", timeout=30000)
                scenario(session)
            except Exception:
                with suppress(E2EError, OSError):
                    session.screenshot(output / name / "failure.png")
                raise
            session.screenshot(output / name / "after.png")
            row["final_url"] = session.page.url
        row["status"] = "passed"
    except Exception as exc:
        row.update(status="failed", code=getattr(exc, "code", type(exc).__name__), message=str(exc)[:750])
    row["seconds"] = round(time.monotonic() - started, 2)
    print(f"{name}: {row['status']} ({row['model_calls']} model calls, {row['seconds']} s)", flush=True)
    if row["status"] == "failed":
        print(f"  {row['code']}: {row['message']}", flush=True)
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", action="append", choices=CASES, help="Repeat to select multiple cases. Defaults to all.")
    support.add_model_arguments(parser)
    parser.add_argument("--output", type=Path, default=Path(".e2e/live/examples"), help="Parent directory for run evidence.")
    args = parser.parse_args()
    provider, model = support.configured_model(parser, args, max_tokens=2048)
    args.output.mkdir(parents=True, exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix="run-", dir=args.output))
    report = {"provider": provider, "model": model.model, "cases": []}
    report_path = output / "report.json"
    print(f"Evidence: {output.resolve()}", flush=True)
    with Web() as web:
        for name in dict.fromkeys(args.case or CASES):
            report["cases"].append(run_case(web, model, name, output))
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if all(case["status"] == "passed" for case in report["cases"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
