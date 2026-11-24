"""Configuration and a local form shared by the runnable examples."""

import argparse
from dataclasses import dataclass
import os
from pathlib import Path

from e2e_web import ChatCompletionsExecutor, DeepSeekExecutor, E2EError, load_env


PROVIDERS = ("deepseek", "compatible")

DEMO_PAGE = """
<!doctype html>
<html lang="en">
<meta charset="utf-8">
<h1>Profile settings</h1>
<label>Name <input id="name"></label>
<button type="button" onclick="document.querySelector('output').textContent =
  'Saved: ' + document.querySelector('#name').value">Save</button>
<output>Ready</output>
</html>
"""


def add_model_arguments(parser: argparse.ArgumentParser, *, selectable: bool = True) -> None:
    parser.add_argument("--env-file", type=Path, help="Fill unset environment variables from this file.")
    if selectable:
        parser.add_argument("--provider", choices=PROVIDERS, help="Override E2E_EXAMPLE_PROVIDER.")


def load_example_env(parser: argparse.ArgumentParser, path: Path | None) -> None:
    if path is not None:
        if not path.is_file():
            parser.error(f"environment file not found: {path}")
        load_env(path)


def model_provider(default: str, override: str | None = None) -> str:
    provider = override or os.getenv("E2E_EXAMPLE_PROVIDER") or default
    if provider not in PROVIDERS:
        raise ValueError("E2E_EXAMPLE_PROVIDER must be deepseek or compatible")
    return provider


def create_model(provider: str, *, max_tokens: int = 4096) -> ChatCompletionsExecutor:
    if provider == "deepseek":
        return DeepSeekExecutor(
            model=os.getenv("DEEPSEEK_MODEL") or "deepseek-flash",
            request_options={"max_tokens": max_tokens},
        )
    return ChatCompletionsExecutor(timeout=60)


def configured_model(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
    *,
    default: str = "deepseek",
    max_tokens: int = 4096,
) -> tuple[str, ChatCompletionsExecutor]:
    load_example_env(parser, args.env_file)
    try:
        provider = model_provider(default, args.provider) if hasattr(args, "provider") else default
        return provider, create_model(provider, max_tokens=max_tokens)
    except (ValueError, E2EError) as exc:
        parser.error(str(exc))


@dataclass(frozen=True)
class ExampleTask:
    url: str | None
    instruction: str
    expected: str

    def open(self, page) -> None:
        if self.url:
            page.goto(self.url, wait_until="domcontentloaded")
        else:
            page.set_content(DEMO_PAGE)


def resolve_task(
    url: str | None = None, task: str | None = None, expected: str | None = None,
) -> ExampleTask:
    values = {
        key: (value if value is not None else os.getenv(key, "")).strip()
        for key, value in (("APP_URL", url), ("TASK", task), ("EXPECTED", expected))
    }
    if not any(values.values()):
        return ExampleTask(
            None,
            "Set Name to Ada and click Save. Finish when Saved: Ada is shown.",
            "The form displays the status Saved: Ada.",
        )
    missing = [key for key, value in values.items() if not value]
    if missing:
        raise ValueError("A custom task requires APP_URL, TASK and EXPECTED. Missing " + ", ".join(missing))
    return ExampleTask(values["APP_URL"], values["TASK"], values["EXPECTED"])
