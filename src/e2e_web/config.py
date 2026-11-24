"""Typed SDK defaults and loading of local environment configuration."""

from os import PathLike
from pathlib import Path
from typing import Annotated, Any, Self

from dotenv import load_dotenv
from pydantic import BeforeValidator, Field, ValidationError
from pydantic_core import PydanticUseDefault
from pydantic_settings import BaseSettings, SettingsConfigDict

from .errors import E2EError
from .types import ObservationConfig


def _strip_boolean(value: Any) -> Any:
    if isinstance(value, str):
        value = value.strip()
        if not value:
            raise PydanticUseDefault()
    return value


_BooleanSetting = Annotated[bool, BeforeValidator(_strip_boolean)]
_NonEmptyString = Annotated[str, Field(min_length=1)]


class _Settings(BaseSettings):
    model_config = SettingsConfigDict(case_sensitive=True, hide_input_in_errors=True)

    @classmethod
    def resolve(cls, **overrides: Any) -> Self:
        # Pass aliases to the initializer so explicit values override the same env keys.
        explicit = {
            cls.model_fields[name].alias or name: value
            for name, value in overrides.items()
            if value is not None
        }
        try:
            return cls(**explicit)
        except ValidationError as exc:
            issues = [f"{error['loc'][0]} ({error['msg']})" for error in exc.errors(include_input=False)]
            raise E2EError("INVALID_ARGUMENT", "Invalid SDK configuration for " + ", ".join(issues)) from None


class _BrowserSettings(_Settings):
    headless: _BooleanSetting = Field(True, alias="E2E_HEADLESS")
    executable_path: str | None = Field(None, alias="E2E_CHROMIUM_EXECUTABLE")


class _AgentSettings(_Settings):
    vision: _BooleanSetting = Field(False, alias="E2E_VISION")


class _ObservationSettings(_Settings):
    max_nodes: int = Field(ObservationConfig.max_nodes, alias="E2E_OBSERVATION_MAX_NODES")
    max_bytes: int = Field(ObservationConfig.max_bytes, alias="E2E_OBSERVATION_MAX_BYTES")


class _ChatCompletionsSettings(_Settings):
    endpoint: _NonEmptyString = Field(alias="E2E_MODEL_ENDPOINT")
    model: _NonEmptyString = Field(alias="E2E_MODEL")
    api_key: str = Field("", alias="E2E_MODEL_API_KEY", repr=False)


class _DeepSeekSettings(_Settings):
    model: _NonEmptyString = Field(alias="DEEPSEEK_MODEL")
    api_key: _NonEmptyString = Field(alias="DEEPSEEK_API_KEY", repr=False)
    base_url: _NonEmptyString = Field("https://api.deepseek.com", alias="DEEPSEEK_BASE_URL")
    supports_vision: _BooleanSetting = Field(False, alias="DEEPSEEK_SUPPORTS_VISION")


def load_env(path: str | PathLike[str] | None = None, *, override: bool = False) -> bool:
    """Load a dotenv file into os.environ, preserving existing values by default.

    The default is .env in the current working directory; parent directories are
    never searched. Missing files return False. SDK import loads the default
    file automatically; call this to load a custom UTF-8 file explicitly.
    """
    return load_dotenv(
        dotenv_path=Path.cwd() / ".env" if path is None else path,
        override=override,
        encoding="utf-8-sig",
    )
