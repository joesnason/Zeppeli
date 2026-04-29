import subprocess
from langchain_ollama import ChatOllama
from langchain_core.tools import tool
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

# MODEL = "gemma4:e4b-mlx-bf16"
MODEL = "gemma4:26b-nvfp4"

SYSTEM_PROMPT = """You are a helpful assistant with access to the following tool:

- list_files(path): List files and directories at a given path using ls -la.

Use this tool when the user asks about files, directories, or folder contents.
For questions unrelated to the filesystem, answer directly without using any tool."""


@tool
def list_files(path: str = ".") -> str:
    """List files and directories at the given path using ls -la."""
    result = subprocess.run(["ls", "-la", path], capture_output=True, text=True)
    return result.stdout if result.returncode == 0 else f"Error: {result.stderr}"


def run_agent(user_prompt: str) -> str:
    llm = ChatOllama(model=MODEL)
    llm_with_tools = llm.bind_tools([list_files])

    messages = [
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(content=user_prompt),
    ]
    print(f"\n{'='*50}")
    print(f"[User]: {user_prompt}")

    response = llm_with_tools.invoke(messages)

    # Stateful tool execution loop — full message history kept each round
    while response.tool_calls:
        messages.append(response)
        for tc in response.tool_calls:
            print(f"[Tool call]: {tc['name']}({tc['args']})")
            result = list_files.invoke(tc["args"])
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
    ]
    for prompt in test_prompts:
        run_agent(prompt)
