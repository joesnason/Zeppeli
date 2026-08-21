"""Automated tests for cloud-model configuration (--base-url/--model/
--api-key and their LITELLM_* env-var fallbacks) plus core.agent.load_llm()'s
backend-branching logic, core.agent.get_context_window()'s modelinfo
key-matching/error-handling, and core.agent.model_supports_reasoning()'s
capabilities-list check/error-handling. No Ollama/network dependency — safe
to run anytime, exits non-zero on failure. Style matches
test_permission_modes.py.
"""

import os
import sys
import types

import cli
import core.agent as agent

_ENV_VARS = ("LITELLM_BASE_URL", "LITELLM_MODEL", "LITELLM_API_KEY")


def _clear_env():
    for var in _ENV_VARS:
        os.environ.pop(var, None)


# --- cli.py: _resolve_config / _resolve_* ---------------------------------

def test_resolve_config_base_url_without_model_raises_systemexit_2():
    _clear_env()
    args = cli._parse_args(["--base-url", "http://x"])
    try:
        cli._resolve_config(args)
        assert False, "expected SystemExit"
    except SystemExit as e:
        assert e.code == 2


def test_resolve_config_base_url_with_model_flag_ok():
    _clear_env()
    args = cli._parse_args(["--base-url", "http://x", "--model", "openai/gpt-4o-mini"])
    model, base_url, api_key = cli._resolve_config(args)
    assert model == "openai/gpt-4o-mini"
    assert base_url == "http://x"
    assert api_key is None


def test_resolve_config_model_standalone_without_base_url_ok():
    _clear_env()
    args = cli._parse_args(["--model", "gemma4:e2b"])
    model, base_url, api_key = cli._resolve_config(args)
    assert model == "gemma4:e2b"
    assert base_url is None


def test_resolve_config_base_url_env_var_fallback():
    _clear_env()
    os.environ["LITELLM_BASE_URL"] = "http://envhost"
    try:
        args = cli._parse_args(["--model", "openai/env-model"])
        model, base_url, api_key = cli._resolve_config(args)
        assert base_url == "http://envhost"
        assert model == "openai/env-model"
    finally:
        _clear_env()


def test_resolve_config_model_env_var_fallback():
    _clear_env()
    os.environ["LITELLM_MODEL"] = "openai/env-model"
    try:
        args = cli._parse_args(["--base-url", "http://x"])
        model, base_url, api_key = cli._resolve_config(args)
        assert model == "openai/env-model"
        assert base_url == "http://x"
    finally:
        _clear_env()


def test_resolve_config_flag_overrides_env():
    _clear_env()
    os.environ["LITELLM_BASE_URL"] = "http://envhost"
    os.environ["LITELLM_MODEL"] = "openai/env-model"
    try:
        args = cli._parse_args(["--base-url", "http://flaghost", "--model", "openai/flag-model"])
        model, base_url, api_key = cli._resolve_config(args)
        assert base_url == "http://flaghost"
        assert model == "openai/flag-model"
    finally:
        _clear_env()


def test_resolve_api_key_prefers_cli_flag():
    _clear_env()
    os.environ["LITELLM_API_KEY"] = "envkey"
    try:
        args = cli._parse_args(["--api-key", "clikey"])
        assert cli._resolve_api_key(args) == "clikey"
    finally:
        _clear_env()


def test_resolve_api_key_falls_back_to_env():
    _clear_env()
    os.environ["LITELLM_API_KEY"] = "envkey"
    try:
        args = cli._parse_args([])
        assert cli._resolve_api_key(args) == "envkey"
    finally:
        _clear_env()


def test_resolve_api_key_returns_none_when_neither_set():
    _clear_env()
    args = cli._parse_args([])
    assert cli._resolve_api_key(args) is None


# --- core/agent.py: load_llm() branching -----------------------------------

def test_load_llm_ollama_branch_default_unchanged():
    calls = []

    class _SpyChatOllama:
        def __init__(self, model):
            calls.append(model)

        def bind_tools(self, tools):
            return self

    original = agent.ChatOllama
    agent.ChatOllama = _SpyChatOllama
    try:
        agent.load_llm()
        assert calls == [agent.MODEL]
    finally:
        agent.ChatOllama = original


def test_load_llm_cloud_branch_builds_expected_kwargs():
    received = {}

    class _SpyChatLiteLLM:
        def __init__(self, **kwargs):
            received.update(kwargs)

        def bind_tools(self, tools):
            return self

    fake_module = types.ModuleType("langchain_litellm")
    fake_module.ChatLiteLLM = _SpyChatLiteLLM
    sys.modules["langchain_litellm"] = fake_module
    try:
        agent.load_llm(model="openai/gpt-4o-mini", base_url="http://x", api_key="k")
        assert received == {"model": "openai/gpt-4o-mini", "api_base": "http://x", "api_key": "k"}
    finally:
        del sys.modules["langchain_litellm"]


def test_load_llm_cloud_branch_omits_api_key_when_absent():
    received = {}

    class _SpyChatLiteLLM:
        def __init__(self, **kwargs):
            received.update(kwargs)

        def bind_tools(self, tools):
            return self

    fake_module = types.ModuleType("langchain_litellm")
    fake_module.ChatLiteLLM = _SpyChatLiteLLM
    sys.modules["langchain_litellm"] = fake_module
    try:
        agent.load_llm(model="openai/gpt-4o-mini", base_url="http://x")
        assert "api_key" not in received
    finally:
        del sys.modules["langchain_litellm"]


# --- core/agent.py: get_context_window() -----------------------------------

def test_get_context_window_reads_family_prefixed_key():
    class _FakeShowResponse:
        modelinfo = {"general.parameter_count": 8e9, "llama.context_length": 131072}

    fake_module = types.ModuleType("ollama")
    fake_module.show = lambda model: _FakeShowResponse()
    sys.modules["ollama"] = fake_module
    try:
        assert agent.get_context_window("gemma4:26b-nvfp4") == 131072
    finally:
        del sys.modules["ollama"]


def test_get_context_window_defaults_model_to_MODEL_constant():
    seen = {}

    class _FakeShowResponse:
        modelinfo = {"gemma3.context_length": 8192}

    def _spy_show(model):
        seen["model"] = model
        return _FakeShowResponse()

    fake_module = types.ModuleType("ollama")
    fake_module.show = _spy_show
    sys.modules["ollama"] = fake_module
    try:
        agent.get_context_window()
        assert seen["model"] == agent.MODEL
    finally:
        del sys.modules["ollama"]


def test_get_context_window_returns_none_when_no_context_length_key():
    class _FakeShowResponse:
        modelinfo = {"general.parameter_count": 8e9}

    fake_module = types.ModuleType("ollama")
    fake_module.show = lambda model: _FakeShowResponse()
    sys.modules["ollama"] = fake_module
    try:
        assert agent.get_context_window("x") is None
    finally:
        del sys.modules["ollama"]


def test_get_context_window_returns_none_when_modelinfo_absent():
    class _FakeShowResponse:
        modelinfo = None

    fake_module = types.ModuleType("ollama")
    fake_module.show = lambda model: _FakeShowResponse()
    sys.modules["ollama"] = fake_module
    try:
        assert agent.get_context_window("x") is None
    finally:
        del sys.modules["ollama"]


def test_get_context_window_returns_none_on_exception():
    def _raising_show(model):
        raise ConnectionError("no route to host")

    fake_module = types.ModuleType("ollama")
    fake_module.show = _raising_show
    sys.modules["ollama"] = fake_module
    try:
        assert agent.get_context_window("x") is None
    finally:
        del sys.modules["ollama"]


# --- core/agent.py: model_supports_reasoning() ------------------------------

def test_model_supports_reasoning_true_when_thinking_in_capabilities():
    class _FakeShowResponse:
        capabilities = ["completion", "tools", "thinking"]

    fake_module = types.ModuleType("ollama")
    fake_module.show = lambda model: _FakeShowResponse()
    sys.modules["ollama"] = fake_module
    try:
        assert agent.model_supports_reasoning("gemma4:26b-nvfp4") is True
    finally:
        del sys.modules["ollama"]


def test_model_supports_reasoning_false_when_thinking_absent():
    class _FakeShowResponse:
        capabilities = ["completion", "tools"]

    fake_module = types.ModuleType("ollama")
    fake_module.show = lambda model: _FakeShowResponse()
    sys.modules["ollama"] = fake_module
    try:
        assert agent.model_supports_reasoning("llama3.1:8b") is False
    finally:
        del sys.modules["ollama"]


def test_model_supports_reasoning_false_when_capabilities_absent():
    class _FakeShowResponse:
        capabilities = None

    fake_module = types.ModuleType("ollama")
    fake_module.show = lambda model: _FakeShowResponse()
    sys.modules["ollama"] = fake_module
    try:
        assert agent.model_supports_reasoning("x") is False
    finally:
        del sys.modules["ollama"]


def test_model_supports_reasoning_defaults_model_to_MODEL_constant():
    seen = {}

    class _FakeShowResponse:
        capabilities = ["thinking"]

    def _spy_show(model):
        seen["model"] = model
        return _FakeShowResponse()

    fake_module = types.ModuleType("ollama")
    fake_module.show = _spy_show
    sys.modules["ollama"] = fake_module
    try:
        agent.model_supports_reasoning()
        assert seen["model"] == agent.MODEL
    finally:
        del sys.modules["ollama"]


def test_model_supports_reasoning_returns_false_on_exception():
    def _raising_show(model):
        raise ConnectionError("no route to host")

    fake_module = types.ModuleType("ollama")
    fake_module.show = _raising_show
    sys.modules["ollama"] = fake_module
    try:
        assert agent.model_supports_reasoning("x") is False
    finally:
        del sys.modules["ollama"]


TESTS = [
    test_resolve_config_base_url_without_model_raises_systemexit_2,
    test_resolve_config_base_url_with_model_flag_ok,
    test_resolve_config_model_standalone_without_base_url_ok,
    test_resolve_config_base_url_env_var_fallback,
    test_resolve_config_model_env_var_fallback,
    test_resolve_config_flag_overrides_env,
    test_resolve_api_key_prefers_cli_flag,
    test_resolve_api_key_falls_back_to_env,
    test_resolve_api_key_returns_none_when_neither_set,
    test_load_llm_ollama_branch_default_unchanged,
    test_load_llm_cloud_branch_builds_expected_kwargs,
    test_load_llm_cloud_branch_omits_api_key_when_absent,
    test_get_context_window_reads_family_prefixed_key,
    test_get_context_window_defaults_model_to_MODEL_constant,
    test_get_context_window_returns_none_when_no_context_length_key,
    test_get_context_window_returns_none_when_modelinfo_absent,
    test_get_context_window_returns_none_on_exception,
    test_model_supports_reasoning_true_when_thinking_in_capabilities,
    test_model_supports_reasoning_false_when_thinking_absent,
    test_model_supports_reasoning_false_when_capabilities_absent,
    test_model_supports_reasoning_defaults_model_to_MODEL_constant,
    test_model_supports_reasoning_returns_false_on_exception,
]


if __name__ == "__main__":
    failures = []
    for t in TESTS:
        _clear_env()
        try:
            t()
            print(f"[PASS] {t.__name__}")
        except Exception as e:
            print(f"[FAIL] {t.__name__}: {e}")
            failures.append(t.__name__)
        finally:
            _clear_env()

    print(f"\n{len(TESTS) - len(failures)}/{len(TESTS)} passed")
    sys.exit(1 if failures else 0)
