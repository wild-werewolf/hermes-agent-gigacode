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
  GigaCode — шаг 7.3 явно направляет их на основную модель.

### Контейнер и сеть

```
 Ноутбук (Linux, пользователь $USER, rootless Podman)
 ┌───────────────────────────────────────────────────────────────────────────────┐
 │   сеть hermes-internal  (--internal: НЕТ маршрута наружу)                     │
 │  ┌──────────────────────────────────────────────┐                             │
 │  │ контейнер hermes  (образ: Hermes + GigaCode) │                             │
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
 │  │ контейнер hermes-egress-proxy (squid)        ├──► только адреса из        │
 │  │  allowlist.txt: API GigaCode,                │    allowlist.txt           │
 │  │  нужные внутренние ресурсы                   │    (остальное → отказ)     │
 │  └──────────────────────────────────────────────┘                             │
 └───────────────────────────────────────────────────────────────────────────────┘
```

- **Hermes целиком в контейнере** (официальный `Dockerfile` из корня репозитория; Podman его
  собирает и запускает). Внутри контейнера терминальный бэкенд Hermes — `local`: контейнер и есть
  песочница, вложенный Podman не нужен.
- **GigaCode — слоем поверх образа Hermes.** Его настройки, включая собственные запреты,
  копируются в контейнер и продолжают работать.
- **Сеть** — схема из `website/docs/user-guide/egress/network-isolation.md`: контейнер Hermes
  подключён только к внутренней сети без маршрута наружу; единственный путь наружу — прокси squid,
  который пропускает только домены из `allowlist.txt`. Если программа проигнорирует прокси, её
  запрос просто никуда не уйдёт (fail-closed).
- **Файлы ноутбука:** видны только `~/hermes-contour/data` (состояние Hermes) и `~/hermes-work`
  (рабочая папка; её же пользователь открывает в своих программах).

---

## 3. Разведка и вопросы пользователю

### 3.1. Факты о машине

```bash
uname -a; head -3 /etc/os-release
id -u; id -g; echo "$HOME"
podman --version          # нужен 4.4+ (keep-id:uid=..., netavark); 5.x — отлично
podman info --format 'rootless={{.Host.Security.Rootless}} net={{.Host.NetworkBackend}} cgroup={{.Host.CgroupsVersion}}'
getenforce 2>/dev/null || echo "SELinux: нет"
env | grep -i -E '^(https?|no)_proxy=' || echo "прокси в окружении не задан"
grep -v -e '^#' -e '^$' ~/.config/containers/registries.conf /etc/containers/registries.conf 2>/dev/null | head -30
cat ~/.npmrc 2>/dev/null; cat ~/.config/pip/pip.conf /etc/pip.conf 2>/dev/null
```

Запиши: версию Podman, сетевой бэкенд (нужен `netavark`), есть ли корпоративный прокси и
зеркала реестров (контейнерного, npm, PyPI, Debian).

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

Переменные для следующих шагов (если оболочка не сохраняет их между командами — запиши блок в
`~/hermes-contour/setup.env` и делай `source` перед каждым шагом):

```bash
export HERMES_FORK_URL="https://github.com/wild-werewolf/hermes-agent-gigacode.git"
export CONTOUR="$HOME/hermes-contour"     # конфиги, образ, состояние Hermes
export WORK="$HOME/hermes-work"           # рабочая папка агента
mkdir -p "$CONTOUR"/{data,squid,image} "$WORK"
```

---

## 4. Образ Hermes

Сборка требует доступа к реестрам и зеркалам (Debian, PyPI, npm, артефакты PM). Это ограничение
**сборки**, а не работы агента: allowlist из шага 5 действует только на запущенные контейнеры.
Пробуй варианты по порядку.

**А. Собрать из форка** (предпочтительно — код ровно из этого форка):

```bash
mkdir -p ~/src && cd ~/src
[ -d hermes-agent-gigacode ] || git clone "$HERMES_FORK_URL" hermes-agent-gigacode
cd hermes-agent-gigacode && git pull --ff-only
podman build -t localhost/hermes-agent:fork .
```

Если сборка падает на скачивании — передай прокси/зеркала из шага 3.1
(`--build-arg HTTPS_PROXY=...`, `--build-arg HTTP_PROXY=...`, `--build-arg NO_PROXY=...`) и
повтори. Лог длинный — смотри на последнюю ошибку.

**Б. Взять готовый образ** через корпоративное зеркало контейнерного реестра (код совпадает с
апстримом; форк пока отличается от него только этой инструкцией):

```bash
podman pull docker.io/nousresearch/hermes-agent:stable
podman tag docker.io/nousresearch/hermes-agent:stable localhost/hermes-agent:fork
```

**В. Перенести образ** с машины, где есть доступ: сделать там А или Б, затем
`podman save -o hermes-agent.tar localhost/hermes-agent:fork`, принести файл и выполнить
`podman load -i hermes-agent.tar`. Это решение пользователя — предложи, если А и Б не прошли.

Образ прокси (так же — через зеркало или перенос):

```bash
podman pull docker.io/ubuntu/squid:6.10-24.04_edge
```

✅ **Проверка:**

```bash
podman run --rm localhost/hermes-agent:fork --version
podman run --rm --entrypoint squid docker.io/ubuntu/squid:6.10-24.04_edge -v | head -1
```

---

## 5. Закрытая сеть и прокси со списком разрешённых ресурсов

### 5.1. Сети Podman

```bash
podman network exists hermes-internal || podman network create --internal hermes-internal
podman network exists hermes-egress   || podman network create hermes-egress
podman network inspect hermes-internal --format '{{.Internal}}'   # должно быть true
```

### 5.2. Allowlist

`$CONTOUR/squid/allowlist.txt` — по одному домену в строке. `.corp.example` (с точкой в начале)
— домен и все поддомены; `api.corp.example` — только этот хост. Заполни по ответам пользователя
и шагу 3.2: адреса API GigaCode (через них идёт и вся «мысль» Hermes) и разрешённые внутренние
ресурсы. Отдельного адреса LLM для Hermes нет.

```bash
cat > "$CONTOUR/squid/allowlist.txt" <<'EOF'
# API GigaCode (возьми из его конфигурации) — через него работает и Hermes
gigacode-api.corp.example
# разрешённые ресурсы контура
.git.corp.example
EOF
```

Если ресурсы заданы IP-адресами, а не именами, — положи их в `$CONTOUR/squid/allow_ips.txt`
(адрес или подсеть в строке), раскомментируй строки `allowed_ips` в конфиге ниже и смонтируй
файл в шаге 5.4.

### 5.3. Конфиг squid

```bash
cat > "$CONTOUR/squid/squid.conf" <<'EOF'
# Egress allowlist for the Hermes container: everything not listed is denied.
http_port 3128

acl allowed_domains dstdomain "/etc/squid/allowlist.txt"
# acl allowed_ips dst "/etc/squid/allow_ips.txt"

acl SSL_ports port 443
acl Safe_ports port 80 443
acl CONNECT method CONNECT

http_access deny !Safe_ports
http_access deny CONNECT !SSL_ports
http_access allow allowed_domains
# http_access allow allowed_ips
http_access deny all

# Laptop reaches the contour through a corporate proxy? Chain to it:
# cache_peer proxy.corp.example parent 8080 0 no-query default
# never_direct allow all

cache deny all
EOF
```

- Нужен нестандартный порт (например 8443) — добавь его в `SSL_ports` и `Safe_ports`.
- Ноутбук ходит в контур через корпоративный прокси (шаг 3.1) — раскомментируй
  `cache_peer`/`never_direct` и подставь его адрес.
- Протоколы, кроме HTTP/HTTPS (ssh, git по ssh, произвольный TCP), через этот прокси не
  проходят — это соответствует политике «без ssh».
- Журнал запросов — `/var/log/squid/access.log` внутри контейнера прокси.

### 5.4. Запуск прокси

```bash
podman run -d --name hermes-egress-proxy --restart unless-stopped \
  --network hermes-egress \
  -v "$CONTOUR/squid/squid.conf:/etc/squid/squid.conf:ro,z" \
  -v "$CONTOUR/squid/allowlist.txt:/etc/squid/allowlist.txt:ro,z" \
  docker.io/ubuntu/squid:6.10-24.04_edge
podman network connect hermes-internal hermes-egress-proxy
```

✅ **Проверка** — временный контейнер во внутренней сети (в образе Hermes есть Python, `curl`
может не быть):

```bash
P='import sys, urllib.request as u
try: print(sys.argv[1], "->", u.urlopen(sys.argv[1], timeout=10).status)
except Exception as e: print(sys.argv[1], "-> ERR", e)'
T="podman run --rm --network hermes-internal --entrypoint python3"
# 1) напрямую наружу — должна быть ошибка (нет маршрута / имя не резолвится)
$T localhost/hermes-agent:fork -c "$P" https://ya.ru
# 2) через прокси к неразрешённому — ошибка "Tunnel connection failed: 403"
$T -e HTTPS_PROXY=http://hermes-egress-proxy:3128 localhost/hermes-agent:fork -c "$P" https://ya.ru
# 3) через прокси к разрешённому — ответ сервиса (любой HTTP-код, не ошибка туннеля)
$T -e HTTPS_PROXY=http://hermes-egress-proxy:3128 localhost/hermes-agent:fork -c "$P" https://<домен из allowlist>/
podman exec hermes-egress-proxy tail -5 /var/log/squid/access.log
```

В журнале: `TCP_DENIED/403 CONNECT ya.ru:443` — заблокировано, `TCP_TUNNEL/200 CONNECT …` —
разрешено.

> Известное ограничение (из документации Hermes): во внутренней сети имена могут
> **разрешаться** через DNS, даже когда соединиться нельзя. Данные так не вынести в сколько-нибудь
> заметном объёме; для большинства моделей угроз это приемлемо.

---

## 6. Образ с GigaCode и провайдером `gigacode`

Собери слой поверх образа Hermes: GigaCode CLI + провайдер-плагин из форка, который делает
GigaCode моделью Hermes. Плагин кладётся в каталог встроенных провайдеров образа
(`/opt/hermes/plugins/model-providers/`, только для чтения), а не в данные — так агент не сможет
его подменить.

```bash
cp -r ~/src/hermes-agent-gigacode/deploy/gigacode/gigacode-acp "$CONTOUR/image/"
```

(если образ Hermes взят по варианту 4Б/4В и клона форка нет — склонируй форк только ради этого
каталога или перенеси его вместе с образом.)

Способ установки самого GigaCode — по результатам шага 3.2.

**Если GigaCode — npm-пакет** (из корпоративного npm-зеркала):

```bash
cat > "$CONTOUR/image/Containerfile" <<'EOF'
FROM localhost/hermes-agent:fork
USER root
ARG GIGACODE_PKG
RUN npm install -g "${GIGACODE_PKG}" && npm cache clean --force && gigacode --version
COPY gigacode-acp/ /opt/hermes/plugins/model-providers/gigacode-acp/
EOF
podman build -t localhost/hermes-gigacode:latest \
  --build-arg GIGACODE_PKG='<пакет>@<версия, как на ноутбуке>' \
  --build-arg HTTPS_PROXY="${HTTPS_PROXY:-}" --build-arg HTTP_PROXY="${HTTP_PROXY:-}" \
  "$CONTOUR/image"
```

Если npm-зеркало задано в `~/.npmrc` — скопируй его в `$CONTOUR/image/`, добавь перед `RUN`
строку `COPY .npmrc /root/.npmrc` и удали этот файл в конце того же `RUN`
(`&& rm /root/.npmrc`), чтобы он не остался в образе.

**Если GigaCode — самостоятельный каталог/архив:** скопируй каталог установки в
`$CONTOUR/image/gigacode/` и:

```bash
cat > "$CONTOUR/image/Containerfile" <<'EOF'
FROM localhost/hermes-agent:fork
USER root
COPY gigacode/ /opt/gigacode/
RUN ln -sf /opt/gigacode/<путь к запускаемому файлу> /usr/local/bin/gigacode && gigacode --version
COPY gigacode-acp/ /opt/hermes/plugins/model-providers/gigacode-acp/
EOF
podman build -t localhost/hermes-gigacode:latest "$CONTOUR/image"
```

В образе Node.js из комплекта Hermes (`node --version` внутри). Если GigaCode нужна другая версия
— сообщи пользователю, не подменяй Node в образе молча.

> Не копируй в образ токены и настройки GigaCode: образ может уйти дальше. Настройки кладутся в
> смонтированный каталог данных в шаге 7.2.

✅ **Проверка:**

```bash
podman run --rm --entrypoint gigacode localhost/hermes-gigacode:latest --version
podman run --rm --entrypoint ls localhost/hermes-gigacode:latest /opt/hermes/plugins/model-providers/gigacode-acp
```

---

## 7. Контейнер Hermes

### 7.1. Запуск

```bash
podman run -d --name hermes --restart unless-stopped \
  --network hermes-internal \
  --userns=keep-id:uid=10000,gid=10000 --user 0:0 \
  -v "$CONTOUR/data:/opt/data:z" \
  -v "$WORK:/opt/data/workspace:z" \
  -e HTTP_PROXY=http://hermes-egress-proxy:3128 \
  -e HTTPS_PROXY=http://hermes-egress-proxy:3128 \
  -e http_proxy=http://hermes-egress-proxy:3128 \
  -e https_proxy=http://hermes-egress-proxy:3128 \
  -e NO_PROXY=localhost,127.0.0.1,::1 \
  -e no_proxy=localhost,127.0.0.1,::1 \
  localhost/hermes-gigacode:latest gateway run
```

Если ACP-аргументы GigaCode из шага 3.2 — не `--acp` (например `--experimental-acp`), добавь
перед именем образа `-e HERMES_GIGACODE_ACP_ARGS='<ACP_ARGS>'`. Если бинарь называется не
`gigacode` или не лежит в `PATH` — `-e HERMES_GIGACODE_ACP_COMMAND=/полный/путь`.

- `gateway run` держит контейнер запущенным под супервизором s6 и выполняет задания по
  расписанию (cron) — это и есть «рутинные задачи». Мессенджеры подключать не обязательно.
- `--userns=keep-id:uid=10000,gid=10000 --user 0:0`: внутри контейнера Hermes работает как
  пользователь `hermes` (uid 10000), который на ноутбуке отображается в **твоего** пользователя,
  поэтому файлы, созданные агентом в `~/hermes-work`, принадлежат пользователю ноутбука.
  `--user 0:0` обязателен: загрузчик образа (s6) стартует от root контейнера; этот root на
  ноутбуке — непривилегированный subuid.
- `:z` — метка SELinux для двух выделенных папок (на системах без SELinux игнорируется). Никогда
  не монтируй `$HOME` целиком и системные каталоги.
- Порты наружу не публикуются: контейнер ничего не слушает для внешнего мира.

Автозапуск после перезагрузки (rootless): `systemctl --user enable --now podman-restart.service`;
чтобы работало без входа в графическую сессию — с согласия пользователя
`sudo loginctl enable-linger "$USER"`.

✅ **Проверка:**

```bash
podman ps --filter name=hermes --format '{{.Names}} {{.Status}}'
podman logs --tail 20 hermes
podman exec hermes hermes --version
podman exec -u hermes hermes touch /opt/data/workspace/.owner-test
ls -ln "$WORK/.owner-test"     # владелец — твой uid (id -u), а не 100000+
rm -f "$WORK/.owner-test"
```

Если Podman не понимает `keep-id:uid=...` (старее 4.3) или владелец не твой uid — пересоздай
контейнер (`podman rm -f hermes`), заменив `--userns=keep-id:uid=10000,gid=10000 --user 0:0` на
явное отображение (то же самое «вручную»: uid 10000 контейнера → твой пользователь, остальные →
subuid; нужен диапазон в `/etc/subuid` и `/etc/subgid` не меньше 65536):

```bash
  --uidmap 0:1:10000 --uidmap 10000:0:1 --uidmap 10001:10001:55535 \
  --gidmap 0:1:10000 --gidmap 10000:0:1 --gidmap 10001:10001:55535 \
```

Если и так не работает — покажи пользователю ошибку из `podman logs hermes`.

### 7.2. Настройки GigaCode внутри контейнера

Узнай, с каким `HOME` Hermes запускает дочерние процессы (в том числе `gigacode --acp`):

```bash
podman exec -u hermes -w /opt/hermes hermes /opt/hermes/.venv/bin/python -c \
  'import os; from hermes_constants import apply_subprocess_home_env as a; e=dict(os.environ); a(e); print(e["HOME"])'
```

Скопируй каталог настроек GigaCode из шага 3.2 (с политиками запретов и авторизацией) в этот
`HOME` внутри смонтированного каталога данных. Например, если `HOME` = `/opt/data`, а настройки у
тебя в `~/.gigacode`:

```bash
cp -a ~/.gigacode "$CONTOUR/data/.gigacode"      # внутри контейнера: /opt/data/.gigacode
chmod -R go-rwx "$CONTOUR/data/.gigacode"
```

(если `HOME` = `/opt/data/home` — соответственно `"$CONTOUR/data/home/.gigacode"`.) Если
GigaCode берёт авторизацию из переменных окружения — пересоздай контейнер (шаг 7.1), добавив
`--env-file "$CONTOUR/gigacode.env"` (файл с правами `600`).

Агент Hermes сможет прочитать эти файлы (это токен того же пользователя, а сеть закрыта
allowlist'ом). Если политика запрещает передавать токен агенту — обсуди с пользователем.

✅ **Проверка** — GigaCode изнутри контейнера отвечает через прокси и понимает ACP
(`<HOME>` — значение из команды выше):

```bash
G="podman exec -u hermes -e HOME=<HOME> -w /opt/data/workspace hermes"
$G gigacode <флаги headless из 3.2> "Ответь одним словом: работаю"
$G sh -c "printf '%s\n' '{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{\"protocolVersion\":1,\"clientCapabilities\":{}}}' | timeout 30 gigacode <ACP_ARGS> | head -c 400"; echo
podman exec hermes-egress-proxy tail -5 /var/log/squid/access.log   # API GigaCode — TCP_TUNNEL, не TCP_DENIED
```

Если GigaCode не ходит через прокси — найди в `--help`/документации его параметр прокси и задай
его в настройках из этого шага.

### 7.3. Настройки Hermes

Все команды — внутри контейнера (`podman exec hermes hermes ...`; обёртка сама запускает их от
пользователя `hermes`).

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
podman exec hermes-egress-proxy grep -c TCP_DENIED /var/log/squid/access.log
```

Отказы `TCP_DENIED` после запуска Hermes — нормально: Hermes в фоне пытается обновить каталог
моделей, проверить обновления плагинов и т.п., а прокси это отбрасывает. Плохо, только если отказ
приходится на нужное для работы (API GigaCode, разрешённые ресурсы) — тогда поправь
`allowlist.txt` и выполни `podman exec hermes-egress-proxy squid -k reconfigure`.

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
podman exec hermes-egress-proxy tail -20 /var/log/squid/access.log
```

Готово, если:

- [ ] `podman ps` показывает `hermes` и `hermes-egress-proxy` в статусе Up;
- [ ] `hermes auth status gigacode` — logged in, `hermes config get model.provider` — `gigacode`;
- [ ] ответы и вызовы инструментов идут через GigaCode (тест из шага 7.3 вывел
      `HERMES-ON-GIGACODE`);
- [ ] GigaCode работает изнутри контейнера; файлы появились в `~/hermes-work/demo` и принадлежат
      пользователю;
- [ ] неразрешённые адреса недоступны (напрямую — нет маршрута, через прокси — `TCP_DENIED`);
- [ ] разрешённые ресурсы доступны; в журнале прокси успешные соединения (`TCP_TUNNEL`) — только
      к API GigaCode и разрешённым ресурсам;
- [ ] агенту не видны файлы ноутбука вне `~/hermes-contour/data` и `~/hermes-work`
      (`podman exec hermes ls /home` — нет каталога пользователя).

Если пользователь назвал рутинные задачи (шаг 3.3) — заведи их как задания по расписанию
(`podman exec -it hermes hermes cron --help`, или попроси Hermes в чате: «каждый будний день в
9:00 делай …») и проверь первое срабатывание.

Удали `~/hermes-work/demo` (спроси) и выдай пользователю памятку:

```text
Чат с агентом:         podman exec -it hermes hermes          (полноэкранный: ... hermes --tui)
   удобный алиас:      alias hermes='podman exec -it hermes hermes'   (в ~/.bashrc)
Рабочая папка:         ~/hermes-work        (внутри контейнера: /opt/data/workspace)
Данные Hermes:         ~/hermes-contour/data
Разрешённые ресурсы:   ~/hermes-contour/squid/allowlist.txt
   применить правку:   podman exec hermes-egress-proxy squid -k reconfigure
Журнал выхода в сеть:  podman exec hermes-egress-proxy tail -f /var/log/squid/access.log
                       (TCP_DENIED = заблокировано, TCP_TUNNEL = разрешено)
Задания по расписанию: podman exec -it hermes hermes cron list
Логи Hermes:           podman exec hermes hermes logs
Остановить/запустить:  podman stop hermes hermes-egress-proxy
                       podman start hermes-egress-proxy hermes
```

**Обновление Hermes:** `git pull` в `~/src/hermes-agent-gigacode`, затем шаги 4А и 6
(пересобрать оба образа; шаг 6 заново копирует свежий плагин `gigacode-acp`) и пересоздать контейнер: `podman rm -f hermes` и команда из шага 7.1.
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
| `short-name resolution enforced` | пиши образы полным именем: `docker.io/...`, `localhost/...` |
| Контейнер `hermes` перезапускается | `podman logs hermes`; ошибка про `--user`/`--userns` — вариант с `--uidmap`/`--gidmap` из шага 7.1 |
| `Permission denied` на `/opt/data` | SELinux: у томов должен быть `:z`; пересоздай контейнер |
| Файлы в `~/hermes-work` чужого владельца | шаг 7.1 (`--userns=keep-id:uid=10000,gid=10000 --user 0:0`); уже созданные: `podman unshare chown -R 10000:10000 ~/hermes-work` |
| Hermes не получает ответа модели | журнал прокси: API GigaCode в `TCP_DENIED`? → в allowlist и `squid -k reconfigure` |
| TLS-ошибки к внутренним ресурсам | корпоративный корневой сертификат: смонтируй CA-бандл ноутбука (`-v <бандл>:/etc/ssl/certs/ca-certificates.crt:ro,z`) и задай `-e SSL_CERT_FILE=… -e REQUESTS_CA_BUNDLE=… -e NODE_EXTRA_CA_CERTS=…` (тот же путь) |
| GigaCode «нет сети» | он игнорирует `HTTPS_PROXY` → его собственная настройка прокси (шаг 7.2) |
| `Could not find the 'gigacode' CLI` | `gigacode` не в `PATH` контейнера → `-e HERMES_GIGACODE_ACP_COMMAND=/полный/путь` (шаг 7.1) |
| `ACP transport not supported by 'gigacode'` | в `--help` нет `--acp` → верные ACP-аргументы в `-e HERMES_GIGACODE_ACP_ARGS=...` (шаг 7.1) |
| `Copilot ACP process exited early: …` / `Timed out waiting for Copilot ACP` | это общий ACP-транспорт Hermes (сообщения названы по Copilot): GigaCode упал или завис — текст ошибки после двоеточия; проверь его авторизацию (`HOME`, шаг 7.2) и рукопожатие из шага 7.2 |
| Провайдера `gigacode` нет в `hermes model` | плагин не попал в образ: проверка из шага 6, пересобери образ |
| Агент «забывает» начало разговора или GigaCode ругается на длину | `model.context_length` (шаг 7.3) меньше/больше реального; отключи лишние наборы инструментов |
| Имя `hermes-egress-proxy` не резолвится | `podman network connect hermes-internal hermes-egress-proxy`; оба контейнера должны быть в `hermes-internal` |
| После перезагрузки ничего не запущено | `systemctl --user enable --now podman-restart.service`, `loginctl enable-linger` |

## Откат

```bash
podman rm -f hermes hermes-egress-proxy
podman network rm hermes-internal hermes-egress
podman rmi localhost/hermes-gigacode:latest localhost/hermes-agent:fork
```

Данные остаются в `~/hermes-contour` и `~/hermes-work`; удалять их — только по прямой просьбе
пользователя.
