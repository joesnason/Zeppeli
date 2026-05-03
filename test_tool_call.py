import json
import pathlib
import subprocess
from langchain_ollama import ChatOllama
from langchain_core.tools import tool
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

# MODEL = "gemma4:e4b-mlx-bf16"
MODEL = "gemma4:26b-nvfp4"
RG_BIN = str(pathlib.Path(__file__).parent / "bin" / "rg")

SYSTEM_PROMPT = """You are a helpful assistant with access to the following tools:

- list_files(path): List files and directories at a given path using ls -la.
- glob_files(pattern, cwd): Find files matching a glob pattern (supports ** for recursive search). Default cwd is ".".
- rg_search(pattern, path, glob): Search file contents using ripgrep (regex supported). Use glob to filter by filename (e.g. "*.py"). Default path is ".".

Use these tools when the user asks about files, directories, folder contents, or searching within files.
For questions unrelated to the filesystem, answer directly without using any tool."""


@tool
def list_files(path: str = ".") -> str:
    """List files and directories at the given path using ls -la."""
    result = subprocess.run(["ls", "-la", path], capture_output=True, text=True)
    return result.stdout if result.returncode == 0 else f"Error: {result.stderr}"


@tool
def glob_files(pattern: str, cwd: str = ".") -> str:
    """Find files matching a glob pattern using Node.js fs.glob. Supports ** for recursive matching."""
    script = f"""
const {{ glob }} = require('node:fs/promises');
(async () => {{
  const results = [];
  for await (const f of glob({json.dumps(pattern)}, {{ cwd: {json.dumps(cwd)} }})) results.push(f);
  console.log(results.join('\\n') || '(no matches)');
}})().catch(e => {{ process.stderr.write(e.message + '\\n'); process.exit(1); }});
"""
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    if result.returncode != 0:
        return f"Error: {result.stderr.strip()}"
    return result.stdout.strip()


@tool
def rg_search(pattern: str, path: str = ".", glob: str = "") -> str:
    """Search file contents using ripgrep. Supports regex. Use glob to filter by filename (e.g. '*.py')."""
    cmd = [RG_BIN, "--no-heading", "--color=never", pattern, path]
    if glob:
        cmd += ["--glob", glob]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 2:
        return f"Error: {result.stderr.strip()}"
    return result.stdout.strip() or "(no matches)"


def run_agent(user_prompt: str) -> str:
    llm = ChatOllama(model=MODEL)
    llm_with_tools = llm.bind_tools([list_files, glob_files, rg_search])

    messages = [
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(content=user_prompt),
    ]
    print(f"\n{'='*50}")
    print(f"[User]: {user_prompt}")

    response = llm_with_tools.invoke(messages)

    # Stateful tool execution loop — full message history kept each round
    tools = {t.name: t for t in [list_files, glob_files, rg_search]}
    while response.tool_calls:
        messages.append(response)
        for tc in response.tool_calls:
            print(f"[Tool call]: {tc['name']}({tc['args']})")
            result = tools[tc["name"]].invoke(tc["args"])
            print(f"[Tool result]:\n{result}")
            messages.append(ToolMessage(content=str(result), tool_call_id=tc["id"]))
        response = llm_with_tools.invoke(messages)

    messages.append(response)
    used_tool = any(isinstance(m, ToolMessage) for m in messages)
    print(f"[Used tool]: {used_tool}")
    print(f"[Answer]: {response.content}")
    return response.content


if __name__ == "__main__":
    test_prompts = [
        "現在目錄下有哪些檔案？",           # 應該用 tool
        "幫我列出 /tmp 裡面的內容",          # 應該用 tool
        "今天天氣怎麼樣？",                  # 不應該用 tool
        "What is 2 + 2?",                   # 不應該用 tool
        "Show me what's in the /tmp folder", # 應該用 tool
        "Find all Python files in the current directory", # 應該用 glob tool
        "Search for the word 'tool' in all Python files", # 應該用 rg tool
    ]
    for prompt in test_prompts:
        run_agent(prompt)
