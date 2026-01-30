import asyncio
import bd
from tools import *
from config import *

# --- MAIN CLI ---

async def main() -> None:
    """Главная CLI-функция"""
    global ACTIVE_PROJECT, r

    if not await bd.init_db():
        print(f"{C_RED}Ошибка инициализации БД. Выход.{C_RESET}")
        return

    if not await bd.init_redis():
        print(f"{C_YELLOW}Предупреждение: Redis недоступен. История не будет сохранена.{C_RESET}")

    if not await bd.init_ollama():
        print(f"{C_RED}Ошибка подключения к Ollama. Выход.{C_RESET}")
        return

    print_header()
    print_help()

    try:
        while True:
            try:
                prompt_proj: str = f"{C_CYAN}[{bd.ACTIVE_PROJECT['name']}]{C_RESET} " if bd.ACTIVE_PROJECT else ""
                user_input: str = input(f"{C_YELLOW}➜ {C_RESET}{prompt_proj}")
            except (EOFError, KeyboardInterrupt):
                break

            if not user_input.strip():
                continue

            parts:list[str] = user_input.split()
            cmd: str = parts[0]

            match cmd:
                case "/exit":
                    break

                case "/close":
                    if bd.ACTIVE_PROJECT:
                        await bd.sync_redis_to_db(project_id=bd.ACTIVE_PROJECT["id"])
                        await bd.update_project_fields(fields={"status": "closed"})
                        name = bd.ACTIVE_PROJECT["name"]
                        bd.ACTIVE_PROJECT = None
                        print(f"{C_GREEN}[CLOSED]{C_RESET} Проект '{name}' сохранен.")
                    else:
                        print(f"{C_GRAY}Нет активного проекта.{C_RESET}")
                    continue

                case "/list":
                    projs = await bd.get_all_projects()
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
                    args_text: str = " ".join(parts[1:])
                    # Формат: /create имя путь "цель с пробелами"
                    # Регулярка для парсинга с учётом кавычек
                    match_args = re.match(r'(\S+)\s+(\S+)\s+"([^"]+)"', args_text) or re.match(r'(\S+)\s+(\S+)\s+(.*)', args_text)
                    if match_args:
                        name = match_args.group(1)
                        path = match_args.group(2)
                        goal = match_args.group(3)

                        # Проверяем существование пути
                        if not os.path.exists(path):
                            print(f"{C_RED}[ERROR]{C_RESET} Путь не существует: {path}")
                            continue

                        if await bd.create_project(name, path, goal):
                            await bd.load_project(name)
                            print(f"{C_GREEN}✅{C_RESET} Проект создан и загружен. Путь: {C_CYAN}{path}{C_RESET}")
                    else:
                        print(f"{C_RED}[ERROR]{C_RESET} Формат: {C_YELLOW}/create имя путь цель{C_RESET}")
                        print(f"{C_GRAY}Пример: /create myapp /path/to/app \"Описание цели\"{C_RESET}")
                    continue

                case "/load":
                    if len(parts) > 1:
                        await bd.load_project(parts[1])
                    else:
                        print(f"{C_RED}[ERROR]{C_RESET} Укажите имя проекта.")
                    continue

                case "/delete":
                    if len(parts) > 1:
                        name: str = parts[1]
                        if bd.ACTIVE_PROJECT and bd.ACTIVE_PROJECT.get("name") == name:
                            bd.ACTIVE_PROJECT = None
                        await bd.delete_project(name)
                    else:
                        print(f"{C_RED}[ERROR]{C_RESET} Укажите имя проекта для удаления.")
                    continue

                case "/doc":
                    if not bd.ACTIVE_PROJECT:
                        print(f"{C_RED}[ERROR]{C_RESET} Нет проекта.{C_RESET}")
                        continue
                    if len(parts) > 1:
                        doc_path: str = parts[1]
                        if os.path.isdir(s=doc_path):
                            await bd.update_project_fields(fields={"doc_path": doc_path})
                            print(f"{C_GREEN}[OK]{C_RESET} Каталог документации привязан: {doc_path}")
                        else:
                            print(f"{C_RED}[ERROR]{C_RESET} Укажите существующий каталог.")
                    else:
                        print(f"{C_RED}[ERROR]{C_RESET} Укажите путь к каталогу документации.")
                    continue

                case "/doc_del":
                    if not bd.ACTIVE_PROJECT:
                        continue
                    if await bd.update_project_fields(fields={"doc_path": None}):
                        print(f"{C_GREEN}[OK]{C_RESET} Путь к документации удален.")
                    else:
                        print(f"{C_RED}[ERROR]{C_RESET} Не удалось удалить путь к документации.")
                    continue

                case "/analyze":
                    if not bd.ACTIVE_PROJECT:
                        print(f"{C_RED}[ERROR]{C_RESET} Нет проекта.{C_RESET}")
                        continue
                    await bd.update_project_fields(fields={"status": "analysis"})
                    print(f"{C_BLUE}[MODE]{C_RESET} Режим Анализа. Используйте /analyze_prompt или /architect.")
                    continue

                case "/analyze_prompt":
                    if not bd.ACTIVE_PROJECT:
                        continue
                    if len(parts) > 1:
                        prompt_text: str = " ".join(parts[1:])
                        await bd.update_project_fields(fields={"final_prompt": prompt_text})
                        print(f"{C_GREEN}[OK]{C_RESET} Промпт сохранен.")
                    else:
                        print(f"{C_RED}[ERROR]{C_RESET} Укажите текст промпта.")
                    continue

                case "/architect":
                    if not bd.ACTIVE_PROJECT:
                        continue
                    if len(parts) > 1:
                        arch_text: str = " ".join(parts[1:])
                        await bd.update_project_fields(fields={"architecture": arch_text})
                        print(f"{C_GREEN}[OK]{C_RESET} Архитектура сохранена.")
                    else:
                        print(f"{C_RED}[ERROR]{C_RESET} Укажите описание архитектуры.")
                    continue

                case "/dev":
                    if not bd.ACTIVE_PROJECT:
                        continue
                    await bd.update_project_fields(fields={"status": "active"})
                    print(f"{C_GREEN}[MODE]{C_RESET} Режим Разработки.")
                    await bd.agent_loop(user_input="Проанализируй Промпт и Архитектуру, создай план и начни разработку.")
                    continue

                case "/review":
                    if not bd.ACTIVE_PROJECT:
                        continue
                    if len(parts) > 1:
                        await bd.agent_loop(user_input=f"Сделай Code Review файла {parts[1]}. Найди ошибки и уязвимости.", mode="review")
                    else:
                        print(f"{C_RED}[ERROR]{C_RESET} Укажите файл для ревью.")
                    continue

                case "/explain":
                    if not bd.ACTIVE_PROJECT:
                        continue
                    if len(parts) > 1:
                        await bd.agent_loop(user_input=f"Объясни файл {parts[1]} построчно.", mode="explain")
                    else:
                        print(f"{C_RED}[ERROR]{C_RESET} Укажите файл для объяснения.")
                    continue

                case "/dialog_web":
                    global DIALOG_MODE
                    question: str = " ".join(parts[1:]) if len(parts) > 1 else ""
                    if not question:
                        print(f"{C_BLUE}[DIALOG]{C_RESET} Режим свободного диалога активирован.")
                        print(f"{C_GRAY}История сохраняется в Redis. Введите сообщение для общения или 'выход' для завершения.{C_RESET}")
                        DIALOG_MODE = True
                        continue
                    else:
                        await dialog_web_loop(user_input=question)
                    continue

                case "/dialog_status":
                    status: str = await get_dialog_status()
                    print(status)
                    continue

                case "/dialog_clean":
                    result: str = await clean_dialog_history()
                    print(result)
                    continue

                case "/info":
                    print_help()
                    continue

                case "/ant":
                    question: str = " ".join(parts[1:]) if len(parts) > 1 else ""
                    if not question:
                        print(f"{C_CYAN}[ANT]{C_RESET} Режим прямого диалога (Anthropic SDK)")
                        print(f"{C_GRAY}Модель: {ANTHROPIC_MODEL} | URL: {ANTHROPIC_BASE_URL}{C_RESET}")
                        while True:
                            try:
                                user_q: str = input(f"{C_YELLOW}ant> {C_RESET}")
                                if user_q.lower() in ["exit", "quit", "/exit"]:
                                    break
                                if user_q.strip():
                                    await bd.stream_anthropic(user_input=user_q)
                            except (KeyboardInterrupt, EOFError):
                                break
                        print(f"\n{C_GRAY}[ANT] Диалог завершен{C_RESET}")
                    else:
                        await bd.stream_anthropic(user_input=question)
                    continue

                case _:
                    # Если активен режим диалога
                    if DIALOG_MODE:
                        if user_input.lower() in ["выход", "exit", "стоп", "quit", "/exit_dialog"]:
                            print(f"{C_BLUE}[DIALOG]{C_RESET} Диалог завершен.")
                            DIALOG_MODE = False
                            continue
                        await dialog_web_loop(user_input)
                        continue

                    # Иначе работа с проектом
                    if not bd.ACTIVE_PROJECT:
                        print(f"{C_GRAY}Нет проекта. Создайте или загрузите.{C_RESET}")
                        continue

                    mode = "analyzer" if bd.ACTIVE_PROJECT.get("status") == "analysis" else "dev"
                    await bd.agent_loop(user_input, mode=mode)

    finally:
        if bd.ACTIVE_PROJECT:
            await bd.sync_redis_to_db(project_id=bd.ACTIVE_PROJECT["id"])
            print(f"{C_GRAY}💾{C_RESET} Проект сохранен.")
        if bd.r:
            await bd.r.aclose()


if __name__ == "__main__":
    try:
        asyncio.run(main=main())
    except KeyboardInterrupt:
        print(f"\n{C_GRAY}👋 До свидания!{C_RESET}")
