from .definitions import *
from ..subagents.runner import spawn_subagent
from ..skills.registry import load_skill
from .schemas import (WEB_SEARCH_TOOL, FETCH_URL_TOOL)
from ..mcp import (
    get_mcp_handler,
    get_mcp_permission,
    get_mcp_tools,
    is_mcp_tool,
)

TOOLS = [
    {
        "name": "bash",
        "description": "Run a non-interactive command using the Windows command shell.",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": (
            "Read file contents. Use offset and limit to read "
            "large files in multiple chunks."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file to read."},
                "offset": {"type": "integer", "minimum": 0, "description": (
                    "Number of lines to skip before reading. "
                    "Defaults to 0."
                ),},
                "limit": {"type": "integer", "minimum": 1, "description": (
                    "Maximum number of lines to return. "
                    "Omit to read all remaining lines."
                ),},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "write_file",
        "description": "Write content to a file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": "Replace exact text in a file once.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
            },
            "required": ["path", "old_text", "new_text"],
        },
    },
    {
        "name": "glob",
        "description": "Find files matching a glob pattern.",
        "input_schema": {
            "type": "object",
            "properties": {"pattern": {"type": "string"}},
            "required": ["pattern"],
        },
    },
    {
        "name": "calculate",
        "description": "Calculate a mathematical expression safely.",
        "input_schema": {
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "Mathematical expression to calculate.",
                }
            },
            "required": ["expression"],
        },
    },
    {
        "name": "get_current_time",
        "description": (
            "Return the authoritative current date and time. "
            "Use when the request involves current or relative time, "
            "including 今天、现在、今年、明年、明天、下个月、最近. "
            "Use this instead of bash for date or time."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "timezone_name": {
                    "type": "string",
                    "description": "Timezone whose current time is required.",
                    "enum": ["Asia/Shanghai", "UTC"],
                    "default": "Asia/Shanghai",
                }
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "query_weather",
        "description": "Query weather information for a city.",
        "input_schema": {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "City name."},
                "days": {"type": "integer", "description": "Forecast days, 1 to 3."},
                "unit": {
                    "type": "string",
                    "description": "Temperature unit.",
                    "enum": ["celsius", "fahrenheit"],
                    "default": "celsius",
                },
            },
            "required": ["city"],
        },
    },
    {
     "name": "todo_write", 
     "description": "Create and manage a task list for your current coding session.",
     "input_schema": {"type": "object", 
                      "properties": {"todos": {"type": "array", 
                                               "items": {"type": "object", 
                                                         "properties": {"content": {"type": "string"}, 
                                                                        "status": {"type": "string", 
                                                                                   "enum": ["pending", "in_progress", "waiting_for_user", "completed"]}
                                                                        }, 
                                                         "required": ["content", "status"]
                                                         }
                                              }
                                    }, 
                                    "required": ["todos"]
                      }
     },
    {
        "name": "search_knowledge",
        "description": (
        "Search the local RAG knowledge base built from PDF, TXT, "
        "and DOCX documents. Use this tool before answering questions "
        "that may depend on information in the local knowledge base, "
        "or whenever the user explicitly asks to search local documents. "
        "Treat the returned passages as reference evidence. Cite the "
        "source filename and page when using them. If the retrieved "
        "passages are insufficient, clearly say so instead of inventing "
        "an answer."
    ),
        "input_schema": {"type": "object",
                         "properties": {
                             "query": {
                                 "type": "string",
                                 "description": (
                                     "A focused semantic search query describing "
                                     "the information that needs to be found."
                                    ),
                             },
                             "top_k": {
                                 "type": "integer",
                                 "description": "Number of relevant text chunks to return.",
                                 "minimum": 1,
                                 "maximum": 10,
                                 "default": 3,
                             },
                         },
                         "required": ["query"],
        }
    },
    {
        "name": "inspect_knowledge_image",
        "description": (
            "Inspect the original cropped PDF image for one image_summary "
            "Chunk returned by search_knowledge. Use only when the question "
            "requires exact visual relationships such as nodes, edges, arrows, "
            "numbers, or containment. The result is machine-generated and is "
            "not written back to the knowledge base."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "chunk_id": {
                    "type": "string",
                    "description": "The image parent Chunk ID returned by search_knowledge.",
                },
                "question": {
                    "type": "string",
                    "description": "A focused question answerable from that single image.",
                    "maxLength": 1000,
                },
            },
            "required": ["chunk_id", "question"],
            "additionalProperties": False,
        },
    },
    {
        "name": "subagent_task",
        "description": (
            "Delegate an isolated subtask to a subagent. "
            "You MUST use this tool before exploration when a task "
            "requires reading 3 or more files, exploring a directory "
            "or subsystem, comparing multiple modules, or performing "
            "architecture analysis. Use mode='research' for read-only "
            "exploration. Use mode='workspace' when a Skill workflow "
            "requires the subagent to create test outputs or other files. "
            "The result includes real token and duration metrics. "
            "The parent agent must not duplicate delegated file reading."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "instructions": {
                    "type": "string",
                    "description": (
                        "A complete and self-contained instructions "
                        "of the subtask."
                    ),
                },
                "mode": {
                    "type": "string",
                    "enum": ["research", "workspace"],
                    "default": "research",
                    "description": (
                        "Use research for read-only analysis. Use workspace "
                        "only when the subtask must create or edit files."
                    ),
                },
            },
            "required": ["instructions"],
            "additionalProperties": False,
        },
    },
    {
        "name": "load_skill",
        "description": (
            "Load the full instructions for an available skill. "
            "Use the exact skill name shown in the system prompt. "
            "Call this before performing a task that clearly "
            "matches a skill."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Exact name of the skill to load.",
                }
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    
    WEB_SEARCH_TOOL,
    FETCH_URL_TOOL,
    

]


READ_ONLY_TOOLS = {
    "read_file",
    "glob",
    "calculate",
    "get_current_time",
    "query_weather",
    "todo_write",
    "search_knowledge",
    "inspect_knowledge_image",
    "subagent_task",
    "load_skill",
    "web_search",
    "fetch_url",
}
WRITE_TOOLS = {"write_file", "edit_file"}

TOOL_HANDLERS = {
    "bash": run_bash,
    "read_file": run_read,
    "write_file": run_write,
    "edit_file": run_edit,
    "glob": run_glob,
    "calculate": run_calc,
    "get_current_time": run_get_current_time,
    "query_weather": run_weather,
    "todo_write": run_todo_write,
    "search_knowledge": run_search_knowledge,
    "inspect_knowledge_image": run_inspect_knowledge_image,
    "subagent_task": spawn_subagent,
    "load_skill": load_skill,
    "web_search": run_web_search,
    "fetch_url": run_fetch_url,
}


def get_active_tools() -> list[dict]:
    """Return built-ins plus tools discovered from configured MCP servers."""
    return [*TOOLS, *get_mcp_tools()]


def get_tool_handler(name: str):
    """Resolve a built-in or MCP tool without exposing MCP connection controls."""
    return TOOL_HANDLERS.get(name) or get_mcp_handler(name)


def is_known_tool(name: str) -> bool:
    return name in TOOL_HANDLERS or is_mcp_tool(name)


def get_tool_permission(name: str) -> str | None:
    """Return read/write for MCP tools; built-ins keep their existing policy."""
    return get_mcp_permission(name) if name.startswith("mcp__") else None
