import readline  # noqa: F401 — enables arrow keys and input history
import subprocess
from langchain_ollama import ChatOllama
from langchain_core.tools import tool
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

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

    while response.tool_calls:
        for tc in response.tool_calls:
            print(f"  [tool: {tc['name']}({tc['args']})]")
            result = list_files.invoke(tc["args"])
            messages.append(ToolMessage(content=str(result), tool_call_id=tc["id"]))
        response = stream_response(llm_with_tools, messages)
        messages.append(response)


def main():
    llm = ChatOllama(model=MODEL)
    llm_with_tools = llm.bind_tools([list_files])
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
