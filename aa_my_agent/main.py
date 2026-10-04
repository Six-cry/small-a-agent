from .hooks.hooks import trigger_hooks
from .agent import agent_loop
from .rag.index_status import check_rag_index_status
from .config import LOG_DIR
from .telemetry import emit, start_session_log
from .mcp import initialize_mcp, shutdown_mcp

try:
    import readline

    readline.parse_and_bind("set bind-tty-special-chars off")
    readline.parse_and_bind("set input-meta on")
    readline.parse_and_bind("set output-meta on")
    readline.parse_and_bind("set convert-meta off")
except ImportError:
    pass

def main():
    start_session_log(LOG_DIR)
    mcp_status = initialize_mcp()
    emit(
        "MCP", "Startup",
        status=("enabled" if mcp_status["enabled"] else "disabled"),
        configured=mcp_status["configured_servers"],
        connected=len(mcp_status["connected_servers"]),
        pending=len(mcp_status["pending_servers"]),
        pending_sources=",".join(mcp_status["pending_servers"]) or "-",
        pending_requirements=";".join(
            f"{name}:{','.join(names)}"
            for name, names in mcp_status["pending_servers"].items()
        ) or "-",
        tools=len(mcp_status["tools"]),
        errors=len(mcp_status["errors"]),
        error_sources=",".join(mcp_status["errors"]) or "-",
    )
    rag_outcome = check_rag_index_status(
        force=True,
        announce=True,
    )
    emit(
        "RAG", "IndexCheck",
        status=rag_outcome.status,
        message=rag_outcome.message,
    )

    print("我是智能体小a")
    print("Type a question, press Enter. Type q to quit.\n")

    history = []
    try:
        while True:
            try:
                query = input("\033[36m小a >> \033[0m")
            except (EOFError, KeyboardInterrupt):
                break

            if query.strip().lower() in ("q", "exit", ""):
                break

            trigger_hooks("UserPromptSubmit", query)
            history.append({"role": "user", "content": query})
            result = agent_loop(history)

            if result.text:
                print(result.text)
            print()
    finally:
        shutdown_mcp()





if __name__ == "__main__":
    main()

    
