import json
import pathlib
import readline  # noqa: F401 — enables arrow keys and input history
import subprocess
from langchain_ollama import ChatOllama
from langchain_core.tools import tool
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

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


def stream_response(llm_with_tools, messages):
    """Stream one response turn, return the accumulated AIMessage."""
    print("Zeppeli> ", end="", flush=True)
    chunks = []
    for chunk in llm_with_tools.stream(messages):
        if chunk.content:
            print(chunk.content, end="", flush=True)
        chunks.append(chunk)
    print()
    response = chunks[0]
    for c in chunks[1:]:
        response = response + c
    return response


def run_turn(llm_with_tools, messages, user_input):
    messages.append(HumanMessage(content=user_input))
    response = stream_response(llm_with_tools, messages)
    messages.append(response)

    tools = {t.name: t for t in [list_files, glob_files, rg_search]}
    while response.tool_calls:
        for tc in response.tool_calls:
            print(f"  [tool: {tc['name']}({tc['args']})]")
            result = tools[tc["name"]].invoke(tc["args"])
            messages.append(ToolMessage(content=str(result), tool_call_id=tc["id"]))
        response = stream_response(llm_with_tools, messages)
        messages.append(response)


def main():
    llm = ChatOllama(model=MODEL)
    llm_with_tools = llm.bind_tools([list_files, glob_files, rg_search])
    messages = [SystemMessage(content=SYSTEM_PROMPT)]

    print(f"Ollama Chat ({MODEL})  — type 'quit' or Ctrl+C to exit\n")

    while True:
        try:
            user_input = input("You> ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nBye!")
            break

        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", "/exit"):
            print("Bye!")
            break

        run_turn(llm_with_tools, messages, user_input)
        print()


if __name__ == "__main__":
    main()
