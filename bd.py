import asyncio
import json
import typing as t
import anthropic
import asyncpg
import redis.asyncio as redis

from typing import cast, List, Dict, Any, Awaitable
from config import *
from tools import *

# --- БАЗА ДАННЫХ ---

# Типы для базы данных
DbRecord: Any = asyncpg.Record

async def stream_anthropic(user_input: str, history: list | None = None) -> Any | str | None:
    """Стриминговый диалог через Anthropic SDK (как в ant.py)"""
    try:
        client: Any = anthropic.AsyncAnthropic(
            base_url=ANTHROPIC_BASE_URL,
            api_key=ANTHROPIC_API_KEY
        )

        messages: list[Any] = history if history else []
        messages.append({"role": "user", "content": user_input})

        full_response: str = ""
        async with client.messages.stream(
            model=ANTHROPIC_MODEL,
            max_tokens=ANTHROPIC_MAX_TOKENS,
            messages=messages
        ) as stream:
            async for text in stream.text_stream:
                print(text, end="", flush=True)
                full_response += text

        print()
        return full_response

    except Exception as e:
        print(f"{C_RED}[ANTHROPIC ERROR]{C_RESET} {e}")
        return None




async def init_db() -> bool:
    """Инициализация PostgreSQL с обработкой ошибок"""
    try:
        conn: Any = await asyncpg.connect(
            user=DB_USER,
            password=DB_PASS,
            database=DB_NAME,
            host=DB_HOST,
            port=DB_PORT,
            timeout=30,
        )

        async with conn.transaction():
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    id SERIAL PRIMARY KEY,
                    name TEXT UNIQUE NOT NULL,
                    path TEXT NOT NULL,
                    goal TEXT,
                    plan TEXT,
                    doc_path TEXT,
                    final_prompt TEXT,
                    architecture TEXT,
                    status TEXT DEFAULT 'active',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """
            )

            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS project_messages (
                    id SERIAL PRIMARY KEY,
                    project_id INT REFERENCES projects(id) ON DELETE CASCADE,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """
            )

        await conn.close()
        print(f"{C_GREEN}[DB]{C_RESET} PostgreSQL готов.")
        return True
    except Exception as e:
        print(f"{C_RED}[DB ERROR]{C_RESET} {e}")
        return False


async def init_redis() -> bool:
    """Инициализация Redis с обработкой ошибок"""
    global r
    try:
        r: Any | None = await redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0, decode_responses=True, socket_timeout=10)
        if not await cast(t.Awaitable[bool], r.ping()):
            raise ConnectionError("Redis не отвечает на ping")
        print(f"{C_GREEN}[REDIS]{C_RESET} Redis готов.")
        return True
    except Exception as e:
        print(f"{C_RED}[REDIS ERROR]{C_RESET} {e}")
        r: None = None
        return False


async def init_ollama() -> bool:
    """Инициализация Ollama-клиента"""
    global client
    try:
        client: Any = AsyncClient(host=OLLAMA_HOST, timeout=OLLAMA_TIMEOUT)
        if client is None:
            raise ConnectionError("Failed to create Ollama client")
        await client.list()
        print(f"{C_GREEN}[OLLAMA]{C_RESET} Ollama готов ({OLLAMA_MODEL}).")
        return True
    except Exception as e:
        print(f"{C_RED}[OLLAMA ERROR]{C_RESET} {e}")
        client = None
        return False


async def create_project(name: str, path: str, goal: str = "") -> bool:
    """Создание нового проекта"""
    if not await init_db():
        return False

    conn: Any = await asyncpg.connect(user=DB_USER, password=DB_PASS, database=DB_NAME, host=DB_HOST, port=DB_PORT)
    try:
        await conn.execute("INSERT INTO projects (name, path, goal) VALUES ($1, $2, $3)", name, path, goal)
        print(f"{C_GREEN}✅{C_RESET} Проект '{name}' создан.")
        return True
    except asyncpg.UniqueViolationError:
        print(f"{C_RED}❌{C_RESET} Проект уже существует.")
        return False
    finally:
        await conn.close()


async def get_all_projects() -> List[Any]:
    """Получение списка всех проектов"""
    conn: Any = await asyncpg.connect(user=DB_USER, password=DB_PASS, database=DB_NAME, host=DB_HOST, port=DB_PORT)
    try:
        rows: Any = await conn.fetch("SELECT name, status, goal FROM projects ORDER BY created_at DESC")
        return rows
    finally:
        await conn.close()


async def load_project(name: str) -> bool:
    """Загрузка проекта в активную сессию"""
    global ACTIVE_PROJECT
    conn: Any = await asyncpg.connect(user=DB_USER, password=DB_PASS, database=DB_NAME, host=DB_HOST, port=DB_PORT)
    try:
        row: Any = await conn.fetchrow("SELECT * FROM projects WHERE name = $1", name)
        if row:
            ACTIVE_PROJECT: dict[Any, Any] = dict(row)
            print(f"{C_GREEN}🚀{C_RESET} Загружен: '{ACTIVE_PROJECT['name']}' ({ACTIVE_PROJECT['status']})")
            await sync_db_to_redis(project_id=ACTIVE_PROJECT["id"])
            return True
        else:
            print(f"{C_RED}❌{C_RESET} Проект не найден.")
            return False
    finally:
        await conn.close()


async def delete_project(name: str) -> bool:
    """Удаление проекта из базы данных"""
    conn: Any = await asyncpg.connect(user=DB_USER, password=DB_PASS, database=DB_NAME, host=DB_HOST, port=DB_PORT)
    try:
        row: Any = await conn.fetchrow("SELECT id FROM projects WHERE name = $1", name)
        if not row:
            print(f"{C_RED}❌{C_RESET} Проект '{name}' не найден.")
            return False

        await conn.execute("DELETE FROM projects WHERE name = $1", name)
        print(f"{C_GREEN}✅{C_RESET} Проект '{name}' удален из базы данных.")
        return True
    finally:
        await conn.close()


async def sync_db_to_redis(project_id: int) -> None:
    """Загружает историю из БД в Redis"""
    if not r:
        pass

    key: str = f"{REDIS_CHAT_KEY_PREFIX}{project_id}"
    conn: Any = await asyncpg.connect(user=DB_USER, password=DB_PASS, database=DB_NAME, host=DB_HOST, port=DB_PORT)
    try:
        rows: Any = await conn.fetch(
            "SELECT role, content FROM project_messages WHERE project_id = $1 ORDER BY id DESC LIMIT $2",
            project_id,
            MAX_DB_HISTORY,
        )
        if rows:
            rows = list(rows)
            rows.reverse()
            messages: list(str) = [json.dumps(obj={"role": row["role"], "content": row["content"]}) for row in rows]
            async with r.pipeline() as pipe:
                pipe.delete(key)
                if messages:
                    pipe.rpush(key, *messages)
                await pipe.execute()
            print(f"{C_GRAY}📜{C_RESET} Загружено {len(messages)} сообщений из истории.")
    finally:
        await conn.close()


async def sync_redis_to_db(project_id: int) -> None:
    """Сохраняет новые сообщения из Redis в PostgreSQL"""
    if not r:
        pass

    key: str = f"{REDIS_CHAT_KEY_PREFIX}{project_id}"
    length: int = await cast(t.Awaitable[int], r.llen(key))
    if length == 0:
        return

    messages_json: list[str] = await cast(t.Awaitable[List[str]], r.lrange(key, -50, -1))
    conn = await asyncpg.connect(user=DB_USER, password=DB_PASS, database=DB_NAME, host=DB_HOST, port=DB_PORT)
    try:
        async with conn.transaction():
            await conn.execute("DELETE FROM project_messages WHERE project_id = $1", project_id)
            for msg_json in messages_json:
                msg = json.loads(msg_json)
                await conn.execute(
                    "INSERT INTO project_messages (project_id, role, content) VALUES ($1, $2, $3)",
                    project_id,
                    msg["role"],
                    msg["content"],
                )
        print(f"{C_GRAY}💾{C_RESET} Сохранено {len(messages_json)} сообщений в БД.")
    finally:
        await conn.close()


async def update_project_fields(fields: Dict[str, Any]) -> bool:
    """Обновляет поля активного проекта в БД"""
    if not ACTIVE_PROJECT or not ACTIVE_PROJECT.get("id"):
        return False

    project_id = ACTIVE_PROJECT["id"]
    conn: Any = await asyncpg.connect(user=DB_USER, password=DB_PASS, database=DB_NAME, host=DB_HOST, port=DB_PORT)
    try:
        set_clause: str = ", ".join([f"{k} = ${i+2}" for i, k in enumerate(fields.keys())])
        values: list[Any] = [project_id] + list(fields.values())

        await conn.execute(
            f"UPDATE projects SET {set_clause} WHERE id = $1",
            *values,
        )

        for k, v in fields.items():
            ACTIVE_PROJECT[k] = v

        return True
    finally:
        await conn.close()

# --- MAIN AGENT LOOP ---

async def agent_loop(user_input: str, mode: str = "dev") -> None:
    """Основной цикл агента с поддержкой инструментов"""
    global ACTIVE_PROJECT, r, client

    if not ACTIVE_PROJECT or not r or not client:
        print(f"{C_RED}[ERROR]{C_RESET} Система не инициализирована.")

    project_id = ACTIVE_PROJECT["id"]
    redis_key: str = f"{REDIS_CHAT_KEY_PREFIX}{project_id}"

    match mode:
        case "analyzer":
            sys_prompt = SYSTEM_PROMPT_ANALYZER
            tools: list[dict[Any, str]] = tools_definition_analyzer
        case "review":
            sys_prompt = SYSTEM_PROMPT_REVIEW
            tools: list[dict[Any, str]] = tools_definition_analyzer
        case "explain":
            sys_prompt = SYSTEM_PROMPT_EXPLAIN
            tools: list[dict[Any, str]] = tools_definition_analyzer
        case "dialog_web":
            sys_prompt = SYSTEM_PROMPT_DIALOG_WEB
            tools: list[dict[Any, str]] = tools_definition_dialog_web
        case _:
            sys_prompt = SYSTEM_PROMPT_DEV
            tools: list[dict[Any, str]] = tools_definition_dev

    messages: list[dict[str, str]]= [{"role": "system", "content": sys_prompt}]

    project_context: list[Any] = []
    project_context.append(f"Проект: {ACTIVE_PROJECT['name']}")
    project_context.append(f"Путь: {ACTIVE_PROJECT['path']}")

    if ACTIVE_PROJECT.get("final_prompt"):
        project_context.append(f"Цель: {ACTIVE_PROJECT['final_prompt']}")
    if ACTIVE_PROJECT.get("architecture"):
        project_context.append(f"Архитектура: {ACTIVE_PROJECT['architecture']}")
    if ACTIVE_PROJECT.get("plan"):
        project_context.append(f"План:\n{ACTIVE_PROJECT['plan']}")

    if project_context:
        messages.append({"role": "system", "content": "\n".join(project_context)})

    history_len: int = await cast(t.Awaitable[int], r.llen(redis_key))
    if history_len == 0 or "посмотр" in user_input.lower() or "проанализируй" in user_input.lower():
        print(f"{C_GRAY}[SYSTEM]{C_RESET} Сканирование файлов проекта...")
        scan_result: str = await scan_directory_tool()
        if scan_result and not scan_result.startswith("Ошибка"):
            messages.append(
                {
                    "role": "system",
                    "content": f"АВТОМАТИЧЕСКИЙ СКАН ПРОЕКТА:\n{scan_result}",
                }
            )

    redis_msgs: list[str] = await cast(t.Awaitable[List[str]], r.lrange(redis_key, 0, -1))
    if redis_msgs:
        try:
            history: list[Any] = [json.loads(s=m) for m in redis_msgs[-MAX_DB_HISTORY:]]
            messages.extend(history)
        except json.JSONDecodeError:
            print(f"{C_YELLOW}[WARN]{C_RESET} Ошибка чтения истории Redis.")

    await cast(t.Awaitable[int], r.rpush(redis_key, json.dumps(obj={"role": "user", "content": user_input})))
    messages.append({"role": "user", "content": user_input})

    for iteration in range(MAX_ITERATIONS):
        try:
            response: ChatResponse = await client.chat(
                model=OLLAMA_MODEL,
                messages=messages,
                tools=tools,
                options=OLLAMA_OPTIONS
            )
        except asyncio.TimeoutError:
            print(f"{C_RED}[ERROR]{C_RESET} Ошибка: Ollama не ответил за {OLLAMA_TIMEOUT} секунд.")
            print(f"{C_GRAY}Совет: Увеличьте OLLAMA_TIMEOUT в конфигурации или используйте меньшую модель.{C_RESET}")
            break
        except Exception as e:
            print(f"{C_RED}[ERROR]{C_RESET} Ошибка Ollama: {type(e).__name__}: {e}")
            break

        msg: Any = response["message"]

        if msg.get("tool_calls"):
            try:
                msg_dict = msg.model_dump() if hasattr(msg, "model_dump") else dict(msg)
            except:
                msg_dict: dict[Any, Any] = dict(msg)

            await cast(t.Awaitable[int], r.rpush(redis_key, json.dumps(obj=msg_dict)))
            messages.append(msg_dict)

            for tool in msg.get("tool_calls"):
                fn = tool.get("function", {})
                name = fn.get("name")
                args = fn.get("arguments", {}) or {}
                res = ""
                tool_id = tool.get("id") or f"{name}_{hash(str(args))}" or "unknown"

                match name:
                    case "write_file":
                        path = args.get("path")
                        content = args.get("content")
                        if not isinstance(path, str) or not isinstance(content, str):
                            res = f"{C_RED}Ошибка: {name} требует 'path' и 'content' строки{C_RESET}"
                        else:
                            print(f"{C_CYAN}[WRITE]{C_RESET} 📝 {path}")
                            res = await write_file_tool(path, content)
                    case "read_file":
                        path = args.get("path")
                        if not isinstance(path, str):
                            res = f"{C_RED}Ошибка: {name} требует 'path' строку{C_RESET}"
                        else:
                            print(f"{C_CYAN}[READ]{C_RESET} 📄 {path}")
                            res = await read_file_tool(path)
                    case "search_code":
                        query = args.get("query")
                        if not isinstance(query, str):
                            res = f"{C_RED}Ошибка: {name} требует 'query' строку{C_RESET}"
                        else:
                            print(f"{C_CYAN}[SEARCH]{C_RESET} 🔎 {query}")
                            res = await search_code_tool(query)
                    case "search_docs":
                        query = args.get("query")
                        if not isinstance(query, str):
                            res = f"{C_RED}Ошибка: {name} требует 'query' строку{C_RESET}"
                        else:
                            print(f"{C_CYAN}[DOCS]{C_RESET} 📚 {query}")
                            res = await search_docs_tool(query)
                    case "run_shell_command":
                        command = args.get("command")
                        if not isinstance(command, str):
                            res = f"{C_RED}Ошибка: {name} требует 'command' строку{C_RESET}"
                        else:
                            print(f"{C_CYAN}[SHELL]{C_RESET} 💻 {command}")
                            res = await run_shell_tool(command)
                    case "scan_directory":
                        print(f"{C_CYAN}[SCAN]{C_RESET} 🔍 Папка проекта")
                        res = await scan_directory_tool()
                    case "web_search":
                        query = args.get("query")
                        if not isinstance(query, str):
                            res = f"{C_RED}Ошибка: {name} требует 'query' строку{C_RESET}"
                        else:
                            print(f"{C_CYAN}[WEB]{C_RESET} 🔍 {query}")
                            res = await web_search_tool(query)
                    case "update_project_plan":
                        plan = args.get("plan")
                        if not isinstance(plan, str):
                            res = f"{C_RED}Ошибка: {name} требует 'plan' строку{C_RESET}"
                        else:
                            print(f"{C_GREEN}[PLAN]{C_RESET} Обновление плана...")
                            if await update_project_fields({"plan": plan}):
                                res = "План обновлен."
                            else:
                                res = "Ошибка обновления плана."
                    case "get_project_info":
                        res = str(ACTIVE_PROJECT) if ACTIVE_PROJECT else "Нет проекта."
                    case _:
                        res = f"Неизвестный инструмент: {name}"

                tool_result = {
                    "role": "tool",
                    "content": res,
                    "tool_call_id": tool_id,
                    "name": name,
                }
                await cast(t.Awaitable[int], r.rpush(redis_key, json.dumps(tool_result)))
                messages.append(tool_result)

            continue

        if msg.get("content"):
            text = msg["content"]
            print(f"{C_GREEN}🤖 [{mode.upper()}]:{C_RESET} {text}")
            await cast(t.Awaitable[int], r.rpush(redis_key, json.dumps({"role": "assistant", "content": text})))
            break

        if iteration == MAX_ITERATIONS - 1:
            print(f"{C_YELLOW}[WARN]{C_RESET} Достигнут лимит итераций.")

    await sync_redis_to_db(project_id)
