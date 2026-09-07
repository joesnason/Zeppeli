"""Automated tests for cloud-model configuration (--base-url/--model/
--api-key and their LITELLM_* env-var fallbacks) plus core.agent.load_llm()'s
backend-branching logic, core.agent.get_context_window()'s modelinfo
key-matching/error-handling, and core.agent.model_supports_reasoning()'s
capabilities-list check/error-handling. No Ollama/network dependency — safe
to run anytime, exits non-zero on failure. Style matches
test_permission_modes.py.
"""

import sys
import types

import pytest

import cli
import core.agent as agent

_ENV_VARS = ("LITELLM_BASE_URL", "LITELLM_MODEL", "LITELLM_API_KEY")


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)


# --- cli.py: _resolve_config / _resolve_* ---------------------------------

def test_resolve_config_base_url_without_model_raises_systemexit_2():
    args = cli._parse_args(["--base-url", "http://x"])
    try:
        cli._resolve_config(args)
        assert False, "expected SystemExit"
    except SystemExit as e:
        assert e.code == 2


def test_resolve_config_base_url_with_model_flag_ok():
    args = cli._parse_args(["--base-url", "http://x", "--model", "openai/gpt-4o-mini"])
    model, base_url, api_key = cli._resolve_config(args)
    assert model == "openai/gpt-4o-mini"
    assert base_url == "http://x"
    assert api_key is None


def test_resolve_config_model_standalone_without_base_url_ok():
    args = cli._parse_args(["--model", "gemma4:e2b"])
    model, base_url, api_key = cli._resolve_config(args)
    assert model == "gemma4:e2b"
    assert base_url is None


def test_resolve_config_base_url_env_var_fallback(monkeypatch):
    monkeypatch.setenv("LITELLM_BASE_URL", "http://envhost")
    args = cli._parse_args(["--model", "openai/env-model"])
    model, base_url, api_key = cli._resolve_config(args)
    assert base_url == "http://envhost"
    assert model == "openai/env-model"


def test_resolve_config_model_env_var_fallback(monkeypatch):
    monkeypatch.setenv("LITELLM_MODEL", "openai/env-model")
    args = cli._parse_args(["--base-url", "http://x"])
    model, base_url, api_key = cli._resolve_config(args)
    assert model == "openai/env-model"
    assert base_url == "http://x"


def test_resolve_config_flag_overrides_env(monkeypatch):
    monkeypatch.setenv("LITELLM_BASE_URL", "http://envhost")
    monkeypatch.setenv("LITELLM_MODEL", "openai/env-model")
    args = cli._parse_args(["--base-url", "http://flaghost", "--model", "openai/flag-model"])
    model, base_url, api_key = cli._resolve_config(args)
    assert base_url == "http://flaghost"
    assert model == "openai/flag-model"


def test_resolve_api_key_prefers_cli_flag(monkeypatch):
    monkeypatch.setenv("LITELLM_API_KEY", "envkey")
    args = cli._parse_args(["--api-key", "clikey"])
    assert cli._resolve_api_key(args) == "clikey"


def test_resolve_api_key_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("LITELLM_API_KEY", "envkey")
    args = cli._parse_args([])
    assert cli._resolve_api_key(args) == "envkey"


def test_resolve_api_key_returns_none_when_neither_set():
    args = cli._parse_args([])
    assert cli._resolve_api_key(args) is None


# --- core/agent.py: load_llm() branching -----------------------------------

def test_load_llm_ollama_branch_default_unchanged(monkeypatch):
    calls = []

    class _SpyChatOllama:
        def __init__(self, model):
            calls.append(model)

        def bind_tools(self, tools):
            return self

    monkeypatch.setattr(agent, "ChatOllama", _SpyChatOllama)
    agent.load_llm()
    assert calls == [agent.MODEL]


def test_load_llm_cloud_branch_builds_expected_kwargs(monkeypatch):
    received = {}

    class _SpyChatLiteLLM:
        def __init__(self, **kwargs):
            received.update(kwargs)

        def bind_tools(self, tools):
            return self

    fake_module = types.ModuleType("langchain_litellm")
    fake_module.ChatLiteLLM = _SpyChatLiteLLM
    monkeypatch.setitem(sys.modules, "langchain_litellm", fake_module)
    agent.load_llm(model="openai/gpt-4o-mini", base_url="http://x", api_key="k")
    assert received == {"model": "openai/gpt-4o-mini", "api_base": "http://x", "api_key": "k"}


def test_load_llm_cloud_branch_omits_api_key_when_absent(monkeypatch):
    received = {}

    class _SpyChatLiteLLM:
        def __init__(self, **kwargs):
            received.update(kwargs)

        def bind_tools(self, tools):
            return self

    fake_module = types.ModuleType("langchain_litellm")
    fake_module.ChatLiteLLM = _SpyChatLiteLLM
    monkeypatch.setitem(sys.modules, "langchain_litellm", fake_module)
    agent.load_llm(model="openai/gpt-4o-mini", base_url="http://x")
    assert "api_key" not in received


# --- core/agent.py: get_context_window() -----------------------------------

def test_get_context_window_reads_family_prefixed_key(monkeypatch):
    class _FakeShowResponse:
        modelinfo = {"general.parameter_count": 8e9, "llama.context_length": 131072}

    fake_module = types.ModuleType("ollama")
    fake_module.show = lambda model: _FakeShowResponse()
    monkeypatch.setitem(sys.modules, "ollama", fake_module)
    assert agent.get_context_window("gemma4:26b-nvfp4") == 131072


def test_get_context_window_defaults_model_to_MODEL_constant(monkeypatch):
    seen = {}

    class _FakeShowResponse:
        modelinfo = {"gemma3.context_length": 8192}

    def _spy_show(model):
        seen["model"] = model
        return _FakeShowResponse()

    fake_module = types.ModuleType("ollama")
    fake_module.show = _spy_show
    monkeypatch.setitem(sys.modules, "ollama", fake_module)
    agent.get_context_window()
    assert seen["model"] == agent.MODEL


def test_get_context_window_returns_none_when_no_context_length_key(monkeypatch):
    class _FakeShowResponse:
        modelinfo = {"general.parameter_count": 8e9}

    fake_module = types.ModuleType("ollama")
    fake_module.show = lambda model: _FakeShowResponse()
    monkeypatch.setitem(sys.modules, "ollama", fake_module)
    assert agent.get_context_window("x") is None


def test_get_context_window_returns_none_when_modelinfo_absent(monkeypatch):
    class _FakeShowResponse:
        modelinfo = None

    fake_module = types.ModuleType("ollama")
    fake_module.show = lambda model: _FakeShowResponse()
    monkeypatch.setitem(sys.modules, "ollama", fake_module)
    assert agent.get_context_window("x") is None


def test_get_context_window_returns_none_on_exception(monkeypatch):
    def _raising_show(model):
        raise ConnectionError("no route to host")

    fake_module = types.ModuleType("ollama")
    fake_module.show = _raising_show
    monkeypatch.setitem(sys.modules, "ollama", fake_module)
    assert agent.get_context_window("x") is None


# --- core/agent.py: model_supports_reasoning() ------------------------------

def test_model_supports_reasoning_true_when_thinking_in_capabilities(monkeypatch):
    class _FakeShowResponse:
        capabilities = ["completion", "tools", "thinking"]

    fake_module = types.ModuleType("ollama")
    fake_module.show = lambda model: _FakeShowResponse()
    monkeypatch.setitem(sys.modules, "ollama", fake_module)
    assert agent.model_supports_reasoning("gemma4:26b-nvfp4") is True


def test_model_supports_reasoning_false_when_thinking_absent(monkeypatch):
    class _FakeShowResponse:
        capabilities = ["completion", "tools"]

    fake_module = types.ModuleType("ollama")
    fake_module.show = lambda model: _FakeShowResponse()
    monkeypatch.setitem(sys.modules, "ollama", fake_module)
    assert agent.model_supports_reasoning("llama3.1:8b") is False


def test_model_supports_reasoning_false_when_capabilities_absent(monkeypatch):
    class _FakeShowResponse:
        capabilities = None

    fake_module = types.ModuleType("ollama")
    fake_module.show = lambda model: _FakeShowResponse()
    monkeypatch.setitem(sys.modules, "ollama", fake_module)
    assert agent.model_supports_reasoning("x") is False


def test_model_supports_reasoning_defaults_model_to_MODEL_constant(monkeypatch):
    seen = {}

    class _FakeShowResponse:
        capabilities = ["thinking"]

    def _spy_show(model):
        seen["model"] = model
        return _FakeShowResponse()

    fake_module = types.ModuleType("ollama")
    fake_module.show = _spy_show
    monkeypatch.setitem(sys.modules, "ollama", fake_module)
    agent.model_supports_reasoning()
    assert seen["model"] == agent.MODEL


def test_model_supports_reasoning_returns_false_on_exception(monkeypatch):
    def _raising_show(model):
        raise ConnectionError("no route to host")

    fake_module = types.ModuleType("ollama")
    fake_module.show = _raising_show
    monkeypatch.setitem(sys.modules, "ollama", fake_module)
    assert agent.model_supports_reasoning("x") is False
