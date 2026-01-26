import json
import asyncio
import os
import re
import difflib
import shlex
import typing as t
from typing import cast, Any, Dict, List, Optional

# Асинхронные библиотеки
import asyncpg
import aiohttp
import aiofiles
from ollama import AsyncClient, ChatResponse
import redis.asyncio as redis

# Синхронные библиотеки
from bs4 import BeautifulSoup
from duckduckgo_search import DDGS

# --- КОНФИГУРАЦИЯ (с загрузкой из env) ---
DB_NAME = os.getenv("DB_NAME", "ai_projects")
DB_USER = os.getenv("DB_USER", "ai_agent")
DB_PASS = os.getenv("DB_PASS", "password")
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")

OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "glm-4.7-flash:q8_0")
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")

REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_DIALOG_KEY = "global_dialog:web" # Ключ для глобальных диалогов

MAX_DIALOG_HISTORY = 20 # Храним последние 20 сообщений для контекста
MAX_ITERATIONS = int(os.getenv("MAX_ITERATIONS", "14"))
REDIS_CHAT_KEY_PREFIX = "project_chat:"
MAX_DB_HISTORY = int(os.getenv("MAX_DB_HISTORY", "50"))

# --- ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ---
ACTIVE_PROJECT: Optional[Dict[str, Any]] = None
r: Optional[redis.Redis] = None
client: Optional[AsyncClient] = None

# --- СИСТЕМНЫЕ ПРОМПТЫ ---

SYSTEM_PROMPT_DEV = """Ты — Senior AI Architect и DevOps Engineer. Твоя задача — разрабатывать проекты.
В ТВОЕМ КОНТЕКСТЕ УЖЕ ЕСТЬ СОДЕРЖИМОЕ ВСЕХ ФАЙЛОВ ПРОЕКТА (ниже).

ТЫ ОБЯЗАН ДЕЙСТВОВАТЬ ПО АЛГОРИТМУ:
1. **ИЗУЧЕНИЕ:** Сначала внимательно прочитай код, предоставленный ниже.
2. **ПЛАНИРОВАНИЕ:** Если задача новая — создай детальный план. Если исправление ошибок — используй `update_project_plan`.
3. **ДОКУМЕНТАЦИЯ:** Используй `search_docs` для поиска в каталоге документации. Адаптируй знания в зависимости от текущей задачи.
4. **ПОИСК ИНФОРМАЦИИ:** Перед модификацией кода используй `web_search` для проверки библиотек.
5. **КОДИРОВАНИЕ:** Создавай файлы в рамках ПЛАНА. НЕ ПЕРЕПИСЫВАЙ весь файл без причины.
6. **ОШИБКИ (DEBUG):** Если ошибка — проанализируй, исправь код и попробуй снова.
7. **ОБНОВЛЕНИЕ ПЛАНА (КРИТИЧНО):** После выполнения шага плана ВЫЗОВИ `update_project_plan` и пометь пункт как `[ВЫПОЛНЕНО]`.
8. **ТЕСТИРОВАНИЕ:** После кода всегда запускай проверки.

ВАЖНО:
*   Sandbox: ЗАПРЕЩЕНО читать/писать файлы за пределами директории проекта.
*   Факты: Всегда используй `web_search` для версий и новых библиотек.
"""

SYSTEM_PROMPT_ANALYZER = """Ты — Senior Researcher. Ты изучаешь документацию и код.
Твоя задача: Подготовить архитектуру и промпт.
Используй `search_docs` для поиска в каталоге документации. Адаптируй знания в зависимости от цели проекта.
ТЫ НЕ ПИШЕШЬ КОД, пока не перейдут в режим /dev.
"""

SYSTEM_PROMPT_REVIEW = """Ты — Code Reviewer. Критикуй код, не пиши новый."""

SYSTEM_PROMPT_EXPLAIN = """Ты — Educator. Объясняй код построчно."""

SYSTEM_PROMPT_DIALOG_WEB = """Ты — полезный AI-ассистент. Ты можешь вести естественный диалог и использовать веб-поиск для получения актуальной информации.

Правила:
1. Если пользователь спрашивает о текущих событиях, новостях, версиях ПО или любых фактах, которые могли устареть — ОБЯЗАТЕЛЬНО используй `web_search`.
2. Если пользователь спрашивает о коде проекта — используй `search_code`.
3. Для общих вопросов о проекте — используй `get_project_info`.
4. Будь кратким и точным в ответах.
5. Если пользователь пишет "выход", "exit", "стоп" — просто напиши "Диалог завершен." и больше не отвечай.

Инструменты:
- `web_search` для получения актуальной информации из интернета"""


# --- ИНСТРУМЕНТЫ (TOOLS) ---

tools_definition_dev = [
    {
        "type": "function",
        "function": {
            "name": "get_project_info",
            "description": "Инфо о проекте",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_project_plan",
            "description": "Обновить план. Используй это, чтобы отметить выполненные пункты как [ВЫПОЛНЕНО] или [DONE].",
            "parameters": {"type": "object", "properties": {"plan": {"type": "string"}}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Записать файл",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Читать файл",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_code",
            "description": "Поиск в коде",
            "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_docs",
            "description": "Поиск в каталоге документации",
            "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_shell_command",
            "description": "Консоль",
            "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scan_directory",
            "description": "Сканирует ВСЕ файлы проекта (.rs, .py, .tomл) и возвращает их код.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Поиск в интернете",
            "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        },
    },
]

tools_definition_analyzer = [
    {
        "type": "function",
        "function": {
            "name": "get_project_info",
            "description": "Инфо о проекте",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_docs",
            "description": "Поиск в каталоге документации",
            "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scan_directory",
            "description": "Сканирует файлы проекта",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Поиск в интернете",
            "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        },
    },
]

tools_definition_dialog_web = [
        {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "Поиск в интернете для получения актуальной информации",
                "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
            },
        },
    ]


# --- БАЗА ДАННЫХ ---

async def init_db():
    """Инициализация PostgreSQL с обработкой ошибок"""
    try:
        conn = await asyncpg.connect(
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
                    doc_path TEXT,  -- Теперь это путь к КАТАЛОГУ документов
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

async def init_redis():
    """Инициализация Redis с обработкой ошибок"""
    global r
    try:
        r = await redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0, decode_responses=True, socket_timeout=10)
        if not await cast(t.Awaitable[bool], r.ping()):
            raise ConnectionError("Redis не отвечает на ping")
        print(f"{C_GREEN}[REDIS]{C_RESET} Redis готов.")
        return True
    except Exception as e:
        print(f"{C_RED}[REDIS ERROR]{C_RESET} {e}")
        r = None
        return False

async def init_ollama():
    """Инициализация Ollama-клиента"""
    global client
    try:
        client = AsyncClient(host=OLLAMA_HOST, timeout=300)
        # Проверяем подключение
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

    conn = await asyncpg.connect(user=DB_USER, password=DB_PASS, database=DB_NAME, host=DB_HOST, port=DB_PORT)
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
    conn = await asyncpg.connect(user=DB_USER, password=DB_PASS, database=DB_NAME, host=DB_HOST, port=DB_PORT)
    try:
        rows = await conn.fetch("SELECT name, status, goal FROM projects ORDER BY created_at DESC")
        return rows
    finally:
        await conn.close()

async def load_project(name: str) -> bool:
    """Загрузка проекта в активную сессию"""
    global ACTIVE_PROJECT
    conn = await asyncpg.connect(user=DB_USER, password=DB_PASS, database=DB_NAME, host=DB_HOST, port=DB_PORT)
    try:
        row = await conn.fetchrow("SELECT * FROM projects WHERE name = $1", name)
        if row:
            ACTIVE_PROJECT = dict(row)
            print(f"{C_GREEN}🚀{C_RESET} Загружен: '{ACTIVE_PROJECT['name']}' ({ACTIVE_PROJECT['status']})")
            await sync_db_to_redis(ACTIVE_PROJECT["id"])
            return True
        else:
            print(f"{C_RED}❌{C_RESET} Проект не найден.")
            return False
    finally:
        await conn.close()

async def delete_project(name: str) -> bool:
    """Удаление проекта из базы данных"""
    conn = await asyncpg.connect(user=DB_USER, password=DB_PASS, database=DB_NAME, host=DB_HOST, port=DB_PORT)
    try:
        # Проверяем существование проекта
        row = await conn.fetchrow("SELECT id FROM projects WHERE name = $1", name)
        if not row:
            print(f"{C_RED}❌{C_RESET} Проект '{name}' не найден.")
            return False

        # Удаляем проект
        await conn.execute("DELETE FROM projects WHERE name = $1", name)
        print(f"{C_GREEN}✅{C_RESET} Проект '{name}' удален из базы данных.")
        return True
    finally:
        await conn.close()

async def sync_db_to_redis(project_id: int):
    """Загружает историю из БД в Redis"""
    if not r:
        return

    key = f"{REDIS_CHAT_KEY_PREFIX}{project_id}"
    conn = await asyncpg.connect(user=DB_USER, password=DB_PASS, database=DB_NAME, host=DB_HOST, port=DB_PORT)
    try:
        rows = await conn.fetch(
            "SELECT role, content FROM project_messages WHERE project_id = $1 ORDER BY id DESC LIMIT $2",
            project_id,
            MAX_DB_HISTORY,
        )
        if rows:
            rows = list(rows)
            rows.reverse()
            messages = [json.dumps({"role": row["role"], "content": row["content"]}) for row in rows]
            async with r.pipeline() as pipe:
                pipe.delete(key)
                if messages:
                    pipe.rpush(key, *messages)
                await pipe.execute()
            print(f"{C_GRAY}📜{C_RESET} Загружено {len(messages)} сообщений из истории.")
    finally:
        await conn.close()

async def sync_redis_to_db(project_id: int):
    """Сохраняет новые сообщения из Redis в PostgreSQL"""
    if not r:
        return

    key = f"{REDIS_CHAT_KEY_PREFIX}{project_id}"
    length = await cast(t.Awaitable[int], r.llen(key))
    if length == 0:
        return

    messages_json = await cast(t.Awaitable[List[str]], r.lrange(key, -50, -1))
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
    conn = await asyncpg.connect(user=DB_USER, password=DB_PASS, database=DB_NAME, host=DB_HOST, port=DB_PORT)
    try:
        set_clause = ", ".join([f"{k} = ${i+2}" for i, k in enumerate(fields.keys())])
        values = [project_id] + list(fields.values())

        await conn.execute(
            f"UPDATE projects SET {set_clause} WHERE id = $1",
            *values,
        )

        for k, v in fields.items():
            ACTIVE_PROJECT[k] = v

        return True
    finally:
        await conn.close()

# --- ИНСТРУМЕНТЫ АГЕНТА ---

def get_full_path(rel_path: str) -> str:
    """Безопасное получение абсолютного пути в рамках проекта"""
    if not ACTIVE_PROJECT or not ACTIVE_PROJECT.get("path"):
        raise PermissionError("Нет активного проекта.")

    base_path = os.path.abspath(ACTIVE_PROJECT["path"])
    rel_path = rel_path.strip()

    if os.path.isabs(rel_path):
        target_path = os.path.abspath(rel_path)
    else:
        target_path = os.path.abspath(os.path.join(base_path, rel_path))

    if not target_path.startswith(base_path):
        raise PermissionError(f"Выход за пределы проекта: {rel_path}")

    return target_path

async def scan_directory_tool() -> str:
    """Сканирует все важные файлы проекта"""
    if not ACTIVE_PROJECT:
        return "Нет проекта."

    base_path = os.path.abspath(ACTIVE_PROJECT["path"])
    print(f"{C_GRAY}[SCAN]{C_RESET} Сканирую {base_path}...")

    cmd = (
        f"find {shlex.quote(base_path)} -type f "
        r'\( -name "*.rs" -o -name "*.py" -o -name "*.toml" -o -name "*.txt" -o -name "*.yaml" -o -name "*.yml" -o -name "*.md" \) '
        r'-not -path "*/target/*" -not -path "*/node_modules/*" -not -path "*/.venv/*" -not -path "*/__pycache__/*" -not -path "*/.git/*"'
    )

    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30.0)

        if stderr and b"Permission denied" in stderr:
            print(f"{C_YELLOW}[WARN]{C_RESET} Нет доступа к некоторым файлам.")

        file_paths = [f.strip() for f in stdout.decode().splitlines()]

        if not file_paths:
            return "Файлов не найдено."

        print(f"{C_GRAY}[SCAN]{C_RESET} Найдено {len(file_paths)} файлов.")

        combined_text = "--- СОДЕРЖИМОЕ ПРОЕКТА ---\n"

        for f_path in file_paths:
            relative_name = ""  # Инициализация для избежания
            try:
                relative_name = os.path.relpath(f_path, base_path)
                async with aiofiles.open(f_path, "r", encoding="utf-8", errors="replace") as f:
                    content = await f.read()
                    preview = content[:5000] + "... (обрезано)" if len(content) > 5000 else content
                    combined_text += f"\n>>> FILE: {relative_name} <<<\n{preview}\n"
            except Exception as e:
                combined_text += f"\n>>> FILE: {relative_name} <<<\nОШИБКА ЧТЕНИЯ: {e}\n"

        return combined_text
    except asyncio.TimeoutError:
        return f"{C_RED}Ошибка сканирования: таймаут{C_RESET}"
    except Exception as e:
        return f"Ошибка сканирования: {e}"

async def dialog_web_loop(user_input: str):
    """Глобальный диалог с веб-поиском"""
    global r, client

    if not r or not client:
        print(f"{C_RED}[ERROR]{C_RESET} Система не инициализирована.")
        return

    tools = tools_definition_dialog_web

    # Загружаем историю предыдущих диалогов из Redis
    messages = [{"role": "system", "content": SYSTEM_PROMPT_DIALOG_WEB}]

    # Получаем предыдущие сообщения для контекста
    previous_msgs = await cast(t.Awaitable[List[str]], r.lrange(REDIS_DIALOG_KEY, -MAX_DIALOG_HISTORY, -1))
    if previous_msgs:
        try:
            history = [json.loads(m) for m in previous_msgs]
            messages.extend(history)
            print(f"{C_GRAY}[CONTEXT]{C_RESET} Загружено {len(history)} сообщений из истории диалогов.")
        except json.JSONDecodeError:
            pass

    # Добавляем текущий ввод
    await cast(t.Awaitable[int], r.rpush(REDIS_DIALOG_KEY, json.dumps({"role": "user", "content": user_input})))
    messages.append({"role": "user", "content": user_input})

    try:
        response: ChatResponse = await client.chat(model=OLLAMA_MODEL, messages=messages, tools=tools)
    except Exception as e:
        print(f"{C_RED}[ERROR]{C_RESET} Ошибка Ollama: {e}")
        return

    msg = response["message"]

    if msg.get("tool_calls"):
        # Обработка tool calls (web_search)
        try:
            msg_dict = msg.model_dump() if hasattr(msg, "model_dump") else dict(msg)
        except:
            msg_dict = dict(msg)

        await cast(t.Awaitable[int], r.rpush(REDIS_DIALOG_KEY, json.dumps(msg_dict)))
        messages.append(msg_dict)

        for tool in msg.get("tool_calls"):
            fn = tool.get("function", {})
            name = fn.get("name")
            args = fn.get("arguments", {}) or {}

            if name == "web_search":
                query = args.get("query")
                if isinstance(query, str):
                    print(f"{C_CYAN}[WEB]{C_RESET} 🔍 {query}")
                    res = await web_search_tool(query)
                else:
                    res = "Ошибка: неверный запрос"
            else:
                res = f"Инструмент {name} недоступен в режиме диалога"

            tool_result = {
                "role": "tool",
                "content": res,
                "tool_call_id": tool["id"],
                "name": name,
            }
            await cast(t.Awaitable[int], r.rpush(REDIS_DIALOG_KEY, json.dumps(tool_result)))
            messages.append(tool_result)

        # Получаем финальный ответ (после tool calls)
        try:
            response2: ChatResponse = await client.chat(model=OLLAMA_MODEL, messages=messages, tools=tools)
            text = response2["message"].get("content", "")
        except Exception as e:
            text = f"Ошибка обработки: {e}"
    else:
        text = msg.get("content", "")

    if text:
        print(f"{C_GREEN}🤖 [DIALOG]:{C_RESET} {text}")
        # Сохраняем ответ ассистента
        await cast(t.Awaitable[int], r.rpush(REDIS_DIALOG_KEY, json.dumps({"role": "assistant", "content": text})))

        # Ограничиваем размер истории
        current_len = await cast(t.Awaitable[int], r.llen(REDIS_DIALOG_KEY))
        if current_len > MAX_DIALOG_HISTORY * 2:  # Храним в 2 раза больше для контекста
            await r.ltrim(REDIS_DIALOG_KEY, -MAX_DIALOG_HISTORY, -1)

async def search_docs_tool(query: str) -> str:
    """Поиск в каталоге документации"""
    if not ACTIVE_PROJECT or not ACTIVE_PROJECT.get("doc_path"):
        return "Нет документации."

    doc_path = ACTIVE_PROJECT["doc_path"]
    if not os.path.exists(doc_path):
        return f"Каталог документации не найден: {doc_path}"

    print(f"{C_GRAY}[DOCS]{C_RESET} Поиск: {query}")
    try:
        # rga рекурсивно ищет в каталоге
        proc = await asyncio.create_subprocess_shell(
            f"rga -i -n {shlex.quote(query)} {shlex.quote(doc_path)}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=20.0)
        result = stdout.decode()
        return result[:4000] if result else "Не найдено."
    except asyncio.TimeoutError:
        return "Таймаут поиска."
    except Exception:
        return "Ошибка поиска."

async def search_code_tool(query: str) -> str:
    """Поиск в коде проекта"""
    if not ACTIVE_PROJECT:
        return "Нет проекта."

    path = ACTIVE_PROJECT["path"]
    print(f"{C_GRAY}[SEARCH]{C_RESET} Поиск кода: {query}")
    try:
        proc = await asyncio.create_subprocess_shell(
            f"rga -i -n --glob='!.git' {shlex.quote(query)} {shlex.quote(path)}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=20.0)
        result = stdout.decode()
        return result[:4000] if result else "Не найдено."
    except asyncio.TimeoutError:
        return "Таймаут поиска."
    except Exception:
        return "Ошибка поиска."

async def write_file_tool(path: str, content: str) -> str:
    """Запись файла с подтверждением и diff"""
    try:
        full_path = get_full_path(path)

        diff_text = ""
        if os.path.exists(full_path):
            print(f"{C_YELLOW}[WARN]{C_RESET} Файл '{path}' существует. Читаю старую версию...")
            async with aiofiles.open(full_path, "r", encoding="utf-8") as f:
                old_content = await f.read()

            diff = difflib.unified_diff(
                old_content.splitlines(keepends=True),
                content.splitlines(keepends=True),
                fromfile=f"a/{path}",
                tofile=f"b/{path}",
                lineterm="",
            )
            diff_text = "".join(diff)

            if diff_text:
                print(f"\n{C_GRAY}--- DIFF ---{C_RESET}")
                print(diff_text)
                print(f"{C_GRAY}--- END ---{C_RESET}")

            confirm = await asyncio.to_thread(input, f"{C_YELLOW}❓ Перезаписать '{path}'? [y/N]: {C_RESET}")
            if confirm.lower() != "y":
                return "Запись отменена."

        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        async with aiofiles.open(full_path, "w", encoding="utf-8") as f:
            await f.write(content)
        return f"{C_GREEN}✅{C_RESET} Файл записан: {path}"
    except PermissionError as e:
        return f"{C_RED}Ошибка: {e}{C_RESET}"
    except Exception as e:
        return f"{C_RED}Ошибка записи: {e}{C_RESET}"

async def read_file_tool(path: str) -> str:
    """Чтение файла"""
    try:
        full_path = get_full_path(path)
        async with aiofiles.open(full_path, "r", encoding="utf-8", errors="replace") as f:
            return await f.read()
    except FileNotFoundError:
        return f"Файл не найден: {path}"
    except Exception as e:
        return f"Ошибка чтения: {e}"

async def run_shell_tool(cmd: str) -> str:
    """Выполнение shell-команды в директории проекта"""
    if not ACTIVE_PROJECT:
        return "Нет проекта."

    project_path = ACTIVE_PROJECT["path"]
    print(f"{C_GRAY}[SHELL]{C_RESET} Команда: {cmd}")
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            cwd=project_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60.0)

        out = []
        if stdout:
            out.append(f"STDOUT:\n{stdout.decode()}")
        if stderr:
            out.append(f"STDERR:\n{stderr.decode()}")

        result = "\n".join(out)
        return result[:4000] if result else "Команда выполнена."
    except asyncio.TimeoutError:
        return f"{C_RED}Таймаут команды{C_RESET}"
    except Exception as e:
        return f"{C_RED}Ошибка: {e}{C_RESET}"

async def web_search_tool(query: str) -> str:
    """Веб-поиск через DuckDuckGo"""
    print(f"{C_GRAY}[WEB]{C_RESET} Поиск: {query}")
    url: Optional[str] = None
    loop = asyncio.get_running_loop()
    try:
        results = await loop.run_in_executor(None, lambda: list(DDGS().text(query, max_results=1)))
        if not results:
            return "Ничего не найдено."

        url = results[0].get("href")
        if not url:
            return "URL не найден в результатах."

        print(f"{C_GRAY}[WEB]{C_RESET} Читаю: {url}")
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                html = await resp.text()

        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.extract()
        text = soup.get_text(separator="\n", strip=True)
        return text[:8000]
    except aiohttp.ClientError:
        return f"Ошибка загрузки страницы: {url or 'unknown URL'}"
    except Exception as e:
        return f"Ошибка поиска: {e}"

# --- ЦВЕТОВЫЕ КОДЫ (ANSI) ---
C_RESET = "\033[0m"
C_BOLD = "\033[1m"
C_GREEN = "\033[92m"
C_BLUE = "\033[94m"
C_YELLOW = "\033[93m"
C_RED = "\033[91m"
C_CYAN = "\033[96m"
C_GRAY = "\033[90m"

def print_header():
    print(f"\n{C_GRAY}{'='*60}{C_RESET}")
    print(f"{C_BLUE}🛠  AI Project Manager v5.1{C_RESET} {C_GRAY}|{C_RESET} Docs Dir & Delete")
    print(f"{C_GRAY}{'='*60}{C_RESET}\n")

def print_help():
    print(f"{C_CYAN}Команды:{C_RESET}")
    print(f"  {C_YELLOW}/info{C_RESET}                          {C_GRAY}Панель команд{C_RESET}")
    print(f"  {C_YELLOW}/create <name> <path> <goal>{C_RESET}   {C_GRAY}Создать проект{C_RESET}")
    print(f"  {C_YELLOW}/list{C_RESET}                          {C_GRAY}Список проектов{C_RESET}")
    print(f"  {C_YELLOW}/load <name>{C_RESET}                   {C_GRAY}Загрузить проект{C_RESET}")
    print(f"  {C_YELLOW}/delete <name>{C_RESET}                 {C_GRAY}Удалить проект{C_RESET}")
    print(f"  {C_YELLOW}/doc <directory>{C_RESET}               {C_GRAY}Прикрепить каталог документации{C_RESET}")
    print(f"  {C_YELLOW}/doc_del{C_RESET}                       {C_GRAY}Удалить путь к документации{C_RESET}")
    print(f"  {C_YELLOW}/analyze{C_RESET}                       {C_GRAY}Режим анализа{C_RESET}")
    print(f"  {C_YELLOW}/analyze_prompt <text>{C_RESET}         {C_GRAY}Сохранить промпт{C_RESET}")
    print(f"  {C_YELLOW}/architect <text>{C_RESET}              {C_GRAY}Сохранить архитектуру{C_RESET}")
    print(f"  {C_YELLOW}/dev{C_RESET}                           {C_GRAY}Режим разработки{C_RESET}")
    print(f"  {C_YELLOW}/review <file>{C_RESET}                 {C_GRAY}Ревью кода{C_RESET}")
    print(f"  {C_YELLOW}/explain <file>{C_RESET}                {C_GRAY}Объяснить код{C_RESET}")
    print(f"  {C_YELLOW}/dialog_web <question>{C_RESET}         {C_GRAY}Диалог с ИИ (с веб-поиском){C_RESET}")
    print(f"  {C_YELLOW}/close{C_RESET}                         {C_GRAY}Сохранить и выйти{C_RESET}")
    print(f"  {C_YELLOW}/exit{C_RESET}                          {C_GRAY}Выход{C_RESET}")
    print()

# --- MAIN AGENT LOOP ---

async def agent_loop(user_input: str, mode: str = "dev"):
    """Основной цикл агента с поддержкой инструментов"""
    global ACTIVE_PROJECT, r, client

    if not ACTIVE_PROJECT or not r or not client:
        print(f"{C_RED}[ERROR]{C_RESET} Система не инициализирована.")
        return

    project_id = ACTIVE_PROJECT["id"]
    redis_key = f"{REDIS_CHAT_KEY_PREFIX}{project_id}"

    match mode:
        case "analyzer":
            sys_prompt = SYSTEM_PROMPT_ANALYZER
            tools = tools_definition_analyzer
        case "review":
            sys_prompt = SYSTEM_PROMPT_REVIEW
            tools = tools_definition_analyzer
        case "explain":
            sys_prompt = SYSTEM_PROMPT_EXPLAIN
            tools = tools_definition_analyzer
        case "dialog_web":
            sys_prompt = SYSTEM_PROMPT_DIALOG_WEB
            tools = tools_definition_dialog_web
        case _:
            sys_prompt = SYSTEM_PROMPT_DEV
            tools = tools_definition_dev

    messages = [{"role": "system", "content": sys_prompt}]

    project_context = []
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

    history_len = await cast(t.Awaitable[int], r.llen(redis_key))
    if history_len == 0:
        print(f"{C_GRAY}[SYSTEM]{C_RESET} Сканирование файлов проекта...")
        scan_result = await scan_directory_tool()
        if scan_result and not scan_result.startswith("Ошибка"):
            messages.append(
                {
                    "role": "system",
                    "content": f"АВТОМАТИЧЕСКИЙ СКАН ПРОЕКТА:\n{scan_result}",
                }
            )

    redis_msgs = await cast(t.Awaitable[List[str]], r.lrange(redis_key, 0, -1))
    if redis_msgs:
        try:
            history = [json.loads(m) for m in redis_msgs[-MAX_DB_HISTORY:]]
            messages.extend(history)
        except json.JSONDecodeError:
            print(f"{C_YELLOW}[WARN]{C_RESET} Ошибка чтения истории Redis.")

    await cast(t.Awaitable[int], r.rpush(redis_key, json.dumps({"role": "user", "content": user_input})))
    messages.append({"role": "user", "content": user_input})

    for iteration in range(MAX_ITERATIONS):
        try:
            response: ChatResponse = await client.chat(model=OLLAMA_MODEL, messages=messages, tools=tools)
        except Exception as e:
            print(f"{C_RED}[ERROR]{C_RESET} Ошибка Ollama: {e}")
            break

        msg = response["message"]

        if msg.get("tool_calls"):
            try:
                msg_dict = msg.model_dump() if hasattr(msg, "model_dump") else dict(msg)
            except:
                msg_dict = dict(msg)

            await cast(t.Awaitable[int], r.rpush(redis_key, json.dumps(msg_dict)))
            messages.append(msg_dict)

            for tool in msg.get("tool_calls"):
                fn = tool.get("function", {})
                name = fn.get("name")
                args = fn.get("arguments", {}) or {}
                res = ""

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
                    "tool_call_id": tool["id"],
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

# --- MAIN CLI ---

async def main():
    """Главная CLI-функция"""
    global ACTIVE_PROJECT

    if not await init_db():
        print(f"{C_RED}Ошибка инициализации БД. Выход.{C_RESET}")
        return

    if not await init_redis():
        print(f"{C_YELLOW}Предупреждение: Redis недоступен. История не будет сохранена.{C_RESET}")

    if not await init_ollama():
        print(f"{C_RED}Ошибка подключения к Ollama. Выход.{C_RESET}")
        return

    print_header()
    print_help()

    while True:
        try:
            prompt_proj = f"{C_CYAN}[{ACTIVE_PROJECT['name']}]{C_RESET} " if ACTIVE_PROJECT else ""
            user_input = input(f"{C_YELLOW}➜ {C_RESET}{prompt_proj}")
        except (EOFError, KeyboardInterrupt):
            break

        if not user_input.strip():
            continue

        parts = user_input.split()
        cmd = parts[0]

        match cmd:
            case "/exit":
                break

            case "/close":
                if ACTIVE_PROJECT:
                    await sync_redis_to_db(ACTIVE_PROJECT["id"])
                    await update_project_fields({"status": "closed"})
                    name = ACTIVE_PROJECT["name"]
                    ACTIVE_PROJECT = None
                    print(f"{C_GREEN}[CLOSED]{C_RESET} Проект '{name}' сохранен.")
                else:
                    print(f"{C_GRAY}Нет активного проекта.{C_RESET}")
                continue

            case "/list":
                projs = await get_all_projects()
                if not projs:
                    print(f"{C_GRAY}Список пуст.{C_RESET}")
                    continue
                print(f"\n{C_CYAN}{'Название':<25} | {'Статус':<10} | {'Цель'}{C_RESET}")
                print("-" * 60)
                for p in projs:
                    status = p["status"]
                    name = p["name"]
                    goal = p["goal"][:40] + "..." if p["goal"] and len(p["goal"]) > 40 else (p["goal"] or "")
                    color = C_GREEN if status == "active" else C_GRAY
                    print(f"{color}{name:<25} | {status:<10} | {goal}{C_RESET}")
                print()
                continue

            case "/create":
                args_text = " ".join(parts[1:])
                match_args = re.match(r'(\S+)\s+(\S+)\s*(.*)', args_text)
                if match_args:
                    if await create_project(match_args.group(1), match_args.group(2), match_args.group(3)):
                        await load_project(match_args.group(1))
                else:
                    print(f"{C_RED}[ERROR]{C_RESET} Формат: {C_YELLOW}/create имя путь цель{C_RESET}")
                continue

            case "/load":
                if len(parts) > 1:
                    await load_project(parts[1])
                else:
                    print(f"{C_RED}[ERROR]{C_RESET} Укажите имя проекта.")
                continue

            case "/delete":
                if len(parts) > 1:
                    name = parts[1]
                    # Если удаляем активный проект, сбрасываем сессию
                    if ACTIVE_PROJECT and ACTIVE_PROJECT.get("name") == name:
                        ACTIVE_PROJECT = None
                    await delete_project(name)
                else:
                    print(f"{C_RED}[ERROR]{C_RESET} Укажите имя проекта для удаления.")
                continue

            case "/doc":
                if not ACTIVE_PROJECT:
                    print(f"{C_RED}[ERROR]{C_RESET} Нет проекта.{C_RESET}")
                    continue
                if len(parts) > 1:
                    doc_path = parts[1]
                    if os.path.isdir(doc_path):
                        await update_project_fields({"doc_path": doc_path})
                        print(f"{C_GREEN}[OK]{C_RESET} Каталог документации привязан: {doc_path}")
                    else:
                        print(f"{C_RED}[ERROR]{C_RESET} Укажите существующий каталог.")
                else:
                    print(f"{C_RED}[ERROR]{C_RESET} Укажите путь к каталогу документации.")
                continue

            case "/doc_del":
                if not ACTIVE_PROJECT:
                    continue
                # Просто очищаем поле в БД
                if await update_project_fields({"doc_path": None}):
                    print(f"{C_GREEN}[OK]{C_RESET} Путь к документации удален.")
                else:
                    print(f"{C_RED}[ERROR]{C_RESET} Не удалось удалить путь к документации.")
                continue

            case "/analyze":
                if not ACTIVE_PROJECT:
                    print(f"{C_RED}[ERROR]{C_RESET} Нет проекта.{C_RESET}")
                    continue
                await update_project_fields({"status": "analysis"})
                print(f"{C_BLUE}[MODE]{C_RESET} Режим Анализа. Используйте /analyze_prompt или /architect.")
                continue

            case "/analyze_prompt":
                if not ACTIVE_PROJECT:
                    continue
                if len(parts) > 1:
                    prompt_text = " ".join(parts[1:])
                    await update_project_fields({"final_prompt": prompt_text})
                    print(f"{C_GREEN}[OK]{C_RESET} Промпт сохранен.")
                else:
                    print(f"{C_RED}[ERROR]{C_RESET} Укажите текст промпта.")
                continue

            case "/architect":
                if not ACTIVE_PROJECT:
                    continue
                if len(parts) > 1:
                    arch_text = " ".join(parts[1:])
                    await update_project_fields({"architecture": arch_text})
                    print(f"{C_GREEN}[OK]{C_RESET} Архитектура сохранена.")
                else:
                    print(f"{C_RED}[ERROR]{C_RESET} Укажите описание архитектуры.")
                continue

            case "/dev":
                if not ACTIVE_PROJECT:
                    continue
                await update_project_fields({"status": "active"})
                print(f"{C_GREEN}[MODE]{C_RESET} Режим Разработки.")
                await agent_loop("Проанализируй Промпт и Архитектуру, создай план и начни разработку.")
                continue

            case "/review":
                if not ACTIVE_PROJECT:
                    continue
                if len(parts) > 1:
                    await agent_loop(f"Сделай Code Review файла {parts[1]}. Найди ошибки и уязвимости.", mode="review")
                else:
                    print(f"{C_RED}[ERROR]{C_RESET} Укажите файл для ревью.")
                continue

            case "/explain":
                if not ACTIVE_PROJECT:
                    continue
                if len(parts) > 1:
                    await agent_loop(f"Объясни файл {parts[1]} построчно.", mode="explain")
                else:
                    print(f"{C_RED}[ERROR]{C_RESET} Укажите файл для объяснения.")
                continue

            case "/dialog_web":
                question = " ".join(parts[1:]) if len(parts) > 1 else ""
                if not question:
                    # Интерактивный режим
                    print(f"{C_BLUE}[DIALOG]{C_RESET} Режим свободного диалога. Введите 'выход' для завершения.")
                    print(f"{C_GRAY}История сохраняется в Redis. Контекст предыдущих разговоров загружен.{C_RESET}")
                    while True:
                        user_input = input(f"{C_YELLOW}> {C_RESET}")
                        if not user_input.strip():
                            continue
                        if user_input.lower() in ["выход", "exit", "стоп", "quit"]:
                            print(f"{C_BLUE}[DIALOG]{C_RESET} Диалог завершен. История сохранена в Redis.")
                            break
                        await dialog_web_loop(user_input)
                else:
                    # Одноразовый вопрос
                    await dialog_web_loop(question)
                continue

            case "/info":
                print_help()
                continue

            case _:
                if not ACTIVE_PROJECT:
                    print(f"{C_GRAY}Нет проекта. Создайте или загрузите.{C_RESET}")
                    continue

                mode = "analyzer" if ACTIVE_PROJECT.get("status") == "analysis" else "dev"
                await agent_loop(user_input, mode=mode)

    if ACTIVE_PROJECT:
        await sync_redis_to_db(ACTIVE_PROJECT["id"])
        print(f"{C_GRAY}💾{C_RESET} Проект сохранен.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print(f"\n{C_GRAY}👋 До свидания!{C_RESET}")
