# Инструкция для GigaCode: Hermes Agent в закрытом контуре на Linux-ноутбуке

> **Кому:** агенту GigaCode, запущенному прямо на Linux-ноутбуке пользователя, который находится
> в закрытом корпоративном контуре. **Что сделать:** развернуть Hermes Agent из этого форка так,
> чтобы агент выполнял рутинные задачи пользователя, при этом:
>
> - **Hermes работает только на GigaCode**: каждый запрос к модели Hermes отправляет в GigaCode
>   по протоколу ACP (так же, как в Hermes подключаются Copilot и Codex). Своего LLM-ключа у
>   Hermes нет — модель, авторизация и политика остаются у GigaCode;
> - Hermes и все его агенты работают **внутри контейнера Podman**, а не на самом ноутбуке;
> - **GigaCode установлен в тот же контейнер** (никаких SSH-туннелей и сетевых мостов к хосту);
> - образ строится **на корпоративном базовом образе** (RHEL-семейство) **без доступа к сети при
>   сборке**: Python 3.14, колёса зависимостей, Node и GigaCode заранее собираются в «комплект» на
>   ноутбуке через его PyPI- и npm-зеркала; образы Debian и squid не нужны;
> - сеть контейнера **закрыта**: выход только через прокси-контейнер со списком разрешённых
>   ресурсов (allowlist), всё остальное отбрасывается;
> - из файлов ноутбука агенту видны только две выделенные папки.
>
> Пользователь даёт задание примерно так: «Прочитай `GIGACODE_SETUP.md` в форке
> `wild-werewolf/hermes-agent-gigacode` и выполни его».

---

## 0. Правила работы (прочитай до начала)

1. **Выполняй шаги по порядку** и после каждого делай проверку из блока «✅ Проверка». Не иди
   дальше, пока проверка не прошла.
2. **Не нарушай политику контура.** Не поднимай SSH-серверы и туннели, не меняй системные
   настройки сети и файрвола, не отключай корпоративные средства защиты. Всё, что описано ниже,
   делается в rootless Podman от имени пользователя.
3. **`sudo` — только с согласия пользователя**, с объяснением, что и зачем.
4. **Не выдумывай.** Где сказано «узнай» (как установлен GigaCode, где его настройки, флаги
   headless-режима, адреса API, зеркала реестров) — выясни на этой машине (`--help`,
   `command -v`, конфиги, вопрос пользователю), а не по памяти.
5. **Секреты** не печатай в чат и не коммить.
6. Не получается — смотри «Диагностику» в конце; не помогло — остановись и опиши пользователю
   точную ошибку и что уже сделано.

---

## 1. Почему контейнер, а не «прямо на ноутбуке»

Ограничения GigaCode (запрет `ssh`/`curl`, запрет корневых каталогов и т.п.) — это настройки
**самого GigaCode**. На Hermes они не распространяются: у Hermes свой инструмент `terminal`,
который выполняет команды сам, от имени пользователя.

Встроенные защиты Hermes полезны, но, по документации самого Hermes, это «defense-in-depth,
not a hard boundary»:

- подтверждение опасных команд (`approvals.mode`) и запреты по шаблону (`approvals.deny`)
  сопоставляют **текст команды** — их обходит скрипт, переименованный бинарь, интерпретатор;
- `HERMES_WRITE_SAFE_ROOT` и защищённые пути ограничивают только инструменты `write_file`/`patch`,
  а `terminal` по-прежнему может писать всюду, куда может пользователь;
- ограничить выход в сеть процессу на ноутбуке без root и файрвола нельзя: переменную
  `HTTPS_PROXY` программа может просто проигнорировать.

Поэтому надёжная граница — **контейнер**: в нём агент физически видит только смонтированные
папки, а сеть ему отрезана на уровне Podman (внутренняя сеть без маршрута наружу). Встроенные
защиты Hermes включаем **дополнительно**, вторым слоем. Вариант «без контейнера» — только если
Podman использовать нельзя; что тогда можно сделать — в приложении А.

---

## 2. Целевая архитектура

### Как Hermes «работает на GigaCode»

```
 пользователь ──► Hermes (цикл агента, память, навыки, cron, инструменты)
                    │  каждый вызов модели (основной ход и служебные задачи)
                    ▼
                  gigacode --acp      ← запускается Hermes по stdio, по процессу на вызов
                    │  вся переписка + список инструментов Hermes одним промптом
                    ▼
                  модель GigaCode (его авторизация и настройки)
                    │  ответ: текст и/или вызовы инструментов <tool_call>{...}</tool_call>
                    ▼
 Hermes выполняет вызовы своими инструментами (terminal, file, ...) в контейнере → следующий ход
```

- Это механизм внешнего ACP-агента из Hermes (провайдер `auth_type: external_process`, в ядре
  так подключён `copilot-acp`). Для GigaCode в форке лежит провайдер-плагин
  `deploy/gigacode/gigacode-acp/`; ядро Hermes не меняется.
- **Мозг — GigaCode, руки — Hermes.** Собственные инструменты GigaCode в этом режиме не
  выполняются: на его запросы разрешений Hermes отвечает отказом, чтение/запись файлов через ACP
  Hermes пропускает только внутри рабочего каталога и через свои проверки. Команды выполняет
  Hermes — поэтому его правила (`approvals.deny`, шаг 7.3) здесь важны: запреты из настроек
  GigaCode на них не распространяются.
- Служебные задачи Hermes (сжатие контекста, оценка риска команд, заголовки) тоже идут в
  GigaCode — по умолчанию, без отдельной настройки (проверено).

### Контейнер и сеть

```
 Ноутбук (Linux, пользователь $USER, rootless Podman)
 ┌───────────────────────────────────────────────────────────────────────────────┐
 │   сеть hermes-internal  (--internal: НЕТ маршрута наружу)                     │
 │  ┌──────────────────────────────────────────────┐                             │
 │  │ контейнер hermes                             │                             │
 │  │  образ: корпоративный базовый + Python 3.14 +│                             │
 │  │         Node + GigaCode + Hermes (офлайн)    │                             │
 │  │  • модель = gigacode --acp (плагин)          │                             │
 │  │  • hermes gateway (cron, рутинные задачи)    │                             │
 │  │  • чат: podman exec -it hermes hermes        │                             │
 │  │  • terminal/file агентов — здесь же          │                             │
 │  │  HTTP(S)_PROXY=http://hermes-egress-proxy:3128                             │
 │  │  /opt/data            ← ~/hermes-contour/data (состояние Hermes)           │
 │  │  /opt/data/workspace  ← ~/hermes-work         (рабочие файлы)              │
 │  └───────────────────────┬──────────────────────┘                             │
 │                          │ только HTTP/HTTPS через прокси                      │
 │  ┌───────────────────────▼──────────────────────┐   сеть hermes-egress        │
 │  │ контейнер hermes-egress-proxy                ├──► только адреса из        │
 │  │  тот же образ, egress_proxy.py из форка      │    allowlist.txt           │
 │  │  allowlist.txt: API GigaCode, нужные ресурсы │    (остальное → отказ)     │
 │  └──────────────────────────────────────────────┘                             │
 └───────────────────────────────────────────────────────────────────────────────┘
```

- **Образ строится на корпоративном базовом образе** (RHEL-семейство: `dnf`, Python 3.12, без
  Node, gcc, git и pip — и этого достаточно). Образы Debian/squid и доступ из сборки к пакетным
  репозиториям **не нужны**: всё, что нужно Hermes, приносится готовым «комплектом» (шаг 4),
  а сборка образа идёт с `--network none` (шаг 5).
- **Python для Hermes — свой, 3.14** (переносимая сборка `python-build-standalone`, та же, что
  закреплена в `pm/lock.json`, со своей SQLite). Системный Python 3.12 базового образа **не
  подходит**: зависимости Hermes объявлены только для 3.14, а код использует возможности
  3.13+ (проверено: на 3.12 Hermes падает при первом же запросе к модели).
- **Зависимости Python — готовыми колёсами** (manylinux, x86_64): компилятор не нужен, всё
  скачивается на ноутбуке через ваше PyPI-зеркало скриптом `deploy/gigacode/fetch_wheels.py`.
- **Node для GigaCode — официальный бинарник** из npm-пакета `node-linux-x64` (берётся через
  npm-зеркало; работает на glibc ≥ 2.28). Сам GigaCode ставится на ноутбуке в отдельный каталог
  (`npm install --prefix`) и копируется в образ.
- **Внутри контейнера** терминальный бэкенд Hermes — `local`: контейнер и есть песочница.
  Код Hermes, Python, Node, GigaCode, плагин и прокси принадлежат root и только для чтения;
  агент работает от пользователя `hermes` и пишет только в `/opt/data`.
- **Сеть** — схема из `website/docs/user-guide/egress/network-isolation.md`: контейнер Hermes
  подключён только к внутренней сети без маршрута наружу; единственный путь наружу —
  контейнер-прокси, который пропускает только домены из `allowlist.txt`. Вместо squid —
  небольшой прокси на Python из форка (`deploy/gigacode/egress_proxy.py`), запускается из того же
  образа. Если программа проигнорирует прокси, её запрос просто никуда не уйдёт (fail-closed).
- **Файлы ноутбука:** видны только `~/hermes-contour/data` (состояние Hermes) и `~/hermes-work`
  (рабочая папка; её же пользователь открывает в своих программах).

---

## 3. Разведка и вопросы пользователю

### 3.1. Факты о машине и базовом образе

```bash
uname -m; head -3 /etc/os-release
id -u; id -g; echo "$HOME"
podman --version          # нужен 4.4+ (keep-id:uid=..., netavark); 5.x — отлично
podman info --format 'rootless={{.Host.Security.Rootless}} net={{.Host.NetworkBackend}} cgroup={{.Host.CgroupsVersion}}'
getenforce 2>/dev/null || echo "SELinux: нет"
env | grep -i -E '^(https?|no)_proxy=' || echo "прокси в окружении не задан"
podman images                                    # какие базовые образы доступны
# инструменты ноутбука для сборки комплекта
command -v git python3 npm node; python3 --version; python3 -m pip --version; node --version
python3 -m pip config list; npm config get registry
```

Проверь базовый образ (имя спроси у пользователя или возьми из `podman images`;
дальше — `$BASE_IMAGE`):

```bash
podman run --rm "$BASE_IMAGE" sh -c 'head -2 /etc/os-release; uname -m; ldd --version | head -1;
  command -v bash tar gzip useradd; ls -l /etc/pki/tls/certs/ca-bundle.crt /etc/ssl/certs/ca-certificates.crt 2>&1'
```

Нужно: `x86_64`, glibc **≥ 2.28**, есть `bash`, `tar`, `gzip`. `useradd` желателен (без него
Containerfile допишет пользователя в `/etc/passwd` сам). Системный CA-бандл должен быть — через
него Node и Python доверяют корпоративным сертификатам.

Запиши: версию Podman, сетевой бэкенд (нужен `netavark`), корпоративный прокси, PyPI- и
npm-зеркала ноутбука, версию Node на ноутбуке.

### 3.2. GigaCode на этой машине

```bash
command -v gigacode && readlink -f "$(command -v gigacode)"
gigacode --version
gigacode --help
```

Выясни и запиши:

- **как он установлен:** npm-пакет (`npm ls -g --depth=0` — имя пакета и версия) или
  самостоятельная сборка/архив (каталог установки);
- **где его настройки и авторизация:** каталог в `$HOME` (например `~/.gigacode` — проверь
  фактическое имя), в каких файлах политики (те самые запреты `ssh`/`curl` и т.д.), где токен;
  либо переменные окружения;
- **какие адреса он использует** (API GigaCode/GigaChat) — из конфигурации или документации;
  они нужны для allowlist;
- **ACP-режим (главное):** есть ли в `--help` флаг запуска агентом по ACP через stdio —
  обычно `--acp` (в старых версиях CLI этого семейства — `--experimental-acp`). Запиши точные
  аргументы — это `ACP_ARGS`. Быстрая проверка рукопожатия:
  ```bash
  printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":1,"clientCapabilities":{}}}' \
    | timeout 20 gigacode <ACP_ARGS> | head -c 400; echo
  ```
  Должен прийти JSON с `"result"` и `protocolVersion`. **Если ACP-режима нет — остановись** и
  сообщи пользователю: схема «Hermes только на GigaCode» без ACP не работает; альтернатива —
  подключить Hermes к той же модели напрямую через OpenAI-совместимый API контура
  (`model.provider custom`), если такой есть;
- **headless-режим** (для навыка из шага 8): флаги «выполни задачу и выйди», задача —
  последним аргументом (например `-p` — проверь по `--help`!);
- **как он учитывает прокси** (`HTTPS_PROXY` или своя настройка).

### 3.3. Спроси пользователя (одним сообщением)

1. **Подтверди схему:** Hermes работает только через GigaCode (его модель и авторизация),
   отдельный LLM-ключ для Hermes не нужен. Если в GigaCode доступно несколько моделей — какую
   использовать (по умолчанию — ту, что настроена в GigaCode).
2. **Список разрешённых ресурсов** — домены/адреса, к которым агенту можно обращаться
   (внутренний git, wiki, трекер, зеркала пакетов и т.п.). Всё остальное будет закрыто.
3. **Рутинные задачи**, которые агент должен делать (чтобы сверить их со списком ресурсов и
   настроить расписание в шаге 9).
4. **Можно ли агенту `curl`/`wget`** к разрешённым ресурсам? В GigaCode это запрещено; для
   Hermes по умолчанию тоже запретим (шаг 7.3), но для части задач это может понадобиться.
5. Можно ли использовать `sudo`, если чего-то не хватит (например, обновить Podman).
6. **Базовый образ** — точное имя корпоративного образа, на котором строить (`$BASE_IMAGE`).
7. **Откуда взять Python 3.14** (архив ~125 МБ с GitHub, шаг 4.2), если GitHub с ноутбука
   недоступен: есть ли корпоративное зеркало GitHub/«raw»-репозиторий или разрешённый способ
   перенести файл.

Переменные для следующих шагов (если оболочка не сохраняет их между командами — запиши блок в
`~/hermes-contour/setup.env` и делай `source` перед каждым шагом):

```bash
export HERMES_FORK_URL="https://github.com/wild-werewolf/hermes-agent-gigacode.git"
export CONTOUR="$HOME/hermes-contour"     # конфиги, образ, состояние Hermes
export WORK="$HOME/hermes-work"           # рабочая папка агента
export KIT="$CONTOUR/kit"                 # комплект для офлайн-сборки образа
export BASE_IMAGE='<корпоративный базовый образ>'
mkdir -p "$CONTOUR"/{data,egress} "$KIT" "$WORK"
```

---

## 4. Комплект для сборки (готовится на ноутбуке)

Всё, чего нет в базовом образе, собирается на ноутбуке в каталог `$KIT` через доступные
ноутбуку зеркала (PyPI, npm) и затем копируется в образ при офлайн-сборке (шаг 5).

### 4.1. Исходники форка

```bash
mkdir -p ~/src && cd ~/src
[ -d hermes-agent-gigacode ] || git clone "$HERMES_FORK_URL" hermes-agent-gigacode
cd hermes-agent-gigacode && git pull --ff-only
export FORK="$PWD"
git archive --format=tar.gz -o "$KIT/hermes-src.tar.gz" HEAD
cp -r deploy/gigacode/gigacode-acp deploy/gigacode/egress_proxy.py deploy/gigacode/Containerfile "$KIT/"
```

Нет `git` на ноутбуке — скачай архив ветки `main` форка с GitHub (`.../archive/refs/heads/main.tar.gz`),
распакуй его как `$FORK` и упакуй обратно в `$KIT/hermes-src.tar.gz` так, чтобы файлы лежали в
корне архива (без верхнего каталога).

### 4.2. Python 3.14

Адрес и контрольная сумма закреплены в форке:

```bash
read -r PY_URL PY_SHA < <(python3 - "$FORK/pm/lock.json" <<'EOF'
import json, sys
a = json.load(open(sys.argv[1]))["packages"]["python"]["artifacts"]["linux-x64"]
print(a["url"], a["sha256"])
EOF
)
echo "$PY_URL"
curl -fL --retry 3 -o "$KIT/python.tar.gz" "$PY_URL"      # или из зеркала/переноса, см. ниже
echo "$PY_SHA  $KIT/python.tar.gz" | sha256sum -c -        # ОБЯЗАТЕЛЬНО: должно быть OK
```

Если прямой загрузки с GitHub нет — источники по порядку: корпоративный прокси GitHub/«raw»-зеркало
(часто есть в Nexus/Artifactory; тот же путь после `https://github.com/`); перенос файла (~125 МБ)
с машины, где он скачивается, по разрешённому в компании каналу. Откуда бы файл ни пришёл,
проверка `sha256sum -c` обязательна — она гарантирует, что это ровно закреплённая сборка.

### 4.3. Колёса Python

```bash
python3 "$FORK/deploy/gigacode/fetch_wheels.py" "$FORK/deploy/gigacode/requirements.lock.txt" "$KIT/wheels"
ls "$KIT/wheels" | wc -l      # ~83 файла, ~50 МБ
```

Скрипт берёт закреплённые версии из `requirements.lock.txt` (экспорт `uv.lock` форка), выбирает
нужные для Linux x86_64 / CPython 3.14 и скачивает **только готовые колёса** через настройки pip
ноутбука (его PyPI-зеркало). Python ноутбука может быть любой версии. Если какой-то пакет не
нашёлся — в зеркале нет колеса под CPython 3.14: сообщи пользователю имя пакета.

### 4.4. Node.js

Возьми ту же основную версию, что у Node на ноутбуке (на ней работает GigaCode), — из npm-зеркала
как пакет с официальным бинарником:

```bash
NODE_MAJOR=$(node -p 'process.versions.node.split(".")[0]')
cd "$KIT" && npm pack "node-linux-x64@$NODE_MAJOR" && mv node-linux-x64-*.tgz node.tgz && cd -
```

### 4.5. GigaCode

```bash
npm install --prefix "$KIT/gigacode" '<пакет GigaCode>@<версия, как на ноутбуке>'
ls "$KIT/gigacode/node_modules/.bin/"          # имя исполняемого файла → GIGACODE_BIN
```

`GIGACODE_BIN` — путь к исполняемому файлу внутри `$KIT/gigacode`, обычно
`node_modules/.bin/gigacode`. Если GigaCode поставляется не npm-пакетом — скопируй его каталог
установки в `$KIT/gigacode/` и укажи путь к запускаемому файлу относительно него.

> Нативные модули npm-пакетов (если они есть) собраны под Node ноутбука — поэтому в 4.4 берётся
> та же основная версия Node. Настройки и токены GigaCode в комплект **не кладутся** (шаг 7.2).

✅ **Проверка комплекта:**

```bash
ls -la "$KIT"; du -sh "$KIT"/*
# ожидается: python.tar.gz hermes-src.tar.gz node.tgz wheels/ gigacode/ gigacode-acp/ egress_proxy.py Containerfile
```

---

## 5. Образ

Сборка — **без сети** (`--network none`): всё берётся из комплекта, пакетный менеджер, компилятор
и зеркала внутри сборки не используются.

```bash
cd "$KIT"
podman build --network none -f Containerfile \
  --build-arg BASE_IMAGE="$BASE_IMAGE" \
  --build-arg GIGACODE_BIN=node_modules/.bin/gigacode \
  -t localhost/hermes-gigacode:latest .
```

Что делает `Containerfile` (`deploy/gigacode/Containerfile`): распаковывает Python 3.14 в
`/opt/python`, создаёт `/opt/hermes/venv` и ставит в него колёса без доступа к сети, кладёт
исходники Hermes в `/opt/hermes/src`, Node в `/opt/node`, GigaCode в `/opt/gigacode`, плагин
`gigacode` — в каталог встроенных провайдеров, прокси — в `/opt/hermes/egress_proxy.py`;
создаёт пользователя `hermes` (uid 10000) и направляет Python и Node на системный CA-бандл
базового образа. Запускается по умолчанию `hermes gateway run`.

✅ **Проверка:**

```bash
I=localhost/hermes-gigacode:latest
podman run --rm --network none "$I" sh -c 'id; hermes --version | head -1; node --version; gigacode --version;
  ls /opt/hermes/src/plugins/model-providers/gigacode-acp; ls -l /opt/hermes/ca-bundle.crt'
podman run --rm --network none "$I" sh -c "printf '%s\n' '{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{\"protocolVersion\":1,\"clientCapabilities\":{}}}' | timeout 30 gigacode <ACP_ARGS> | head -c 300"; echo
```

Последняя команда должна вернуть JSON с `"result"` — GigaCode в образе понимает ACP.

---

## 6. Закрытая сеть и прокси со списком разрешённых ресурсов

### 6.1. Сети Podman

```bash
podman network exists hermes-internal || podman network create --internal hermes-internal
podman network exists hermes-egress   || podman network create hermes-egress
podman network inspect hermes-internal --format '{{.Internal}}'   # должно быть true
```

### 6.2. Allowlist

`$CONTOUR/egress/allowlist.txt` — по одному домену в строке, `#` — комментарий.
`.corp.example` (с точкой в начале) — домен и все поддомены; `api.corp.example` — только этот
хост. Заполни по ответам пользователя и шагу 3.2: адреса API GigaCode (через них идёт и вся
«мысль» Hermes) и разрешённые внутренние ресурсы.

```bash
cat > "$CONTOUR/egress/allowlist.txt" <<'EOF'
# API GigaCode (возьми из его конфигурации) — через него работает и Hermes
gigacode-api.corp.example
# разрешённые ресурсы контура
.git.corp.example
EOF
```

Файл перечитывается при каждом изменении — перезапуск прокси не нужен.

### 6.3. Запуск прокси

```bash
podman run -d --name hermes-egress-proxy --restart unless-stopped \
  --network hermes-egress \
  -v "$CONTOUR/egress:/etc/egress:ro,z" \
  localhost/hermes-gigacode:latest \
  python /opt/hermes/egress_proxy.py --allowlist /etc/egress/allowlist.txt
podman network connect hermes-internal hermes-egress-proxy
```

Параметры прокси (`python /opt/hermes/egress_proxy.py --help`):

- `--ports 443,80` — разрешённые порты назначения (добавь, например, `8443`, если он нужен);
- `--upstream proxy.corp.example:8080` — если ноутбук ходит в контур через корпоративный прокси
  (шаг 3.1), прокси Hermes отправит разрешённые соединения через него;
- HTTPS идёт туннелем `CONNECT` (трафик не расшифровывается), HTTP — обычным запросом. Другие
  протоколы (ssh, git по ssh, произвольный TCP) не проходят — это соответствует политике «без ssh».

Журнал — `podman logs -f hermes-egress-proxy`: `ALLOW …` — пропущено, `DENY …` — отклонено,
`FAIL …` — разрешено, но адрес не ответил.

✅ **Проверка** — временный контейнер во внутренней сети:

```bash
P='import sys, urllib.request as u
try: print(sys.argv[1], "->", u.urlopen(sys.argv[1], timeout=10).status)
except Exception as e: print(sys.argv[1], "-> ERR", e)'
T="podman run --rm --network hermes-internal localhost/hermes-gigacode:latest python -c"
PX="-e HTTPS_PROXY=http://hermes-egress-proxy:3128 -e HTTP_PROXY=http://hermes-egress-proxy:3128"
# 1) напрямую наружу — ошибка (нет маршрута / имя не резолвится)
$T "$P" https://ya.ru
# 2) через прокси к неразрешённому — "Tunnel connection failed: 403"
podman run --rm --network hermes-internal $PX localhost/hermes-gigacode:latest python -c "$P" https://ya.ru
# 3) через прокси к разрешённому — ответ сервиса (любой HTTP-код, не ошибка туннеля)
podman run --rm --network hermes-internal $PX localhost/hermes-gigacode:latest python -c "$P" https://<домен из allowlist>/
podman logs --tail 5 hermes-egress-proxy
```

> Известное ограничение (из документации Hermes): во внутренней сети имена могут
> **разрешаться** через DNS, даже когда соединиться нельзя. Данные так не вынести в сколько-нибудь
> заметном объёме; для большинства моделей угроз это приемлемо.

---

## 7. Контейнер Hermes

### 7.1. Запуск

```bash
podman run -d --name hermes --restart unless-stopped \
  --network hermes-internal \
  --userns=keep-id:uid=10000,gid=10000 \
  -v "$CONTOUR/data:/opt/data:z" \
  -v "$WORK:/opt/data/workspace:z" \
  -e HTTP_PROXY=http://hermes-egress-proxy:3128 \
  -e HTTPS_PROXY=http://hermes-egress-proxy:3128 \
  -e http_proxy=http://hermes-egress-proxy:3128 \
  -e https_proxy=http://hermes-egress-proxy:3128 \
  -e NO_PROXY=localhost,127.0.0.1,::1 \
  -e no_proxy=localhost,127.0.0.1,::1 \
  localhost/hermes-gigacode:latest
```

Если ACP-аргументы GigaCode из шага 3.2 — не `--acp` (например `--experimental-acp`), добавь
перед именем образа `-e HERMES_GIGACODE_ACP_ARGS='<ACP_ARGS>'`.

- Команда по умолчанию — `hermes gateway run`: держит контейнер запущенным и выполняет задания по
  расписанию (cron) — это и есть «рутинные задачи». Мессенджеры подключать не обязательно. Если
  процесс упадёт, Podman перезапустит контейнер (`--restart unless-stopped`).
- `--userns=keep-id:uid=10000,gid=10000`: пользователь `hermes` (uid 10000) внутри контейнера
  отображается в **твоего** пользователя ноутбука, поэтому файлы агента в `~/hermes-work`
  принадлежат тебе.
- `:z` — метка SELinux для двух выделенных папок (без SELinux игнорируется). Никогда не монтируй
  `$HOME` целиком и системные каталоги.
- Порты наружу не публикуются.

Автозапуск после перезагрузки (rootless): `systemctl --user enable --now podman-restart.service`;
чтобы работало без входа в графическую сессию — с согласия пользователя
`sudo loginctl enable-linger "$USER"`.

✅ **Проверка:**

```bash
podman ps --filter name=hermes --format '{{.Names}} {{.Status}}'
podman logs --tail 20 hermes          # «Hermes Gateway Starting…», «No messaging platforms enabled» — норма
podman exec hermes hermes --version
podman exec hermes touch /opt/data/workspace/.owner-test
ls -ln "$WORK/.owner-test"            # владелец — твой uid (id -u), а не 100000+
rm -f "$WORK/.owner-test"
```

Если Podman не понимает `keep-id:uid=...` (старее 4.3) или владелец не твой uid — пересоздай
контейнер (`podman rm -f hermes`), заменив `--userns=keep-id:uid=10000,gid=10000` на явное
отображение (нужен диапазон в `/etc/subuid` и `/etc/subgid` не меньше 65536):

```bash
  --uidmap 0:1:10000 --uidmap 10000:0:1 --uidmap 10001:10001:55535 \
  --gidmap 0:1:10000 --gidmap 10000:0:1 --gidmap 10001:10001:55535 \
```

### 7.2. Настройки и авторизация GigaCode внутри контейнера

Узнай, с каким `HOME` Hermes запускает дочерние процессы (в том числе `gigacode --acp`):

```bash
podman exec -w /opt/hermes/src hermes /opt/hermes/venv/bin/python -c \
  'import os; from hermes_constants import apply_subprocess_home_env as a; e=dict(os.environ); a(e); print(e["HOME"])'
```

Скопируй каталог настроек GigaCode из шага 3.2 (с политиками запретов и авторизацией) в этот
`HOME` внутри смонтированного каталога данных. Например, если `HOME` = `/opt/data`, а настройки у
тебя в `~/.gigacode`:

```bash
cp -a ~/.gigacode "$CONTOUR/data/.gigacode"      # внутри контейнера: /opt/data/.gigacode
chmod -R go-rwx "$CONTOUR/data/.gigacode"
```

(если `HOME` = `/opt/data/home` — соответственно `"$CONTOUR/data/home/.gigacode"`.)

Если GigaCode берёт авторизацию из **переменных окружения**, не передавай их всему контейнеру
(`-e`): Hermes тоже читает окружение, а переменные вроде `OPENAI_BASE_URL` он может принять за
свои настройки. Дай их только процессу GigaCode — через обёртку в каталоге данных:

```bash
mkdir -p "$CONTOUR/data/bin"
cat > "$CONTOUR/data/gigacode.env" <<'EOF'
export ИМЯ='значение'
EOF
chmod 600 "$CONTOUR/data/gigacode.env"
cat > "$CONTOUR/data/bin/gigacode-acp" <<'EOF'
#!/bin/sh
. /opt/data/gigacode.env
exec gigacode "$@"
EOF
chmod 700 "$CONTOUR/data/bin/gigacode-acp"
```

и пересоздай контейнер (шаг 7.1) с `-e HERMES_GIGACODE_ACP_COMMAND=/opt/data/bin/gigacode-acp`.

Агент Hermes сможет прочитать эти файлы (это токен того же пользователя, а сеть закрыта
allowlist'ом). Если политика запрещает передавать токен агенту — обсуди с пользователем.

✅ **Проверка** — GigaCode изнутри контейнера отвечает через прокси (`<HOME>` — значение из
команды выше):

```bash
G="podman exec -e HOME=<HOME> -w /opt/data/workspace hermes"
$G gigacode <флаги headless из 3.2> "Ответь одним словом: работаю"
podman logs --tail 5 hermes-egress-proxy      # API GigaCode — ALLOW, не DENY
```

Если GigaCode не ходит через прокси — найди в `--help`/документации его параметр прокси и задай
его в настройках из этого шага. Ошибка TLS (`unable to get local issuer certificate`) — в
базовом образе нет корпоративного корневого сертификата в системном бандле: проверь
`ls -l /opt/hermes/ca-bundle.crt` и сообщи пользователю.

### 7.3. Настройки Hermes

Все команды — внутри контейнера (`podman exec hermes hermes ...`; они выполняются от
пользователя `hermes`, как и сам агент).

**Модель Hermes = GigaCode** (провайдер `gigacode` из плагина):

```bash
H="podman exec hermes hermes"
$H auth status gigacode                       # должно быть: gigacode: logged in (бинарь найден)
$H config set model.provider gigacode
$H config set model.base_url acp://gigacode
$H config set model.default gigacode          # «модель, которая настроена в самом GigaCode»
$H config set model.context_length <контекст модели GigaCode в токенах>
```

- `model.default gigacode` — служебное имя: Hermes не переключает модель и использует ту, что
  выбрана в GigaCode. Если пользователь хочет конкретную — посмотри список
  (`podman exec -it hermes hermes model` → GigaCode) и укажи её id вместо `gigacode`.
- `model.context_length` — размер контекста модели GigaCode (узнай в его документации/настройках).
  Без него Hermes считает 256 000 токенов и начнёт сжимать историю слишком поздно. Учти: описание
  инструментов Hermes само занимает ~15 000 токенов; если контекст модели маленький — отключи
  лишние наборы инструментов ниже.
- Служебные задачи Hermes (заголовки, сжатие, оценка риска команд) по умолчанию идут в ту же
  модель — проверено: отдельных ключей не нужно.
- Каждый вызов модели — отдельный короткий процесс `gigacode --acp`; это добавляет пару секунд
  к каждому шагу агента — так и задумано.

**Рабочая папка и бэкенд:**

```bash
$H config set terminal.backend local         # контейнер — уже песочница
$H config set terminal.cwd /opt/data/workspace
```

**Отключи инструменты, которым нужен открытый интернет** (в контуре они бесполезны и только
порождают отказы на прокси):

```bash
$H config set agent.disabled_toolsets '["web","search","x_search","browser","image_gen","video_gen","tts","spotify","computer_use"]'
```

**Второй слой — правила Hermes** (дополняют сеть и контейнер, не заменяют их):

```bash
$H config set approvals.mode smart            # опасные команды: оценка риска, сомнительные — к человеку
$H config set approvals.cron_mode deny        # в фоновых заданиях опасное — запрещено
$H config set approvals.deny '["ssh *","scp *","sftp *","rsync *:*","nc *","ncat *","netcat *","socat *","telnet *","sudo *","su *","curl *","wget *"]'
```

Убери `curl *` и `wget *`, если пользователь в шаге 3.3 разрешил их для работы с разрешёнными
ресурсами (сеть всё равно ограничена allowlist'ом).

✅ **Проверка:**

```bash
$H doctor
$H chat --oneshot -q 'Ответь одним словом: готов'
$H chat --oneshot --yolo -q 'Выполни в терминале echo HERMES-ON-GIGACODE и покажи результат'
$H config get agent.disabled_toolsets
podman logs hermes-egress-proxy 2>&1 | grep -c DENY
```

Отказы `DENY` после запуска Hermes — нормально: Hermes в фоне пытается обновить каталог моделей,
проверить обновления плагинов и т.п., а прокси это отбрасывает. Плохо, только если отказ
приходится на нужное для работы (API GigaCode, разрешённые ресурсы) — тогда поправь
`$CONTOUR/egress/allowlist.txt` (прокси перечитает его сам).

---

## 8. Навык Hermes «gigacode» (необязательно)

Hermes уже думает через GigaCode (шаг 7.3), но там GigaCode — только «мозг»: свои инструменты он
не запускает. Для больших задач по коду бывает удобнее отдать GigaCode задачу целиком, чтобы он
отработал её своим агентным циклом, своими инструментами и **под своей политикой** (те самые
запреты `ssh`/`curl` и т.д.). Навык объясняет агенту, как это сделать (флаги headless-режима — из
шага 3.2):

```bash
mkdir -p "$CONTOUR/data/skills/gigacode"
cat > "$CONTOUR/data/skills/gigacode/SKILL.md" <<'EOF'
---
name: gigacode
description: "Delegate coding tasks to the GigaCode CLI installed in this container."
version: 1.0.0
author: local
platforms: [linux]
metadata:
  hermes:
    tags: [Coding-Agent, GigaCode, Delegation]
    related_skills: [opencode, claude-code, codex]
---

# GigaCode

GigaCode is installed in this container as the `gigacode` command and has its own safety policy.
The network is closed: only allowlisted internal resources are reachable through the proxy.

## When to Use

- The user asks to use GigaCode.
- A coding task (implement, refactor, review, explain code) on a project in /opt/data/workspace.

## How

1. Work inside /opt/data/workspace (the user's ~/hermes-work).
2. Run from the project directory:
   `gigacode <HEADLESS_FLAGS> "<self-contained task>"`.
3. GigaCode cannot ask follow-up questions: give the goal, constraints, files and the expected
   result in one prompt.
4. For long tasks start it with background=true and poll.
5. Review its changes with `git diff` before reporting back.

## Errors

- Network errors or "Tunnel connection failed: 403": the host is not on the allowlist. Do not look
  for workarounds; tell the user which host is needed.
EOF
```

Замени `<HEADLESS_FLAGS>` на реальные флаги.

✅ **Проверка:** `podman exec hermes hermes skills list | grep gigacode`

---

## 9. Финальная проверка и передача пользователю

```bash
mkdir -p "$WORK/demo"
podman exec -it hermes hermes chat --oneshot -q 'Используя навык gigacode, попроси GigaCode создать в /opt/data/workspace/demo файл hello.py, печатающий "привет", затем запусти его и покажи вывод. Потом проверь средствами Python, доступен ли https://ya.ru, и коротко объясни результат.'
ls -ln "$WORK/demo"
podman logs --tail 20 hermes-egress-proxy
```

Готово, если:

- [ ] `podman ps` показывает `hermes` и `hermes-egress-proxy` в статусе Up;
- [ ] `hermes auth status gigacode` — logged in, `hermes config get model.provider` — `gigacode`;
- [ ] ответы и вызовы инструментов идут через GigaCode (тест из шага 7.3 вывел
      `HERMES-ON-GIGACODE`);
- [ ] GigaCode работает изнутри контейнера; файлы появились в `~/hermes-work/demo` и принадлежат
      пользователю;
- [ ] неразрешённые адреса недоступны (напрямую — нет маршрута, через прокси — `DENY`);
- [ ] разрешённые ресурсы доступны; в журнале прокси `ALLOW` — только к API GigaCode и
      разрешённым ресурсам;
- [ ] агенту не видны файлы ноутбука вне `~/hermes-contour/data` и `~/hermes-work`
      (`podman exec hermes ls /home` — нет каталога пользователя).

Если пользователь назвал рутинные задачи (шаг 3.3) — заведи их как задания по расписанию
(`podman exec -it hermes hermes cron --help`, или попроси Hermes в чате: «каждый будний день в
9:00 делай …») и проверь первое срабатывание.

Удали `~/hermes-work/demo` (спроси) и выдай пользователю памятку:

```text
Чат с агентом:         podman exec -it hermes hermes
   удобный алиас:      alias hermes='podman exec -it hermes hermes'   (в ~/.bashrc)
Рабочая папка:         ~/hermes-work        (внутри контейнера: /opt/data/workspace)
Данные Hermes:         ~/hermes-contour/data
Разрешённые ресурсы:   ~/hermes-contour/egress/allowlist.txt   (правка применяется сразу)
Журнал выхода в сеть:  podman logs -f hermes-egress-proxy   (DENY = заблокировано, ALLOW = разрешено)
Задания по расписанию: podman exec -it hermes hermes cron list
Логи Hermes:           podman exec hermes hermes logs
Остановить/запустить:  podman stop hermes hermes-egress-proxy
                       podman start hermes-egress-proxy hermes
```

**Обновление Hermes:** `git pull` в `~/src/hermes-agent-gigacode`, затем шаги 4.1–4.3 (новые
исходники, при изменении `pm/lock.json` — новый Python, при изменении `requirements.lock.txt` —
новые колёса) и 5 (пересобрать образ), затем пересоздать оба контейнера:
`podman rm -f hermes hermes-egress-proxy` и команды из шагов 6.3 и 7.1.
Данные в `~/hermes-contour/data` сохраняются. `hermes update` внутри контейнера не используй:
код в образе неизменяемый.

---

## Приложение А. Если контейнер использовать нельзя

Тогда Hermes ставится на ноутбук (из клона форка:
`HERMES_REPO_URL="$HERMES_FORK_URL" bash scripts/install.sh --branch main --non-interactive`), и
пользователю нужно честно сказать: **жёсткой границы не будет** — агент работает с правами
пользователя, а сеть без root ограничить нельзя. Работа на GigaCode настраивается так же: каталог
`deploy/gigacode/gigacode-acp` копируется в `~/.hermes/plugins/model-providers/`, затем настройки
модели из шага 7.3 (`hermes config set ...` без `podman exec hermes`). Максимум защиты средствами
Hermes:

- `approvals.mode manual` — каждая опасная команда только с подтверждения человека;
- `approvals.deny` — тот же список, что в шаге 7.3;
- `agent.disabled_toolsets` — как в шаге 7.3;
- `HERMES_WRITE_SAFE_ROOT="$HOME/hermes-work:$HOME/.hermes"` в окружении запуска — ограничивает
  `write_file`/`patch` (но не `terminal`);
- `terminal.cwd` = `~/hermes-work`;
- `approvals.cron_mode deny` и никаких заданий по расписанию без присмотра.

Промежуточный вариант, если Podman есть, но запускать в нём сам Hermes нельзя: Hermes на ноутбуке
с `terminal.backend: docker` (Podman) и `terminal.docker_network: false` — команды агента идут в
контейнер без сети. Но тогда процесс Hermes и `gigacode --acp` работают на ноутбуке без ограничений
сети, а GigaCode в такой песочнице не достучится до своего API. Основной вариант (Hermes целиком в
контейнере за прокси) лучше.

---

## Диагностика

| Симптом | Что проверить |
|---|---|
| `podman network create --internal` не работает | нужен netavark (`podman info --format '{{.Host.NetworkBackend}}'`); со старым CNI — обновить Podman (с согласия) |
| `short-name resolution enforced` | пиши образы полным именем: `<реестр>/...`, `localhost/...` |
| Сборка образа падает на `pip install` | колесо не найдено: в 4.3 `fetch_wheels.py` должен был скачать все пакеты без ошибок; пересобери комплект |
| `gigacode --version` падает при сборке | несовпадение Node: основная версия в 4.4 должна совпадать с Node ноутбука; или неверный `GIGACODE_BIN` |
| `GLIBC_2.xx not found` | glibc базового образа ниже 2.28 — такой образ не подходит (шаг 3.1) |
| Контейнер `hermes` перезапускается | `podman logs hermes`; ошибка про `--user`/`--userns` — вариант с `--uidmap`/`--gidmap` из шага 7.1 |
| `Permission denied` на `/opt/data` | SELinux: у томов должен быть `:z`; пересоздай контейнер |
| Файлы в `~/hermes-work` чужого владельца | шаг 7.1 (`--userns=keep-id:uid=10000,gid=10000`); уже созданные: `podman unshare chown -R 10000:10000 ~/hermes-work` |
| Hermes не получает ответа модели | журнал прокси: API GigaCode в `DENY`? → в `allowlist.txt` |
| TLS-ошибки (`unable to get local issuer certificate`) | Python и Node образа доверяют `/opt/hermes/ca-bundle.crt` = системный бандл базового образа; если корпоративного корня там нет — смонтируй бандл ноутбука поверх: `-v <бандл ноутбука>:/opt/hermes/ca-bundle.crt:ro,z` (шаги 6.3 и 7.1) |
| GigaCode «нет сети» | он игнорирует `HTTPS_PROXY` → его собственная настройка прокси (шаг 7.2) |
| `Could not find the 'gigacode' CLI` | `gigacode` не в `PATH` контейнера → `-e HERMES_GIGACODE_ACP_COMMAND=/полный/путь` (шаг 7.1) |
| `ACP transport not supported by 'gigacode'` | в `--help` нет `--acp` → верные ACP-аргументы в `-e HERMES_GIGACODE_ACP_ARGS=...` (шаг 7.1) |
| `Copilot ACP process exited early: …` / `Timed out waiting for Copilot ACP` | это общий ACP-транспорт Hermes (сообщения названы по Copilot): GigaCode упал или завис — текст ошибки после двоеточия; проверь его авторизацию (`HOME`, шаг 7.2) и рукопожатие из шага 7.2 |
| Провайдера `gigacode` нет в `hermes model` | плагин не попал в образ: проверка из шага 5, пересобери образ |
| Агент «забывает» начало разговора или GigaCode ругается на длину | `model.context_length` (шаг 7.3) меньше/больше реального; отключи лишние наборы инструментов |
| Имя `hermes-egress-proxy` не резолвится | `podman network connect hermes-internal hermes-egress-proxy`; оба контейнера должны быть в `hermes-internal` |
| После перезагрузки ничего не запущено | `systemctl --user enable --now podman-restart.service`, `loginctl enable-linger` |

## Откат

```bash
podman rm -f hermes hermes-egress-proxy
podman network rm hermes-internal hermes-egress
podman rmi localhost/hermes-gigacode:latest
```

Данные остаются в `~/hermes-contour` и `~/hermes-work`; удалять их — только по прямой просьбе
пользователя.
