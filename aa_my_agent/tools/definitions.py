import glob, ast, json, locale, math, subprocess, time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlencode, urlparse
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError
from ..config import (
    RAG_ALLOW_ON_DEMAND_IMAGE_VERIFY,
    TAVILY_API_KEY,
    WORKDIR,
)
from ..rag.image_verifier import ImageVerificationService
from ..rag.service import get_rag_service
from .sensitive_files import (
    blocked_sensitive_message,
    sensitive_command_reason,
    sensitive_path_reason,
)

CURRENT_TODOS: list[dict] = [] # 内存中的任务列表，模型每调一次 todo_write 就整体覆盖更新
TAVILY_API_BASE = "https://api.tavily.com"

CHINA_TIMEZONE = timezone(
    timedelta(hours=8),
    "Asia/Shanghai",
)

SUPPORTED_TIMEZONES = {
    "Asia/Shanghai": CHINA_TIMEZONE,
    "UTC": timezone.utc,
}


def run_get_current_time(
    timezone_name: str = "Asia/Shanghai",
) -> str:
    """返回指定时区当前可信的日期和时间。"""
    target_timezone = SUPPORTED_TIMEZONES.get(timezone_name)

    if target_timezone is None:
        supported = ", ".join(SUPPORTED_TIMEZONES)
        return (
            f"Error: Unsupported timezone {timezone_name!r}. "
            f"Supported timezones: {supported}"
        )

    now = datetime.now(target_timezone)

    return json.dumps(
        {
            "date": now.strftime("%Y-%m-%d"),
            "time": now.strftime("%H:%M:%S"),
            "datetime": now.isoformat(timespec="seconds"),
            "year": now.year,
            "month": now.month,
            "day": now.day,
            "timezone": timezone_name,
            "utc_offset": now.strftime("%z"),
            "unix_timestamp": int(now.timestamp()),
        },
        ensure_ascii=False,
    )

def safe_path(path: str) -> Path:
    file_path = (WORKDIR / path).resolve()
    if not file_path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {path}")
    sensitive_reason = sensitive_path_reason(file_path)
    if sensitive_reason:
        raise PermissionError(
            "Access to sensitive files is blocked "
            f"({sensitive_reason})"
        )
    return file_path


def _decode_process_output(data: bytes) -> str:
    """
    解码 Shell 输出。

    Skill 脚本经常显式输出 UTF-8，而 Windows 原生命令通常使用系统
    代码页。先严格尝试 UTF-8，再尝试系统编码和 GB18030，避免使用
    errors="replace" 后把乱码永久反馈给模型。
    """
    encodings = [
        "utf-8",
        locale.getpreferredencoding(False),
        "gb18030",
    ]
    tried: set[str] = set()

    for encoding in encodings:
        normalized = encoding.casefold()
        if normalized in tried:
            continue
        tried.add(normalized)

        try:
            return data.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue

    return data.decode("utf-8", errors="replace")


def run_bash(command: str) -> str:
    sensitive_reason = sensitive_command_reason(command)
    if sensitive_reason:
        return blocked_sensitive_message(sensitive_reason)

    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if=", "> /dev/"]
    if any(pattern in command for pattern in dangerous):
        return "Error: Dangerous command blocked"

    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=WORKDIR,
            capture_output=True,
            text=False,
            timeout=120,
        )
        output = _decode_process_output(
            result.stdout + result.stderr
        ).strip()

        if result.returncode != 0:
            detail = output or "(no output)"
            return (
                f"Error: command exited with code "
                f"{result.returncode}\n{detail[:50000]}"
            )

        return output[:50000] if output else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"
    except (FileNotFoundError, OSError) as exc:
        return f"Error: {exc}"


def run_read(path: str, limit: int | None = None, offset: int = 0) -> str:
    try:
        if offset < 0:
            return "Error: Offset must be a non-negative integer"

        if limit is not None and limit <= 0:
            return "Error: Limit must be a positive integer"
        
        all_lines = safe_path(path).read_text(encoding="utf-8", errors="replace").splitlines()

        end = None if limit is None else offset + limit
        lines = all_lines[offset:end]

        remaining = len(all_lines) - (offset + len(lines))
        if remaining > 0:
            lines.append(f"... ({remaining} more lines)")

        return "\n".join(lines)
    
    except Exception as exc:
        return f"Error: {exc}"
    


def run_write(path: str, content: str) -> str:
    try:
        if len(content) > 100_000:
            return f"Error: content too large ({len(content)} bytes, max 100KB)"

        file_path = safe_path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as exc:
        return f"Error: {exc}"


def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        file_path = safe_path(path)
        text = file_path.read_text(encoding="utf-8", errors="replace")
        if old_text not in text:
            return f"Error: text not found in {path}"

        file_path.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
        return f"Edited {path}"
    except Exception as exc:
        return f"Error: {exc}"


def run_glob(pattern: str) -> str:
    try:
        sensitive_reason = sensitive_path_reason(pattern)
        if sensitive_reason:
            return blocked_sensitive_message(sensitive_reason)

        matches = []
        for match in glob.glob(pattern, root_dir=WORKDIR, recursive=True):
            resolved = (WORKDIR / match).resolve()
            if (
                resolved.is_relative_to(WORKDIR)
                and sensitive_path_reason(resolved) is None
            ):
                matches.append(match)
        return "\n".join(matches) if matches else "(no matches)"
    except Exception as exc:
        return f"Error: {exc}"


def run_calc(expression: str) -> str:
    allowed_names = {
        "abs": abs,
        "ceil": math.ceil,
        "cos": math.cos,
        "e": math.e,
        "exp": math.exp,
        "factorial": math.factorial,
        "floor": math.floor,
        "log": math.log,
        "log10": math.log10,
        "pi": math.pi,
        "pow": math.pow,
        "round": round,
        "sin": math.sin,
        "sqrt": math.sqrt,
        "tan": math.tan,
    }

    try:
        result = eval(expression, {"__builtins__": {}}, allowed_names)
        if isinstance(result, float):
            result = int(result) if result.is_integer() else round(result, 10)
        return f"= {result}"
    except ZeroDivisionError:
        return "Error: Division by zero"
    except SyntaxError as exc:
        return f"Error: Invalid expression - {exc}"
    except NameError as exc:
        return f"Error: Unknown function or variable - {exc}"
    except Exception as exc:
        return f"Error: {exc}"


def run_weather(city: str, days: int = 1, unit: str = "celsius") -> str:
    try:
        city = city.strip()
        if not city:
            return "Error: City name cannot be empty"

        days = int(days)
        if days < 1 or days > 3:
            return "Error: Days must be between 1 and 3"

        unit = unit if unit in ("celsius", "fahrenheit") else "celsius"
        query = urlencode({"format": "j1", "lang": "zh"})
        url = f"https://wttr.in/{quote(city)}?{query}"

        with urlopen(url, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8", errors="replace"))

        current = data.get("current_condition", [{}])[0]
        forecast = data.get("weather", [])
        location = data.get("nearest_area", [{}])[0]
        city_name = location.get("areaName", [{}])[0].get("value", city)
        region = location.get("region", [{}])[0].get("value", "")
        country = location.get("country", [{}])[0].get("value", "")

        temp_key = "temp_F" if unit == "fahrenheit" else "temp_C"
        max_key = "maxtempF" if unit == "fahrenheit" else "maxtempC"
        min_key = "mintempF" if unit == "fahrenheit" else "mintempC"
        unit_label = "F" if unit == "fahrenheit" else "C"

        lines = [f"Weather for {city_name}"]
        if region or country:
            lines.append(f"Location: {region}, {country}".strip(", "))

        if current:
            desc = current.get("weatherDesc", [{}])[0].get("value", "unknown")
            lines.extend(
                [
                    f"Current: {desc}",
                    f"Temperature: {current.get(temp_key, 'N/A')} {unit_label}",
                    f"Humidity: {current.get('humidity', 'N/A')}%",
                    f"Wind: {current.get('winddir16Point', 'N/A')} {current.get('windspeedKmph', 'N/A')} km/h",
                ]
            )

        for day_data in forecast[:days]:
            date = day_data.get("date", "unknown")
            lines.append(
                f"{date}: high {day_data.get(max_key, 'N/A')} {unit_label}, "
                f"low {day_data.get(min_key, 'N/A')} {unit_label}"
            )

        return "\n".join(lines)
    except TimeoutError:
        return "Error: API request timeout (10s)"
    except OSError as exc:
        return f"Error: Network request failed - {exc}"
    except json.JSONDecodeError:
        return "Error: Invalid API response"
    except Exception as exc:
        return f"Error: {exc}"

VALID_TODO_STATUSES = {"pending", "in_progress", "waiting_for_user", "completed",}
    
def _normalize_todos(todos): # 统一格式化、校验待办列表数据
    if isinstance(todos, str): # isinstance函数判断变量todos是否属于该类型
        try:
            todos = json.loads(todos) # 把符合 JSON 格式的字符串，转换成对应的 Python 原生数据类型（list或者dict）
        except json.JSONDecodeError: # 捕获异常
            try:
                todos = ast.literal_eval(todos) # 安全解析 Python 格式字面量字符串
            except (SyntaxError, ValueError):
                return None, "Error: todos must be a list or JSON array string"
    if not isinstance(todos, list):
        return None, "Error: todos must be a list"
    for i, t in enumerate(todos):
        if not isinstance(t, dict):
            return None, f"Error: todos[{i}] must be an object"
        if "content" not in t or "status" not in t:
            return None, f"Error: todos[{i}] missing 'content' or 'status'"
        if t["status"] not in VALID_TODO_STATUSES:
            return None, f"Error: todos[{i}] has invalid status '{t['status']}'"
    return todos, None

def run_todo_write(todos: list) -> str:
    global CURRENT_TODOS # 修改的是全局变量 
    todos, error = _normalize_todos(todos)
    if error:
        return error
    CURRENT_TODOS = todos
    lines = ["\n\033[33m## Current Tasks\033[0m"]
    for t in CURRENT_TODOS:
        icon = {"pending": " ",
                "in_progress": "\033[36m▸\033[0m",
                "waiting_for_user": "\033[33m?\033[0m",
                "completed": "\033[32m✓\033[0m"}[t["status"]] # 选择图标
        lines.append(f"  [{icon}] {t['content']}")
    print("\n".join(lines))
    return f"Updated {len(CURRENT_TODOS)} tasks"


def run_search_knowledge(query: str, top_k: int = 3) -> str:
    try:
        if (not isinstance(query, str) or not query.strip()):
            return "Error: query 不能为空"

        if (isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 10):
            return "Error: top_k 必须是 1 到 10 之间的整数"
        
        service = get_rag_service()

        return service.search(
            query=query.strip(),
            top_k=top_k,
        )
    
    except Exception as exc:
        return (
            "Error: 知识库检索失败 - "
            f"{type(exc).__name__}: {exc}"
        )


def run_inspect_knowledge_image(chunk_id: str, question: str) -> str:
    """围绕当前问题核验 RAG 命中的一张 PDF 原始裁图。"""
    try:
        service = ImageVerificationService(
            allow_external=RAG_ALLOW_ON_DEMAND_IMAGE_VERIFY,
        )
        return service.verify(chunk_id=chunk_id, question=question)
    except Exception as exc:
        return (
            "Error: 图片核验失败 - "
            f"{type(exc).__name__}: {exc}"
        )



def _tavily_post(
    endpoint: str,
    payload: dict,
) -> dict:
    """向Tavily发送请求，临时错误最多重试一次。"""
    if not TAVILY_API_KEY:
        raise RuntimeError(
            "TAVILY_API_KEY is not configured"
        )

    retryable_http_codes = {
        408,
        429,
        500,
        502,
        503,
        504,
        529,
    }

    raw = ""

    for attempt in range(2):
        request = Request(
            url=f"{TAVILY_API_BASE}{endpoint}",
            data=json.dumps(
                payload
            ).encode("utf-8"),
            headers={
                "Authorization": (
                    f"Bearer {TAVILY_API_KEY}"
                ),
                "Content-Type":
                    "application/json",
            },
            method="POST",
        )

        try:
            with urlopen(
                request,
                timeout=30,
            ) as response:
                raw = response.read().decode(
                    "utf-8",
                    errors="replace",
                )

            break

        except HTTPError as exc:
            if (
                exc.code
                in retryable_http_codes
                and attempt == 0
            ):
                time.sleep(1)
                continue

            raise RuntimeError(
                f"Tavily returned HTTP {exc.code}"
            ) from exc

        except URLError as exc:
            if attempt == 0:
                time.sleep(1)
                continue

            raise RuntimeError(
                "Tavily network error: "
                f"{exc.reason}"
            ) from exc

        except TimeoutError as exc:
            if attempt == 0:
                time.sleep(1)
                continue

            raise RuntimeError(
                "Tavily request timed out"
            ) from exc

    try:
        return json.loads(raw)

    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "Tavily returned invalid JSON"
        ) from exc


def run_web_search(
    query: str,
    max_results: int = 3,
    search_depth: str = "basic",
    include_domains: list[str] | None = None,
) -> str:
    """搜索互联网，返回标题、摘要和来源URL。"""
    try:
        if not isinstance(query, str) or not query.strip():
            return "Error: query cannot be empty"

        if (
            isinstance(max_results, bool)
            or not isinstance(max_results, int)
            or max_results < 1
        ):
            return (
                "Error: max_results must be a positive integer"
            )

        # 即使模型沿用旧参数请求5或10条，也直接收紧到3条，
        # 避免用一次参数错误换来额外的模型重试。
        max_results = min(max_results, 3)

        if search_depth not in {
            "basic",
            "advanced",
        }:
            return (
                "Error: search_depth must be "
                "'basic' or 'advanced'"
            )

        if include_domains is not None:
            if (
                not isinstance(include_domains, list)
                or not all(
                    isinstance(domain, str)
                    and domain.strip()
                    for domain in include_domains
                )
            ):
                return (
                    "Error: include_domains must "
                    "be a list of domain names"
                )

        payload = {
            "query": query.strip(),
            "search_depth": search_depth,
            "topic": "general",
            "max_results": max_results,
            "include_answer": False,
            "include_raw_content": False,
            "include_images": False,
        }

        if include_domains:
            payload["include_domains"] = [
                domain.strip()
                for domain in include_domains
            ]

        response = _tavily_post(
            "/search",
            payload,
        )

        results = []

        for item in response.get("results", []):
            results.append(
                {
                    "title": item.get(
                        "title",
                        "",
                    ),
                    "url": item.get(
                        "url",
                        "",
                    ),
                    "content": str(
                        item.get(
                            "content",
                            "",
                        )
                        or ""
                    )[:1200],
                    "score": item.get(
                        "score",
                    ),
                    "published_date": item.get(
                        "published_date",
                    ),
                }
            )

        if not results:
            return "No web search results found."

        return json.dumps(
            {
                "query": query.strip(),
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        )

    except Exception as exc:
        return (
            "Error: web search failed - "
            f"{type(exc).__name__}: {exc}"
        )


def run_fetch_url(url: str) -> str:
    """提取一个公开网页的正文。"""
    try:
        if not isinstance(url, str) or not url.strip():
            return "Error: url cannot be empty"

        normalized_url = url.strip()
        parsed = urlparse(normalized_url)

        if parsed.scheme not in {"http", "https"}:
            return (
                "Error: only HTTP and HTTPS "
                "URLs are allowed"
            )

        if not parsed.netloc:
            return "Error: invalid URL"

        response = _tavily_post(
            "/extract",
            {
                "urls": [normalized_url],
                "extract_depth": "basic",
                "format": "markdown",
                "include_images": False,
            },
        )

        results = response.get("results", [])

        if not results:
            failed = response.get(
                "failed_results",
                [],
            )
            return (
                "Error: no page content extracted. "
                f"Failed results: {failed}"
            )

        content = str(
            results[0].get(
                "raw_content",
                "",
            )
        )

        if not content.strip():
            return "Error: extracted page is empty"

        max_chars = 10_000

        if len(content) > max_chars:
            content = (
                content[:max_chars]
                + "\n\n... (page content truncated)"
            )

        return (
            f"Source URL: {normalized_url}\n\n"
            f"{content}"
        )

    except Exception as exc:
        return (
            "Error: URL extraction failed - "
            f"{type(exc).__name__}: {exc}"
        )
