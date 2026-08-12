"""Batch runner for testing tool call behavior. No PRE_TOOL_HOOKS equivalent —
write_file and delete_file execute unguarded here. Don't add prompts to
test_prompts that would trigger them without checking the effect on the local
filesystem first.
"""

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from core import SYSTEM_PROMPT, TOOLS_BY_NAME, load_llm


def run_agent(user_prompt: str) -> str:
    llm_with_tools = load_llm()

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
            result = TOOLS_BY_NAME[tc["name"]].invoke(tc["args"])
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
        "Read the file README.md",                        # 應該用 read_file tool
        "Show me the first 10 lines of cli.py",           # 應該用 read_file tool，limit=10
        "Read lines 5 to 15 of cli.py",                  # 應該用 read_file tool，offset=5, limit=10
    ]
    for prompt in test_prompts:
        run_agent(prompt)
