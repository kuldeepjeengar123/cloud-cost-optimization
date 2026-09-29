from __future__ import annotations

import sys

from dotenv import load_dotenv

from agent_module.agent import build_cost_agent

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main() -> None:
    load_dotenv()
    agent = build_cost_agent()
    question = "Why is EC2 spend high?"

    result = agent.invoke({"messages": [{"role": "user", "content": question}]})

    print(f"\nQuestion: {question}\n")
    print("-" * 60)
    # The last message is the final answer, printed separately below,
    # so the step-by-step trace here stops one message before it.
    for message in result["messages"][:-1]:
        role = message.__class__.__name__
        if role == "AIMessage" and getattr(message, "tool_calls", None):
            for call in message.tool_calls:
                print(f"[{role}] called tool: {call['name']}({call['args']})")
        elif role == "ToolMessage":
            print(f"[{role}] ({message.name}): {message.content}")
        elif getattr(message, "content", None):
            print(f"[{role}]: {message.content}")
    print("-" * 60)
    print(f"\nFinal answer:\n{result['messages'][-1].content}")


if __name__ == "__main__":
    main()
