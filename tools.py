import asyncio
import json
import os
import re
import difflib
import shlex
import typing as t
from typing import cast, List
from urllib.parse import urlparse

import aiohttp
import aiofiles
from bs4 import BeautifulSoup
from ddgs import DDGS
from ollama import ChatResponse

# Импорты из наших модулей
from config import *
import bd

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

    if not ACTIVE_PROJECT.get("path"):
        return "Путь к проекту не указан в базе данных."

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
            relative_name = ""
            try:
                relative_name = os.path.relpath(f_path, base_path)
                async with aiofiles.open(f_path, "r", encoding="utf-8", errors="replace") as f:
                    content = await f.read()
                    preview = content[:LENGTH_CONTEXT] + "... (обрезано)" if len(content) > LENGTH_CONTEXT else content
                    combined_text += f"\n>>> FILE: {relative_name} <<<\n{preview}\n"
            except Exception as e:
                combined_text += f"\n>>> FILE: {relative_name} <<<\nОШИБКА ЧТЕНИЯ: {e}\n"

        return combined_text
    except asyncio.TimeoutError:
        return f"{C_RED}Ошибка сканирования: таймаут{C_RESET}"
    except Exception as e:
        return f"Ошибка сканирования: {e}"


async def get_dialog_status() -> str:
    """Показывает статус глобального диалога"""
    if not bd.r:
        return "Redis не доступен."

    try:
        length = await cast(t.Awaitable[int], bd.r.llen(REDIS_DIALOG_KEY))
        memory_usage = await bd.r.memory_usage(REDIS_DIALOG_KEY) or 0

        preview = ""
        if length > 0:
            first_msgs = await cast(t.Awaitable[List[str]], bd.r.lrange(REDIS_DIALOG_KEY, 0, 2))
            last_msgs = await cast(t.Awaitable[List[str]], bd.r.lrange(REDIS_DIALOG_KEY, -3, -1))

            preview += f"\n{C_GRAY}Первые сообщения:{C_RESET}\n"
            for i, msg in enumerate(first_msgs, 1):
                try:
                    data = json.loads(msg)
                    role = data.get("role", "unknown")
                    content = data.get("content", "")[:50] + "..." if len(data.get("content", "")) > 50 else data.get("content", "")
                    preview += f"  {i}. [{role}] {content}\n"
                except:
                    preview += f"  {i}. [ошибка чтения]\n"

            if length > 6:
                preview += f"  ... ({length - 6} сообщений скрыто) ...\n"

            preview += f"\n{C_GRAY}Последние сообщения:{C_RESET}\n"
            for i, msg in enumerate(last_msgs, max(1, length - 2)):
                try:
                    data = json.loads(msg)
                    role = data.get("role", "unknown")
                    content = data.get("content", "")[:50] + "..." if len(data.get("content", "")) > 50 else data.get("content", "")
                    preview += f"  {i}. [{role}] {content}\n"
                except:
                    preview += f"  {i}. [ошибка чтения]\n"

        return (f"{C_CYAN}=== Статус диалога ==={C_RESET}\n"
                f"Количество сообщений: {C_GREEN}{length}{C_RESET}\n"
                f"Использование памяти: {C_GREEN}{memory_usage / 1024:.2f} KB{C_RESET}\n"
                f"Лимит истории: {MAX_DIALOG_HISTORY}\n"
                f"{preview}")
    except Exception as e:
        return f"{C_RED}Ошибка получения статуса: {e}{C_RESET}"


async def clean_dialog_history() -> str:
    """Очищает всю историю глобального диалога"""
    if not bd.r:
        return f"{C_RED}Redis не доступен.{C_RESET}"

    try:
        await bd.r.delete(REDIS_DIALOG_KEY)
        return f"{C_GREEN}✅{C_RESET} История диалога полностью очищена."
    except Exception as e:
        return f"{C_RED}Ошибка очистки: {e}{C_RESET}"


async def dialog_web_loop(user_input: str):
    """Глобальный диалог с веб-поиском и поддержкой множественных tool_calls"""
    global r, client

    if not bd.r or not bd.client:
        print(f"{C_RED}[ERROR]{C_RESET} Система не инициализирована.")
        return

    tools = tools_definition_dialog_web
    messages = [{"role": "system", "content": SYSTEM_PROMPT_DIALOG_WEB}]

    # Загружаем историю из Redis
    previous_msgs = await cast(t.Awaitable[List[str]], bd.r.lrange(REDIS_DIALOG_KEY, -MAX_DIALOG_HISTORY, -1))
    if previous_msgs:
        try:
            history = [json.loads(m) for m in previous_msgs]
            messages.extend(history)
            print(f"{C_GRAY}[CONTEXT]{C_RESET} Загружено {len(history)} сообщений из истории диалогов.")
        except json.JSONDecodeError:
            pass

    # Добавляем сообщение пользователя
    await cast(t.Awaitable[int], bd.r.rpush(REDIS_DIALOG_KEY, json.dumps({"role": "user", "content": user_input})))
    messages.append({"role": "user", "content": user_input})

    # ОСНОВНОЙ ЦИКЛ - поддержка множественных tool_calls
    max_iterations = DIALOG_MAX_ITERATIONS
    for iteration in range(max_iterations):
        print(f"{C_GRAY}[DIALOG]{C_RESET} Итерация {iteration + 1}/{max_iterations}...")

        try:
            response: ChatResponse = await bd.client.chat(
                model=OLLAMA_MODEL,
                messages=messages,
                tools=tools,
                options=OLLAMA_OPTIONS
            )
        except Exception as e:
            print(f"{C_RED}[ERROR]{C_RESET} Ошибка Ollama: {e}")
            return

        msg = response["message"]

        # Если модель хочет использовать инструменты
        if msg.get("tool_calls"):
            try:
                msg_dict = msg.model_dump() if hasattr(msg, "model_dump") else dict(msg)
            except:
                msg_dict = dict(msg)

            # Сохраняем сообщение с tool_calls
            await cast(t.Awaitable[int], bd.r.rpush(REDIS_DIALOG_KEY, json.dumps(msg_dict)))
            messages.append(msg_dict)

            # Обрабатываем каждый вызов инструмента
            for tool in msg.get("tool_calls"):
                fn = tool.get("function", {})
                name = fn.get("name")
                args = fn.get("arguments", {}) or {}
                tool_id = tool.get("id") or f"{name}_{hash(str(args))}" or "unknown"

                if name == "web_search":
                    query = args.get("query")
                    if isinstance(query, str):
                        print(f"{C_CYAN}[WEB]{C_RESET} 🔍 Поиск #{iteration + 1}: {query}")
                        res = await web_search_tool(query)
                    else:
                        res = "Ошибка: неверный запрос"
                else:
                    res = f"Инструмент {name} недоступен в режиме диалога"

                # Сохраняем результат инструмента
                tool_result = {
                    "role": "tool",
                    "content": res,
                    "tool_call_id": tool_id,
                    "name": name,
                }
                await cast(t.Awaitable[int], bd.r.rpush(REDIS_DIALOG_KEY, json.dumps(tool_result)))
                messages.append(tool_result)

            # Продолжаем цикл - даём модели возможность обработать результаты
            continue

        # Если модель вернула текстовый ответ
        text = msg.get("content", "")
        if text:
            print(f"{C_GREEN}🤖 [DIALOG]:{C_RESET} {text}")
            await cast(t.Awaitable[int], bd.r.rpush(REDIS_DIALOG_KEY, json.dumps({"role": "assistant", "content": text})))
            break
        else:
            # Нет ни tool_calls, ни content
            print(f"{C_YELLOW}[WARN]{C_RESET} Модель вернула пустой ответ на итерации {iteration + 1}")
            if iteration == max_iterations - 1:
                fallback_text = "Извините, не удалось сформировать ответ. Попробуйте переформулировать вопрос."
                print(f"{C_GREEN}🤖 [DIALOG]:{C_RESET} {fallback_text}")
                await cast(t.Awaitable[int], bd.r.rpush(REDIS_DIALOG_KEY, json.dumps({"role": "assistant", "content": fallback_text})))
            break

    # Обрезаем историю если она слишком длинная
    current_len = await cast(t.Awaitable[int], bd.r.llen(REDIS_DIALOG_KEY))
    if current_len > MAX_DIALOG_HISTORY * 2:
        await bd.r.ltrim(REDIS_DIALOG_KEY, -MAX_DIALOG_HISTORY, -1)
        print(f"{C_GRAY}[REDIS]{C_RESET} История обрезана до {MAX_DIALOG_HISTORY} сообщений.")

async def search_docs_tool(query: str) -> str:
    """Поиск в каталоге документации"""
    if not ACTIVE_PROJECT or not ACTIVE_PROJECT.get("doc_path"):
        return "Нет документации."

    doc_path = ACTIVE_PROJECT["doc_path"]
    if not os.path.exists(doc_path):
        return f"Каталог документации не найден: {doc_path}"

    print(f"{C_GRAY}[DOCS]{C_RESET} Поиск: {query}")
    try:
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
    """Веб-поиск через DuckDuckGo с фильтрацией китайских и мусорных сайтов"""
    print(f"{C_GRAY}[WEB]{C_RESET} Поиск: {query} (макс. {WEB_SEARCH_MAX_RESULTS} сайтов)")

    loop = asyncio.get_running_loop()
    all_texts = []

    # Определяем специальные случаи для прямого запроса
    rust_query = 'rust' in query.lower() and ('версия' in query.lower() or 'version' in query.lower())
    python_query = 'python' in query.lower() and ('версия' in query.lower() or 'version' in query.lower())

    # Для запросов о версиях языков программирования - сразу идем к официальным источникам
    if rust_query:
        print(f"{C_CYAN}[DIRECT]{C_RESET} Запрос к официальному источнику Rust...")
        try:
            async with aiohttp.ClientSession() as session:
                # Получаем главную страницу rust-lang.org
                try:
                    async with session.get('https://www.rust-lang.org/', timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        html = await resp.text()
                        soup = BeautifulSoup(html, "html.parser")

                        # Ищем информацию о версии на главной странице
                        text = soup.get_text(separator="\n", strip=True)
                        lines = [line.strip() for line in text.splitlines() if line.strip() and len(line.strip()) > 10]
                        text = "\n".join(lines[:100])  # Увеличено с 50 до 100 строк

                        if text:
                            all_texts.append(f"=== Источник 1: Rust Official Website (rust-lang.org) ===\n{text[:3500]}")  # Увеличено с 2000 до 3500
                            print(f"{C_GREEN}✓{C_RESET} Получено с rust-lang.org: {len(text[:3500])} символов")
                except Exception as e:
                    print(f"{C_YELLOW}⚠{C_RESET} Ошибка rust-lang.org: {e}")

                # Пробуем получить информацию о релизах из блога
                try:
                    async with session.get('https://blog.rust-lang.org/', timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        html = await resp.text()
                        soup = BeautifulSoup(html, "html.parser")

                        # Ищем последние посты о релизах
                        posts_found = 0
                        for article in soup.find_all(['article', 'div'], limit=10):
                            title_elem = article.find(['h1', 'h2', 'h3', 'a'])
                            if not title_elem:
                                continue

                            title = title_elem.get_text(strip=True)

                            if any(keyword in title.lower() for keyword in ['announcing', 'release', '1.', 'rust']):
                                content = article.get_text(separator="\n", strip=True)
                                lines = [line.strip() for line in content.splitlines() if line.strip() and len(line.strip()) > 10]
                                content = "\n".join(lines[:80])  # Увеличено с 40 до 80 строк

                                if len(content) > 100:
                                    all_texts.append(f"=== Источник {len(all_texts)+1}: Rust Blog - {title[:80]} ===\n{content[:3000]}")  # Увеличено с 1800 до 3000
                                    print(f"{C_GREEN}✓{C_RESET} Получено с blog.rust-lang.org: {title[:60]}... ({len(content[:3000])} символов)")
                                    posts_found += 1

                                    if posts_found >= 2:  # Берем максимум 2 поста о релизах
                                        break
                except Exception as e:
                    print(f"{C_YELLOW}⚠{C_RESET} Ошибка blog.rust-lang.org: {e}")

                # Пробуем получить changelog или release notes
                try:
                    async with session.get('https://github.com/rust-lang/rust/releases', timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        html = await resp.text()
                        soup = BeautifulSoup(html, "html.parser")

                        # Ищем первый релиз
                        release = soup.find('div', class_='release-entry') or soup.find('section')
                        if release:
                            content = release.get_text(separator="\n", strip=True)
                            lines = [line.strip() for line in content.splitlines() if line.strip() and len(line.strip()) > 10]
                            content = "\n".join(lines[:60])  # Увеличено с 30 до 60 строк

                            if len(content) > 100:
                                all_texts.append(f"=== Источник {len(all_texts)+1}: GitHub Rust Releases ===\n{content[:2500]}")  # Увеличено с 1500 до 2500
                                print(f"{C_GREEN}✓{C_RESET} Получено с GitHub releases: {len(content[:2500])} символов")
                except Exception as e:
                    print(f"{C_YELLOW}⚠{C_RESET} Ошибка GitHub releases: {e}")

                if all_texts:
                    combined = "\n\n".join(all_texts)
                    if len(combined) > WEB_SEARCH_MAX_LENGTH:
                        combined = combined[:WEB_SEARCH_MAX_LENGTH] + f"\n\n... [Обрезано до {WEB_SEARCH_MAX_LENGTH} символов]"
                    print(f"{C_GRAY}[WEB]{C_RESET} Возвращаю {len(combined)} символов данных из {len(all_texts)} источников")
                    return combined
                else:
                    print(f"{C_YELLOW}[WARN]{C_RESET} Прямой запрос не дал результатов, переключаюсь на поиск...")

        except Exception as e:
            print(f"{C_YELLOW}[WARN]{C_RESET} Прямой запрос не удался: {e}, переключаюсь на поиск...")

    elif python_query:
        print(f"{C_CYAN}[DIRECT]{C_RESET} Запрос к официальному источнику Python...")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get('https://www.python.org/', timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    html = await resp.text()
                    soup = BeautifulSoup(html, "html.parser")
                    text = soup.get_text(separator="\n", strip=True)
                    lines = [line.strip() for line in text.splitlines() if line.strip() and len(line.strip()) > 10]
                    text = "\n".join(lines[:LIMIT_PARSING])

                    if text:
                        all_texts.append(f"=== Источник 1: Python Official Website (python.org) ===\n{text[:2000]}")
                        print(f"{C_GREEN}✓{C_RESET} Получено с python.org: {len(text)} символов")
                        combined = "\n\n".join(all_texts)
                        return combined[:WEB_SEARCH_MAX_LENGTH]
        except Exception as e:
            print(f"{C_YELLOW}[WARN]{C_RESET} Прямой запрос не удался: {e}, переключаюсь на поиск...")

    # Черный список доменов (развлекательные + КИТАЙСКИЕ + ФОРУМЫ)
    blocked_domains = {
        # Развлекательные
        'rutube.ru', 'youtube.com', 'youtu.be', 'kinopoisk.ru',
        'vk.com', 'ok.ru', 'tiktok.com', 'instagram.com', 'facebook.com',
        'genius.com', 'wikislovary.ru', 'wiktionary.org', 'urban dictionary',
        'pinterest.com', 'twitter.com', 'x.com',
        # КИТАЙСКИЕ ДОМЕНЫ
        'zhihu.com', 'baidu.com', 'weibo.com', 'qq.com', 'taobao.com',
        'tmall.com', 'jd.com', 'sina.com.cn', 'sohu.com', '163.com',
        'douban.com', 'bilibili.com', 'csdn.net', 'cnblogs.com',
        'jianshu.com', 'oschina.net', 'iteye.com', 'segmentfault.com',
        'juejin.cn', 'toutiao.com', 'aliyun.com', 'huawei.com',
        'xiaomi.com', 'oppo.com', 'vivo.com',
        # ФОРУМЫ И НЕОФИЦИАЛЬНЫЕ ИСТОЧНИКИ
        'alkad.org'
    }

    # Паттерны мусорных сайтов
    blocked_patterns = [
        'как пишется', 'песня', 'текст песни', 'lyrics', 'фильм',
        'смотреть онлайн', 'трейлер', 'wiki/последняя', 'wiki/последний',
        'значение слова', 'перевод', 'словарь', 'что значит',
        'форум', 'обсуждение'
    ]

    try:
        query_lower = query.lower()
        enhanced_query = query
        replacements = {
                    'последняя версия': 'latest version',
                    'последний': 'latest',
                    'версия': 'version',
                    'какая': 'what',
                    'какой': 'what'
                }
        english_keywords = {'version', 'latest', 'release', 'notes', 'changes', 'stable', 'programming'}
        match None:
            case _ if any(kw in query_lower for kw in english_keywords):
                pass
            case _:
                for rus, eng in replacements.items():
                    if rus in query_lower:
                        enhanced_query = enhanced_query.replace(rus,eng)
        print(f"{C_GRAY}[WEB]{C_RESET} Запрос к поиску: {enhanced_query}")

        # Получаем результаты
        search_region = 'wt-wt'  # без региональных фильтров

        print(f"{C_GRAY}[WEB]{C_RESET} DuckDuckGo поиск (регион: {search_region})...")

        results = await loop.run_in_executor(
            None,
            lambda: list(DDGS().text(enhanced_query, max_results=30, region=search_region))
        )

        print(f"{C_GRAY}[WEB]{C_RESET} Найдено результатов: {len(results)}")

        if not results:
            return "Ничего не найдено в поисковой системе. Попробуйте переформулировать запрос."

        print(f"{C_GRAY}[WEB]{C_RESET} Начинаю фильтрацию результатов...")

        # Фильтрация с приоритизацией
        all_valid_results = []
        blocked_count = {'chinese_domain': 0, 'chinese_title': 0, 'patterns': 0, 'invalid_url': 0, 'forums': 0}

        # Официальные домены для приоритета (с весами)
        priority_domains = {
            'rust-lang.org': 100,
            'doc.rust-lang.org': 100,
            'blog.rust-lang.org': 90,
            'github.com/rust-lang': 85,
            'python.org': 100,
            'docs.python.org': 100,
            'nodejs.org': 100,
            'developer.mozilla.org': 95,
            'golang.org': 100,
            'go.dev': 100,
            'docs.oracle.com': 90,
            'openjdk.org': 90,
            'wikipedia.org': 90,
            'en.wikipedia.org': 70,
            'reddit.com': 95,
            'habr.com': 80,
            'stackoverflow.com': 95
        }

        for r in results:
            title = r.get("title", "")
            href = r.get("href", "")
            body = r.get("body", "")

            # Проверяем URL
            if not href.startswith(('http://', 'https://')):
                blocked_count['invalid_url'] += 1
                continue

            # Проверяем домен
            parsed = urlparse(href)
            domain = parsed.netloc.lower()

            if domain.startswith('www.'):
                domain = domain[4:]

            if not domain or '.' not in domain:
                blocked_count['invalid_url'] += 1
                continue

            # БЛОКИРУЕМ форумы
            if 'forum' in domain or 'форум' in title.lower():
                blocked_count['forums'] += 1
                continue

            # БЛОКИРУЕМ домены с китайскими расширениями
            if domain.endswith(('.cn', '.com.cn')):
                blocked_count['chinese_domain'] += 1
                continue

            # БЛОКИРУЕМ известные китайские/мусорные домены
            if any(blocked in domain for blocked in blocked_domains):
                blocked_count['chinese_domain'] += 1
                continue

            # БЛОКИРУЕМ если в домене есть китайские символы
            if any('\u4e00' <= char <= '\u9fff' for char in domain):
                blocked_count['chinese_domain'] += 1
                continue

            # Проверяем заголовок на китайские символы
            chinese_in_title = sum(1 for char in title if '\u4e00' <= char <= '\u9fff')
            if chinese_in_title > 0:
                blocked_count['chinese_title'] += 1
                continue

            # Проверяем паттерны в заголовке и описании
            title_lower = title.lower()
            body_lower = body.lower()

            if any(pattern in title_lower or pattern in body_lower for pattern in blocked_patterns):
                blocked_count['patterns'] += 1
                continue

            # Определяем приоритет
            priority_score = 0
            for priority_domain, score in priority_domains.items():
                if priority_domain in domain or priority_domain in href:
                    priority_score = score
                    break

            # Добавляем результат с приоритетом
            all_valid_results.append({
                'result': r,
                'priority': priority_score,
                'domain': domain,
                'title': title
            })

        # Сортируем: сначала по приоритету (убывание), потом по порядку
        all_valid_results.sort(key=lambda x: (-x['priority'], results.index(x['result'])))

        # Берем топ результатов
        final_results = [item['result'] for item in all_valid_results[:WEB_SEARCH_MAX_RESULTS]]

        # Выводим информацию
        print(f"{C_GRAY}[WEB]{C_RESET} Отфильтровано: домены={blocked_count['chinese_domain']}, заголовки={blocked_count['chinese_title']}, паттерны={blocked_count['patterns']}, форумы={blocked_count['forums']}, некорректные={blocked_count['invalid_url']}")

        priority_count = sum(1 for item in all_valid_results[:WEB_SEARCH_MAX_RESULTS] if item['priority'] > 0)
        print(f"{C_GRAY}[WEB]{C_RESET} Выбрано результатов: {len(final_results)} (приоритетных: {priority_count})")

        # Показываем что выбрано
        for i, item in enumerate(all_valid_results[:WEB_SEARCH_MAX_RESULTS], 1):
            if item['priority'] > 0:
                print(f"{C_GREEN}[★ {item['priority']}]{C_RESET} {item['title'][:60]}... ({item['domain']})")
            else:
                print(f"{C_CYAN}[OK]{C_RESET} {item['title'][:60]}... ({item['domain']})")

        if not final_results:
            return f"Не удалось найти релевантные результаты. Всего найдено: {len(results)}, заблокировано: {sum(blocked_count.values())}"

        # Создаем connector с увеличенным таймаутом для подключения
        timeout = aiohttp.ClientTimeout(
            total=WEB_SEARCH_TIMEOUT,
            connect=5,  # 5 секунд на подключение
            sock_read=5  # 5 секунд на чтение сокета
        )

        connector = aiohttp.TCPConnector(
            limit=10,
            limit_per_host=3,
            ttl_dns_cache=300
        )

        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            for i, result in enumerate(final_results, 1):
                url = result.get("href")
                title = result.get("title", "Без названия")

                print(f"{C_GRAY}[WEB]{C_RESET} [{i}/{len(final_results)}] {title[:50]}...")

                try:
                    async with session.get(
                        url,
                        headers={
                            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                            'Accept-Language': 'en-US,en;q=0.9',
                            'Accept-Encoding': 'gzip, deflate',
                        },
                        allow_redirects=True,
                        max_redirects=2
                    ) as resp:

                        content_type = resp.headers.get('content-type', '').lower()
                        if 'text/html' not in content_type:
                            all_texts.append(f"=== Источник {i}: {title} ===\n[Не HTML: {content_type}]")
                            continue

                        html = await resp.text(errors='replace')

                    # Парсинг
                    soup = BeautifulSoup(html, "html.parser")

                    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form", "iframe", "noscript"]):
                        tag.decompose()

                    # Ищем контент
                    main_content = (
                        soup.find('main') or
                        soup.find('article') or
                        soup.find('div', class_=re.compile('content|main|article|post')) or
                        soup.find('div', id=re.compile('content|main|article'))
                    )

                    if main_content:
                        text = main_content.get_text(separator="\n", strip=True)
                    else:
                        text = soup.get_text(separator="\n", strip=True)

                    # ФИЛЬТРУЕМ китайские символы из текста
                    lines = []
                    for line in text.splitlines():
                        line = line.strip()
                        # Пропускаем строки с большим количеством китайских символов
                        chinese_chars = sum(1 for char in line if '\u4e00' <= char <= '\u9fff')
                        if chinese_chars > len(line) * 0.3:  # Если >30% китайских символов
                            continue
                        if line and len(line) > 20:
                            lines.append(line)

                    text = "\n".join(lines[:50])  # Увеличено с 25 до 50 строк

                    if len(text) < 50:
                        all_texts.append(f"=== Источник {i}: {title} ===\n[Содержимое слишком короткое]")
                        continue

                    all_texts.append(f"=== Источник {i}: {title} ({url}) ===\n{text[:2500]}")  # Увеличено с 1800 до 2500
                    print(f"{C_GREEN}✓{C_RESET} {len(text)} символов")

                except asyncio.TimeoutError:
                    print(f"{C_YELLOW}⚠{C_RESET} Таймаут (пропускаем)")
                    # НЕ добавляем в all_texts, просто пропускаем
                    continue
                except aiohttp.ClientError as e:
                    print(f"{C_YELLOW}⚠{C_RESET} Ошибка соединения (пропускаем)")
                    continue
                except Exception as e:
                    print(f"{C_YELLOW}⚠{C_RESET} Ошибка обработки (пропускаем)")
                    continue

        if not all_texts:
            return "Не удалось получить данные с сайтов (все источники недоступны или по таймауту)."

        combined = "\n\n".join(all_texts)
        if len(combined) > WEB_SEARCH_MAX_LENGTH:
            combined = combined[:WEB_SEARCH_MAX_LENGTH] + f"\n\n... [Обрезано до {WEB_SEARCH_MAX_LENGTH} символов]"

        print(f"{C_GRAY}[WEB]{C_RESET} Итого возвращаю: {len(combined)} символов из {len(all_texts)} источников")
        return combined

    except Exception as e:
        error_msg = f"Критическая ошибка поиска: {type(e).__name__}: {str(e)}"
        print(f"{C_RED}[ERROR]{C_RESET} {error_msg}")
        return error_msg

def print_header():
    print(f"\n{C_GRAY}{'='*60}{C_RESET}")
    print(f"{C_BLUE}🛠  AI Project Manager v5.5{C_RESET} {C_GRAY}|{C_RESET} Smart Search")
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
    print(f"  {C_YELLOW}/dialog_status{C_RESET}                 {C_GRAY}Показать статус диалога{C_RESET}")
    print(f"  {C_YELLOW}/dialog_clean{C_RESET}                  {C_GRAY}Очистить историю диалога{C_RESET}")
    print(f"  {C_YELLOW}/close{C_RESET}                         {C_GRAY}Сохранить и выйти{C_RESET}")
    print(f"  {C_YELLOW}/exit{C_RESET}                          {C_GRAY}Выход{C_RESET}")
    print(f"  {C_YELLOW}/ant <question>{C_RESET}              {C_GRAY}Диалог через Anthropic SDK (как ant.py){C_RESET}")
    print(f"\n{C_GRAY}Настройки поиска: {WEB_SEARCH_MAX_RESULTS} сайтов, {WEB_SEARCH_MAX_LENGTH} символов, таймаут {WEB_SEARCH_TIMEOUT}с{C_RESET}")
    print(f"{C_GRAY}Настройки диалога: макс. итераций {DIALOG_MAX_ITERATIONS}{C_RESET}")
    print()
