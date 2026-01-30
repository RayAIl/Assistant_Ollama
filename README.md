```markdown
# 🛠️ AI Project Manager 5.5v

**AI Project Manager** — это автономный локальный агент для разработки, отладки и научных исследований. Система сочетает в себе возможности AI-разработчика (пишет код, тестирует, ищет ошибки), AI-исследователя (читает PDF, находит формулы, анализирует документацию) и AI-верификатора фактов (проверяет информацию через веб-поиск).

Это не просто чат-бот, а полноценный менеджер проектов с **постоянной памятью (PostgreSQL)** и **контекстным буфером (Redis)**.

![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-18+-blue.svg)
![Ollama](https://img.shields.io/badge/Ollama-GLM--4-brightgreen.svg)
![Redis](https://img.shields.io/badge/Redis-7.0+-red.svg)
![RAG](https://img.shields.io/badge/Support-RAG_Ready-success.svg)

## 🚀 Возможности

*   **Режим Разработчика (`/dev`):** Автоматический цикл: Скан проекта → План → Код → Тесты → Дебаг. Агент самостоятельно читает файлы, правит `Cargo.toml`, запускает `cargo build` и исправляет ошибки.
*   **Режим Анализа (`/analyze`):** Подготовка архитектуры и промптов перед написанием кода.
*   **Режим Fact-Checking (`/dialog_web`):** Отдельный агент для общения с пользователем, который *обязан* искать информацию в интернете перед ответом. Идеален для проверки версий библиотек, новостей и технических фактов.
*   **Интеграция Anthropic SDK (`/ant`):** Прямой диалог с моделями Anthropic (Claude) через их SDK, если Ollama недоступен.
*   **Code Review & Explain:** Команды для ревью кода (`/review`) и подробного объяснения (`/explain`).
*   **RAG (Retrieval-Augmented Generation):** Агент умеет подключать внешние папки с документацией (PDF, DOCX, TXT). Использует `ripgrep-all` для поиска внутри PDF.
*   **Sandbox (Изоляция):** Агент работает строго внутри указанной директории проекта. Блокировка выхода за пределы папки.
*   **Умный Веб-Поиск:** Интегрированный DuckDuckGo с фильтрацией мусорных сайтов, приоритетом официальной документации и поддержкой `proxychains`.

## 📋 Требования

*   **OS:** Linux (Gentoo, Arch, Ubuntu, WSL).
*   **Python:** 3.11+
*   **RAM:** Рекомендуется 16GB+ (для комфортной работы моделей).
*   **Services:**
    *   Ollama (сервер LLM).
    *   PostgreSQL 18+.
    *   Redis 7+ (для истории диалогов).

## 🛠 Установка и Настройка

### 1. Клонирование и Виртуальное окружение

```bash
git clone <repo_url> ai_pm
cd ai_pm

# Создай виртуальное окружение
python3 -m venv .venv
source .venv/bin/activate
```

### 2. Установка Python зависимостей

```bash
pip install -r requirements.txt
```

### 3. Настройка Ollama

1.  **Установка:**
    ```bash
    curl -fsSL https://ollama.com/install.sh | sh
    ```
2.  **Запуск сервера:**
    ```bash
    ollama serve &
    ```
3.  **Загрузка модели:**
    По умолчанию используется `glm-4.7-flash` (быстрая и умная).
    ```bash
    ollama pull glm-4.7-flash
    ```

### 4. Настройка Redis

Redis используется для хранения истории диалогов в реальном времени.

1.  **Установка:**
    ```bash
    sudo emerge dev-db/redis  # Gentoo
    sudo pacman -S redis      # Arch
    ```
2.  **Запуск:**
    ```bash
    sudo /etc/init.d/redis start
    sudo rc-update add redis default
    ```

### 5. Настройка PostgreSQL

1.  **Установка и Инициализация:**
    ```bash
    sudo emerge --ask dev-db/postgresql
    sudo emerge --config dev-db/postgresql:18
    ```
2.  **Запуск службы:**
    ```bash
    sudo /etc/init.d/postgresql-18 start
    sudo rc-update add postgresql-18 default
    ```
3.  **Создание Базы и Пользователя:**
    ```bash
    sudo -u postgres psql
    ```
    ```sql
    CREATE USER ai_agent WITH PASSWORD 'password';
    CREATE DATABASE ai_projects OWNER ai_agent;
    GRANT ALL PRIVILEGES ON DATABASE ai_projects TO ai_agent;
    \q
    ```

> **Примечание:** Скрипт автоматически создаст необходимые таблицы (`projects`, `project_messages`) при первом запуске.


### Консольные команды

| Команда | Описание | Пример |
| :--- | :--- | :--- |
| **Управление Проектами** |||
| `/info` | Вызывает панель справки. | |
| `/create <name> <path> <goal>` | Создает проект, привязывает путь и цель. | `/create xtts ~/code "TTS system"` |
| `/list` | Список всех проектов и их статусов. | `/list` |
| `/load <name>` | Загружает проект в активную сессию. | `/load xtts` |
| `/delete <name>` | Удаляет проект из БД. | `/delete old_app` |
| `/close` | Закрывает проект, сохраняет прогресс в БД. | `/close` |
| **Режимы Работы** |||
| `/dev` | **Главный режим.** Запускает цикл разработки: анализ → план → код. | `/dev` |
| `/analyze` | Переключает статус проекта на "analysis". Подготовка этапа. | `/analyze` |
| **Работа с Документацией** |||
| `/doc <path>` | Привязывает папку с документацией (PDF) к проекту. | `/doc /home/books/rust` |
| `/doc_del` | Удаляет привязку документации. | `/doc_del` |
| **Планирование и Архитектура** |||
| `/analyze_prompt <text>` | Сохраняет текстовую цель/промпт проекта. | `/analyze_prompt "Fix errors"` |
| `/architect <text>` | Сохраняет описание архитектуры проекта. | `/architect "Client-Server"` |
| **Инструменты Кода** |||
| `/review <file>` | Делает Code Review указанного файла. | `/review src/main.rs` |
| `/explain <file>` | Объясняет код файла построчно. | `/explain src/lib.rs` |
| **Диалог и Исследования** |||
| `/dialog_web [question]` | Запускает режим "Web-верификатор". ИИ ищет факты перед ответом. | `/dialog_web` |
| `/dialog_status` | Показывает статус истории диалога в Redis. | `/dialog_status` |
| `/dialog_clean` | Очищает историю глобального диалога. | `/dialog_clean` |
| `/ant [question]` | Режим прямого диалога через Anthropic SDK (Claude). | `/ant` |
| **Система** |||
| `/exit` | Завершает работу скрипта. | `/exit` |

## 🧠 Логика работы (Workflow)

### 1. Режим Разработки (`/dev`)
Это основной цикл (`agent_loop`).
1.  **Сканирование:** Агент вызывает `scan_directory`. Он получает список **реальных** файлов (main.rs, Cargo.toml).
2.  **Контекст:** Если есть привязанная документация (`/doc`), он ищет ответы там (`search_docs`).
3.  **Интернет:** Если нужна свежая инфа (версии crate) — ищет в вебе (`web_search`).
4.  **План:** Создает план действий и вызывает `update_project_plan`.
5.  **Код:** Пишет/изменяет файлы (`write_file`).
6.  **Тест:** Запускает `cargo check` или `python script.py` (`run_shell_command`).
7.  **Цикл:** Если есть ошибки — возвращается к шагу 5 или 2.

### 2. Режим Диалога (`/dialog_web`)
Это отдельный агент с промптом `SYSTEM_PROMPT_DIALOG_WEB`.
*   **Принцип:** "Не верь своей памяти". На любой вопрос агент **обязан** сделать `web_search`.
*   **Применение:** Проверка новостей, актуальных версий библиотек, технических спорных моментов.
*   **Фильтрация:** Агент настроен игнорировать мусорные сайты и форумы, приоритет отдает официальной документации.

## ⚙️ Конфигурация

Параметры задаются в `config.py`.

```python
# --- БАЗА ДАННЫХ ---
DB_NAME = "ai_projects"
DB_USER = "ai_agent"
DB_PASS = "password"

# --- OLLAMA ---
OLLAMA_MODEL = "glm-4.7-flash"  # Модель по умолчанию
OLLAMA_HOST = "http://localhost:11434"
OLLAMA_TIMEOUT = 9600  # Таймаут в секундах

# --- ANTHROPIC (для /ant) ---
ANTHROPIC_BASE_URL = "http://localhost:11434" # Можно использовать прокси к Claude
ANTHROPIC_API_KEY = "sk-ant-..."

# --- REDIS ---
REDIS_HOST = "localhost"
REDIS_PORT = 6379

# --- ПОИСК ---
WEB_SEARCH_MAX_RESULTS = 10      # Сколько сайтов парсить
WEB_SEARCH_MAX_LENGTH = 50000    # Лимит текста с сайтов
WEB_SEARCH_TIMEOUT = 50           # Таймаут на один сайт

# --- АГЕНТ ---
MAX_ITERATIONS = 14              # Макс. шагов (План->Код->Ошибки) за сессию
MAX_DB_HISTORY = 50              # Сколько сообщений из БД загружать в контекст
```

## ⚠️ Предупреждения

1.  **Безопасность Shell:** Агент запускает команды (`run_shell_command`) от вашего пользователя. Не давайте ему права `root`. Песочница (`get_full_path`) запрещает `../` выходы, но `rm -rf .` внутри проекта работает.
2.  **Галлюцинации:** LLM может ошибаться. Всегда проверяйте сгенерированный код, особенно в режиме `/dev`.
3.  **Контекст:** Если проект огромен (1000+ файлов), агент может "переварить" не всё. Используйте `/explain` для конкретных файлов.
4.  **Proxy:** Если вы используете `proxychains`, запустите скрипт через него: `proxychains python main.py`.

## 📝 Лицензия

MIT License.
```
