---
title: GigaCode CLI Runtime (optional)
sidebar_label: GigaCode CLI Runtime
---

# GigaCode CLI как основной исполнитель Hermes

Режим `api_mode: gigacode_cli` передаёт **каждый ход целиком** агенту GigaCode CLI. Hermes остаётся
оболочкой: доступ, сессии и история, память, расписания (cron), выдача прав на инструменты. GigaCode
сам ведёт цикл рассуждения и вызывает инструменты Hermes через MCP-мост, который живёт внутри
процесса Hermes ровно один запуск.

Режим **выключен по умолчанию** и не влияет на остальные провайдеры. В этом режиме Hermes не создаёт
клиент модели, не вызывает старого провайдера для заголовков, сжатия, обзора памяти или
делегирования и **никогда не переключается на другой провайдер** при ошибке GigaCode.

:::warning Что проверено, а что нет
Поставка содержит программный контракт (`offline_contract`), проверенный автономными тестами с
Python-имитатором CLI и настоящим HTTP/MCP-обменом на loopback. **Совместимость конкретной
корпоративной сборки GigaCode, авторизация, барьер ОС (bubblewrap) и работа в боевом Telegram
не проверены** (`live_activation`). Их подтверждает оператор испытаниями и фиксирует в манифесте;
без манифеста настоящий CLI не запускается даже для текстового запроса.
:::

## Как устроен ход

1. Gateway принимает сообщение; общая подготовка Hermes создаёт строку сессии и сохраняет
   сообщение пользователя.
2. Runtime вычисляет **ключ запроса** `(profile, channel, user, request_id)` и записывает его в
   журнал до запуска процесса. Для Telegram `channel = telegram:<id бота>`, `request_id = update_id`;
   повторная доставка того же update находит прежний запуск и **не исполняет его повторно**.
3. Очередь: по умолчанию один процесс одновременно, до 32 ожидающих запросов (дальше —
   `runtime_busy`), одна сессия всегда выполняется последовательно.
4. Перед запуском проверяется манифест (`config_unverified` при любом несовпадении).
5. Из истории Hermes строится пакет контекста (stdin): правила исполнения, системные инструкции
   (включая `SOUL.md` и память профиля), данные сессии, целые последние ходы, текущий запрос.
   Старые ходы отбрасываются целиком; если обязательная часть не помещается в
   `prompt_budget_tokens` — `context_too_large` (байт UTF-8 считается за токен).
6. Поднимается MCP-мост на `127.0.0.1:<случайный порт>` с одноразовым Bearer-токеном; запускается
   `gigacode` в песочнице bubblewrap; поток `stream-json` разбирается строгим парсером.
7. После завершения токен отзывается, каталог запуска удаляется, история (пары вызов/результат и
   финальный ответ) сохраняется **один раз** штатным механизмом Hermes; только после этого запуск
   получает состояние `succeeded`.

Запуск CLI фиксированный, без shell:

```text
gigacode --output-format stream-json --mcp-config /run/hermes/mcp.json
         --allowed-mcp-server-names hermes --allowed-tools mcp__hermes
         --approval-mode=auto-edit [--model <проверенный id>]
```

## Конфигурация

Секция `gigacode:` в `config.yaml` профиля (секреты в неё не пишутся; `.env` не используется):

```yaml
model:
  provider: gigacode-cli
  api_mode: gigacode_cli
gigacode:
  executable: /opt/gigacode/bin/gigacode     # путь ВНУТРИ runtime_rootfs
  verified_version: "1.2.3"                  # из испытаний; маркер REQUIRED_FROM_PREFLIGHT недопустим
  model: null                                # null = авторизованная модель CLI (фиксируется манифестом)
  max_concurrent_runs: 1
  wall_timeout_seconds: 1800
  silence_warning_seconds: 180               # только предупреждение в логе, не остановка
  termination_grace_seconds: 5
  stream_protocol: maestro_stream_json_v1
  tool_policy: owner_private
  prompt_budget_tokens: 120000               # проверенное окно модели минус резерв на инструменты и ответ
  verification_manifest: /srv/gigacode/manifest.json
  execution_driver: bubblewrap
  runtime_rootfs: /srv/gigacode/rootfs       # проверенный read-only bundle с CLI и его runtime
  bwrap_executable: /usr/bin/bwrap
  auth_bundle: /srv/gigacode/auth            # только файлы учётных данных из allowlist манифеста
  skills_allowlist: []                       # навыки, доступные через hermes_skill_view
  named_subagents: false                     # в этой версии только false
  fork_subagents: false                      # в этой версии только false
  fallback: disabled                         # в этой версии только disabled
```

Неверный тип или значение — `config_invalid` до запуска процесса. Незаполненные операторские значения
(`REQUIRED_FROM_PREFLIGHT`, пустые пути) — `config_unverified`. Переключателя «режим имитатора» в
конфиге нет: имитатор подключается только внедрением зависимостей в тестах.

## Подготовка к активации

### 1. Резервная копия

Перед любыми изменениями — timestamped backup в `~/hermes-backups/`: весь `~/.hermes/` (конфиг,
`.env`, `SOUL.md`, память, сессии, навыки, профили, cron, состояние gateway, нужные логи). SQLite
копируйте при остановленных писателях (`hermes gateway stop`) или через backup API SQLite. Рядом
сохраните версии Hermes и GigaCode и эту инструкцию. Отдельно — защищённое состояние GigaCode
(`auth_bundle`). Секреты в спецификации и манифесте не хранятся.

### 2. rootfs и песочница

`runtime_rootfs` — read-only каталог с CLI, его Node/runtime-зависимостями, системными
библиотеками, CA и служебными файлами. В нём **не должно быть** домашней директории Hermes,
`.hermes`, секретов, сокетов управления. В rootfs заранее создаются пустые точки монтирования:
каталоги `/work`, `/home/gigacode/.gigacode`, `/run/hermes`, `/tmp`, `/proc`, `/dev` и пустой файл <!-- no-tmp: ok — documents the sandbox's own mount points -->
`/run/hermes/mcp.json`. Hermes не создаёт и не меняет их после проверки хеша.

Песочница: `bwrap --unshare-user --unshare-pid --unshare-ipc --unshare-uts --new-session
--die-with-parent --cap-drop ALL`, rootfs только для чтения, свой `/proc` и `/dev`, tmpfs `/tmp`, <!-- no-tmp: ok — documents the sandbox's own mount points -->
`/work` — отдельная записываемая папка запуска; `settings.json`, `GIGACODE.md`, `mcp.json` и
пользовательский scope с учётными данными монтируются только для чтения. Окружение процесса
собирается с нуля (нет `.env` Hermes, токена Telegram, ключей других сервисов).

:::caution Граница этой версии: сеть
Песочница сохраняет сеть хоста (нужна для endpoint модели и MCP-моста). Все сервисы на loopback
и abstract AF_UNIX сокеты **достижимы** из песочницы. Перед активацией проинвентаризируйте их из
песочницы, убедитесь, что у каждого есть собственная аутентификация, и отметьте это в манифесте
(`os_isolation.loopback_inventory_reviewed`). Неаутентифицированный интерфейс управления блокирует
активацию.
:::

### 3. Auth bundle

`auth_bundle` содержит **только** файлы учётных данных выбранной сборки. Каждый файл перечисляется
в манифесте с размером и SHA-256. Любой лишний или изменённый файл, symlink, `settings.json`,
MCP-конфиги, `extensions/`, `commands/`, `agents/`, `skills/`, файлы памяти и инструкций —
`config_unverified`. Если CLI нужно обновить токен (запись в read-only каталог), запуск
завершается `auth_refresh_required`: обновите bundle и хеши в манифесте отдельной операторской
процедурой.

### 4. Манифест верификации

```bash
hermes gigacode preflight     # хеши rootfs, CLI, bwrap, политики и файлов auth bundle
```

Команда ничего не запускает. Скопируйте вывод `facts` в манифест и дополните результатами
испытаний **конкретной сборки** (не `--help`):

```json
{
  "schema": "hermes-gigacode-manifest/1",
  "verified_at": "2026-10-10T12:00:00Z",
  "stream_protocol": "maestro_stream_json_v1",
  "policy_sha256": "<из preflight>",
  "cli": {"executable": "/opt/gigacode/bin/gigacode", "sha256": "<из preflight>", "version": "1.2.3"},
  "rootfs": {"path": "/srv/gigacode/rootfs", "tree_sha256": "<из preflight>",
             "system_settings_paths": ["/etc/gigacode/settings.json"]},
  "bwrap": {"executable": "/usr/bin/bwrap", "sha256": "<из preflight>"},
  "auth_bundle": {"files": [{"path": "oauth_creds.json", "size": 1234, "sha256": "<из preflight>"}]},
  "model": {"id": "<фактическая модель>", "provider": "<провайдер>", "endpoint": "<допустимый endpoint>"},
  "mcp": {"flags_verified": true, "native_deny_verified": true, "qualified_tool_prefix": "mcp__hermes__"},
  "os_isolation": {"checks_passed": true, "loopback_inventory_reviewed": true, "cleanup_verified": true}
}
```

Что подтвердить испытаниями перед установкой флагов в `true`:

- реальный поток `stream-json` сборки соответствует профилю (события `system/init`, `assistant`,
  `user`, `result`; формат qualified-имён MCP-инструментов → `qualified_tool_prefix`);
- флаги запуска работают, ошибка флага/авторизации даёт явную ошибку без зависания;
- запреты нативных инструментов (`tools.exclude` / `excludeTools`, см. ниже) действительно
  применяются: попытки shell/файлов/сети/памяти отклоняются; чужие MCP и расширения не подхватываются;
- все system-scope файлы настроек сборки внутри rootfs перечислены в `system_settings_paths`
  (они должны отсутствовать или совпадать со сгенерированной политикой);
- песочница не даёт прочитать `.env` Hermes, токен Telegram и чужие файлы; отмена завершает все
  процессы запуска, включая потомков с `setsid` (`cleanup_verified`);
- фактическая модель и endpoint совпадают с допустимыми, скрытого fallback нет.

```bash
hermes gigacode verify        # та же проверка, что перед каждым настоящим запуском
```

Любое изменение rootfs, бинарника, bwrap, политики инструментов или модели меняет хеши — нужен
новый прогон испытаний и новый манифест.

## Активация

1. Тестовый профиль: `hermes -p gigatest config edit` → `model.provider/api_mode` и секция
   `gigacode:`; `hermes -p gigatest gigacode verify`.
2. Один личный чат allowlisted-владельца. Группы — только после отдельной проверки изоляции
   (`group_sessions_per_user: true`, `telegram.require_mention: true`, два реальных пользователя).
3. `hermes gateway restart`, отправить простой вопрос, затем `hermes gigacode runs list` /
   `runs show <run_id>`: состояние `succeeded`, `token_revoked: 1`, `cleanup_state: complete`.

Не совмещайте первое переключение с обновлением Hermes.

## Инструменты и права

GigaCode видит только MCP-сервер `hermes` и только выданные инструменты под **wire-именами** с
префиксом `hermes_` (они не совпадают с запрещёнными нативными именами):

| Wire-имя | Обработчик Hermes | `owner_private` по умолчанию |
| --- | --- | --- |
| `hermes_web_search`, `hermes_web_extract` | `web_search`, `web_extract` (штатные сетевые ограничения) | да |
| `hermes_memory_read` | чтение памяти текущего профиля | да |
| `hermes_session_search` | `session_search`, только собственные сессии того же профиля | да |
| `hermes_skills_list`, `hermes_skill_view` | только навыки из `skills_allowlist`, без запуска скриптов | да |
| `hermes_memory` (add/replace/remove) | `memory` | **нет** — только operator grant |
| `hermes_read_file`, `hermes_write_file` | безопасный доступ внутри выданного корня | **нет** — только operator grant |
| терминал, установка пакетов, делегирование | — | не выдаются |

Инструмент выдаётся, только если он включён у агента Hermes этого профиля. Каждый вызов проходит
`authorize → validate_arguments → audit_start → dispatch_with_context → audit_finish`; контекст
прав задаётся на сервере и не принимается из аргументов. Файловые операции открывают путь
покомпонентно от дескриптора корня с `O_NOFOLLOW`: `..`, symlink и подмена пути отклоняются.

Нативные инструменты GigaCode запрещаются сгенерированными `settings.json` (оба поля,
`tools.exclude` и `excludeTools`, получают одинаковый объединённый список: файлы, shell, сеть,
изменение MCP, собственная память, computer-use, вопросы пользователю, навыки). Неизвестный новый
инструмент в каталоге `system/init` или вызов невыданного инструмента прерывает запуск
(`tool_policy_violation`).

### Одноразовые операторские права

Только локальный оператор профиля (право записи в его `config.yaml`); из Telegram не выдаются:

```bash
# запись в память для следующего нового личного сообщения владельца, на 120 секунд
hermes gigacode grants issue --profile default --channel telegram:<id бота> --user telegram:<id владельца> \
    --tool hermes_memory --actions add --next-request --ttl 120

# запись файла в тестовый каталог для конкретного запроса (ключ из `runs show`)
hermes gigacode grants issue --profile default --channel telegram:<id бота> --user telegram:<id> \
    --tool hermes_write_file --actions write --request-key '["default","telegram:<бот>","telegram:<id>","<update_id>"]' \
    --ttl 300 --path-root /srv/gigacode-test

hermes gigacode grants list --profile default
hermes gigacode grants revoke <grant-id>
```

Правила: `--ttl` от 1 до `wall_timeout_seconds`; без wildcard и нескольких пользователей; нельзя
выдать права уже запущенному или завершённому запросу; на одну область и инструмент — не более
одного активного `--next-request`. Grant потребляется атомарно при выборе запроса из очереди и
больше не возвращается, даже если запуск не удался. Каждый вызов повторно проверяет срок и отзыв.
В аудите запись памяти помечается флагом `untrusted_input_before`, если ей предшествовал
результат web/extract/session_search.

## Журнал и диагностика

```bash
hermes gigacode runs list
hermes gigacode runs show <run_id>    # состояние, ошибка, процесс, usage, аудит (хеши аргументов)
```

Журнал: `<HERMES_HOME>/gigacode/journal.db` (отдельный файл; `state.db` не мигрируется).
Состояния: `queued → running → succeeded | failed | cancelled | timed_out`, `recovery_required`,
после решения оператора — `resolved_succeeded | resolved_failed | abandoned`.

| `error.kind` | Значение |
| --- | --- |
| `config_invalid` / `config_unverified` | ошибка конфига / нет действующего манифеста; CLI не запускался |
| `context_too_large` | обязательная часть пакета не помещается в бюджет |
| `runtime_busy` | очередь (32) заполнена |
| `unsupported_attachment` | изображения, файлы, голос в этой версии не поддерживаются |
| `session_recovery_required` | в сессии есть неразобранный `recovery_required` запуск |
| `duplicate_request` | повторная доставка того же запроса; исполнения не было |
| `cli_error`, `api_error`, `nonzero_exit` | ошибка CLI/модели, включая «успех» с `[API Error: …]` |
| `protocol_*`, `empty_final_response` | нарушение профиля потока, нет результата, пустой ответ |
| `tool_policy_violation` | CLI объявил или вызвал невыданный инструмент |
| `cancelled`, `timed_out` | отмена пользователем/остановкой или превышение `wall_timeout_seconds` |
| `persist_failed`, `cleanup_incomplete` | ответ получен, но история не сохранилась / дерево процессов не подтверждено остановленным → `recovery_required` |
| `auth_refresh_required` | CLI пытался обновить учётные данные в read-only каталоге |
| `cron_provenance_missing` | у задания cron нет владельца |
| `policy_scope_denied` | `owner_private` работает только в личных сообщениях (не в группах) |
| `restart_before_start` | Hermes перезапустился до старта запроса; отправьте запрос заново |

Пользователь получает безопасный текст и код задачи (`run_id`), не stderr и не сырой поток CLI.
`api_calls` в результате хода равен 0 (Hermes не обращался к модели), `agent_runs` = 1,
`agent_api_calls` = `null` (CLI не сообщает число внутренних вызовов достоверно). Usage берётся
только из итогового события; без него значения неизвестны (`usage_partial` хранится отдельно).
Стоимость показывается, только если её сообщил CLI.

## Восстановление (`recovery_required`)

Пока запуск не разобран, новые сообщения в этой сессии получают ответ: «Предыдущая задача требует
проверки оператором. Код задачи: …». Ничего не повторяется автоматически.

```bash
hermes gigacode runs show <run_id>                      # аудит, очистка, сохранённый ответ
hermes gigacode reconcile <run_id> --decision succeeded --reason "ответ проверен" --dry-run
hermes gigacode reconcile <run_id> --decision succeeded --reason "ответ проверен"
```

- `succeeded` — только при подтверждённом успешном результате CLI; сохранённая история
  записывается в сессию ровно один раз (повтор команды ничего не меняет) → `resolved_succeeded`.
- `failed` → `resolved_failed`; ответ не отправляется как успех.
- `abandoned` — когда аудит или результат повреждены; неизвестные эффекты остаются помеченными и
  передаются в контекст следующих ходов как «не повторять автоматически».

Команда сначала проверяет, что процесс запуска остановлен, а токен отозван, и никогда не вызывает
GigaCode и не отправляет сообщений. После решения сессия снова принимает запросы. После обычного
рестарта Hermes незавершённый `running` становится `recovery_required`, а `queued` —
`failed`/`restart_before_start`.

## Cron

Задания запускаются тем же runtime с политикой задания (`source = cron_job:<id>`). Ключ запроса —
SHA-256 от `job_id:плановое_время_UTC`; ручной запуск использует свой durable execution id.
Владелец задания — пользователь из его `origin` (чат, где задание создано). Старые задания без
`origin` в этом режиме **пропускаются** с ошибкой `cron_provenance_missing` (видна в выводе
задания): пересоздайте задание из личного чата владельца и удалите старое. Действия, требующие
интерактивного подтверждения, в cron не выполняются.

## Откат

1. `hermes gateway stop` (отменяет активные запуски: процессы завершаются, токены отзываются).
2. Вернуть сохранённые `model.provider`/`api_mode` и прежний конфиг.
3. `hermes gateway start`, проверить личное сообщение в Telegram.

Новые сессии и память сохраняются. Полный restore архива — только при повреждении состояния и
после сохранения новых данных для последующего согласования; внешние эффекты архив не откатывает.

## Автономные тесты

```bash
scripts/run_tests.sh tests/agent/gigacode/ tests/hermes_cli/test_gigacode_cmd.py
```

Пакет использует Python-имитатор CLI (`tests/agent/gigacode/fake_gigacode.py`) как отдельный процесс
с теми же аргументами и stdin, настоящий HTTP/MCP-обмен с мостом, настоящий `AIAgent` и `SessionDB`
во временном `HERMES_HOME`. Fixtures потока — синтетические. Результат тестов подтверждает
программный контракт, **но не совместимость реальной сборки GigaCode, модели или изоляции ОС**.

## Ограничения первой версии

- Нет вложений, голоса, изображений; нативные навыки GigaCode, субагенты и `fork` выключены.
- Нет Telegram-протокола подтверждений: действия, требующие подтверждения, не выполняются.
- Каждый запрос — новый процесс GigaCode без `--resume`; история передаётся Hermes заново.
- Ручной `/compress` недоступен; история ограничивается детерминированно в каждом запросе.
- Голосовые сообщения не транскрибируются, изображения не анализируются другой моделью:
  такие запросы получают понятный отказ `unsupported_attachment`.
- Инструменты Hermes не поддерживают ключи идемпотентности: прерванное изменяющее действие
  помечается `unknown` и не повторяется автоматически.
- Сетевая изоляция песочницы не обеспечивается (см. выше).
