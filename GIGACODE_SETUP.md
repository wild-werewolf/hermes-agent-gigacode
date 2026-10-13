# Инструкция для GigaCode: собрать, развернуть и запустить Hermes Agent на этом Linux-ноутбуке

> **Кому:** агенту GigaCode, запущенному прямо на Linux-ноутбуке пользователя (не по SSH, не в
> облаке). **Что сделать:** собрать Hermes Agent из этого форка, настроить так, чтобы все команды
> агентов Hermes выполнялись в контейнерах Podman, при этом из контейнера были доступны
> (1) GigaCode, работающий на хосте, и (2) локальная сеть/сервисы ноутбука. В конце пользователь
> должен открыть терминал, набрать `hermes` и начать работать.
>
> Пользователь даёт задание примерно так: «Прочитай `GIGACODE_SETUP.md` в форке
> `wild-werewolf/hermes-agent-gigacode` и выполни его».

---

## 0. Правила работы (прочитай до начала)

1. **Выполняй шаги по порядку** и после каждого шага делай проверку из блока «✅ Проверка».
   Не переходи дальше, пока проверка не прошла.
2. **Ничего не удаляй и не перезаписывай без бэкапа.** Если `~/.hermes` уже существует — сначала
   сделай `hermes backup` (если `hermes` есть) или `cp -a ~/.hermes ~/.hermes.bak.$(date +%s)`.
3. **`sudo` — только с согласия пользователя.** Перед любой командой с `sudo` скажи, что и зачем
   ставишь, и дождись «да».
4. **Не выдумывай флаги.** Где инструкция говорит «узнай» (флаги headless-режима GigaCode, путь к
   бинарю, имя пакета в дистрибутиве) — выясни на этой машине (`--help`, `command -v`, пакетный
   менеджер), а не по памяти.
5. **Секреты** (API-ключи, токены) не печатай в чат и не коммить. Ключи Hermes живут в
   `~/.hermes/.env`, ключи моста — в `~/.config/gigacode-bridge/env` (права `600`).
6. Если шаг не получается — смотри раздел «Диагностика» в конце, а если и это не помогло,
   остановись и опиши пользователю точную ошибку и что уже сделано.

---

## 1. Целевая архитектура

```
 Ноутбук (Linux, пользователь $USER)
 ┌──────────────────────────────────────────────────────────────────────────────┐
 │  hermes (CLI / TUI / gateway)  ── процесс на хосте: мозг агента, LLM-запросы │
 │        │  terminal.backend: docker  (бинарь = podman, rootless)              │
 │        ▼                                                                     │
 │  ┌──────────────── контейнер Podman (песочница Hermes) ────────────────────┐ │
 │  │  все terminal / file / execute_code агентов и субагентов               │ │
 │  │  --network=host  → видит localhost ноутбука, VPN, DNS, прокси          │ │
 │  │  ~/hermes-work   → смонтирован по ТОМУ ЖЕ пути, что и на хосте         │ │
 │  │  gigacode-host "задача"  ──ssh 127.0.0.1:22022 (ключ, forced command)──┼─┼─┐
 │  └────────────────────────────────────────────────────────────────────────┘ │ │
 │                                                                              │ │
 │  gigacode-bridge (sshd пользователя, только 127.0.0.1, только ForceCommand) ◄┼─┘
 │        └─► bridge.sh ─► gigacode (headless) в каталоге внутри ~/hermes-work  │
 └──────────────────────────────────────────────────────────────────────────────┘
```

Почему так:

- **Hermes ставится на хост**, а изоляция делается его штатным терминальным бэкендом `docker`,
  который из коробки работает с Podman (`HERMES_DOCKER_BINARY=podman`). Один долгоживущий
  контейнер обслуживает все сессии и субагентов (`delegate_task`) — это стандартный режим Hermes.
- **Сеть ноутбука:** `docker_extra_args: ["--network=host"]` — контейнер делит сетевой стек с
  хостом: доступны сервисы на `127.0.0.1`, корпоративный VPN, внутренние DNS-имена, прокси.
- **GigaCode снаружи контейнера:** мост через отдельный loopback-sshd, запущенный от имени
  пользователя (не системный sshd, без root). Ключ из контейнера умеет ровно одно — запустить
  `bridge.sh`, который запускает GigaCode в headless-режиме, и только в каталоге внутри общего
  workspace. Произвольную команду на хосте этим ключом выполнить нельзя.
- **Общий workspace** `~/hermes-work` смонтирован в контейнер по тому же абсолютному пути, поэтому
  путь, который агент передаёт в GigaCode, на хосте означает тот же каталог.

> ⚠️ Честное предупреждение для пользователя: GigaCode на хосте сам по себе умеет менять файлы и
> выполнять команды. Мост ограничивает *каталог запуска*, но не то, что GigaCode сделает внутри
> своей сессии. Режим подтверждений GigaCode (approval mode) выбирай осознанно — см. шаг 6.3.

Альтернатива (не по умолчанию): запустить весь Hermes в контейнере Podman (`docker-compose.yml` в
корне репозитория уже использует `network_mode: host`). Делай так, только если пользователь прямо
попросит, — тогда мост из шага 6 нужен тот же, а установка из шага 3 заменяется сборкой образа
(`podman build -t hermes-agent .`) и запуском по `website/docs/user-guide/docker.md`.

---

## 2. Разведка и вопросы пользователю

### 2.1. Собери факты о машине

```bash
uname -a; cat /etc/os-release | head -5
id -u; id -g; echo "$HOME" "$SHELL"
command -v git curl tar sha256sum podman docker ssh ssh-keygen sshd /usr/sbin/sshd 2>/dev/null
podman --version
podman info --format 'rootless={{.Host.Security.Rootless}} cgroup={{.Host.CgroupsVersion}}'
getenforce 2>/dev/null || echo "SELinux: нет"
env | grep -i -E '^(https?|no|all)_proxy=' || echo "прокси в окружении не задан"
ls -ld ~/.hermes 2>/dev/null && echo "ВНИМАНИЕ: ~/.hermes уже есть — нужен бэкап"
ss -ltn | grep -E ':22022\b' && echo "ВНИМАНИЕ: порт 22022 занят — выбери другой"
```

### 2.2. Найди GigaCode и его headless-режим

```bash
command -v gigacode || ls ~/.local/bin ~/.npm-global/bin 2>/dev/null | grep -i giga
gigacode --version
gigacode --help
```

Из `--help` определи и запиши:

- `GIGACODE_BIN` — абсолютный путь к бинарю (`readlink -f "$(command -v gigacode)"`);
- `GIGACODE_ARGS` — флаги **неинтерактивного** запуска «выполни задачу и выйди», куда задача
  передаётся последним аргументом (например, `-p` / `--prompt`; проверь по `--help`!), и при
  необходимости флаг режима подтверждений;
- откуда GigaCode берёт авторизацию: файл в `$HOME` (тогда ничего делать не нужно) или
  переменные окружения (тогда их нужно будет положить в `~/.config/gigacode-bridge/env`).

Проверь, что headless-запуск реально работает **из обычного терминала**:

```bash
cd "$(mktemp -d)" && "$GIGACODE_BIN" $GIGACODE_ARGS "Ответь одним словом: ок"
```

### 2.3. Спроси пользователя (одним сообщением)

1. **Какую LLM использовать для Hermes?** Варианты: ключ провайдера, который уже есть (OpenRouter,
   Anthropic, OpenAI и т.п.); GigaChat через OpenAI-совместимый прокси на `localhost`
   (например `gpt2giga`); локальная модель (Ollama/LM Studio/vLLM). Hermes без LLM не запустится.
2. **Каталог общего workspace** — по умолчанию `~/hermes-work`. Туда же пользователь кладёт
   проекты, над которыми будут работать агенты и GigaCode.
3. **Можно ли ставить пакеты через `sudo`** (нужны `openssh-server` для моста и, возможно,
   `libatomic`)?
4. **Нужен ли мессенджер** (Telegram и т.п.) сейчас или достаточно CLI.

Дальше в командах используются переменные — задай их в своей shell-сессии:

```bash
export HERMES_FORK_URL="https://github.com/wild-werewolf/hermes-agent-gigacode.git"
export WORK="$HOME/hermes-work"          # или ответ пользователя
export BRIDGE_PORT=22022                 # или другой свободный порт
export BRIDGE_DIR="$HOME/.config/gigacode-bridge"
export CLIENT_DIR="$HOME/.local/share/gigacode-bridge/client"
```

Если твоя оболочка не сохраняет переменные между командами — запиши этот блок в файл
(например `~/.config/hermes-gigacode-setup.env`) и делай `source` этого файла перед каждым шагом;
туда же допиши `WORK_REAL` и `SSHD_BIN`, когда они появятся в шагах 6.1–6.2.

---

## 3. Сборка и установка Hermes из форка

Установщик клонирует исходники в `~/.hermes/hermes-agent`, сам скачивает зафиксированные версии
`uv`, Python 3.14, Node.js и т.д. (через PM, по `pm/lock.json`) и создаёт обёртку
`~/.local/bin/hermes`. Системный Python/Node не нужны.

```bash
# бэкап, если Hermes уже стоял
[ -d ~/.hermes ] && { command -v hermes >/dev/null && hermes backup || cp -a ~/.hermes ~/.hermes.bak.$(date +%s); }

mkdir -p ~/src && cd ~/src
[ -d hermes-agent-gigacode ] || git clone "$HERMES_FORK_URL" hermes-agent-gigacode
cd hermes-agent-gigacode && git pull --ff-only

HERMES_REPO_URL="$HERMES_FORK_URL" bash scripts/install.sh --branch main --non-interactive
```

- `HERMES_REPO_URL` важен: без него установщик возьмёт апстрим `NousResearch/hermes-agent`,
  а не форк. Он же запоминается для `hermes update`.
- `--non-interactive` — пропустить мастер настройки: настроим явно в шагах 4–7.
- Если установщик попросит `libatomic` (минимальные Debian/Fedora) — это ожидаемо, спроси
  пользователя про `sudo`.
- Подробный лог: `~/.hermes/logs/install.log`.

Сделай `hermes` доступным в текущей сессии:

```bash
export PATH="$HOME/.local/bin:$PATH"
hash -r
```

✅ **Проверка:**

```bash
hermes --version
git -C ~/.hermes/hermes-agent remote get-url origin   # должен быть форк wild-werewolf/...
```

---

## 4. Модель (LLM) для Hermes

Интерактивный `hermes model` — это меню; если не можешь им управлять, попроси пользователя
запустить его в отдельном терминале **или** настрой неинтерактивно:

**Провайдер с ключом** (пример — OpenRouter; имя переменной для другого провайдера смотри в
`website/docs/integrations/providers.md`):

```bash
hermes config set OPENROUTER_API_KEY '<ключ от пользователя>'   # уходит в ~/.hermes/.env
hermes config set model.provider openrouter
hermes config set model.default '<id модели>'
```

**Любой OpenAI-совместимый endpoint** (GigaChat через прокси на хосте, Ollama, vLLM и т.п.):

```bash
hermes config set model.provider custom
hermes config set model.base_url 'http://127.0.0.1:<порт>/v1'
hermes config set model.default '<имя модели на этом endpoint>'
hermes config set GIGA_PROXY_KEY '<ключ, если endpoint его требует>'   # имя переменной — любое
hermes config set model.key_env GIGA_PROXY_KEY
```

Для GigaChat: GigaChat API не OpenAI-совместим напрямую, нужен прокси (например `gpt2giga`).
Если пользователь выбрал этот путь — поставь и запусти прокси на хосте по его документации
(`--help`), убедись, что `curl -s http://127.0.0.1:<порт>/v1/models` отвечает, и только потом
прописывай `base_url`. Процесс Hermes работает на хосте, поэтому `127.0.0.1` здесь — сам ноутбук.

✅ **Проверка:** `hermes chat --oneshot -q "Ответь одним словом: работаю"` печатает ответ модели.

---

## 5. Песочница: Podman + сеть ноутбука

### 5.1. Podman готов к работе без root

```bash
podman info --format '{{.Host.Security.Rootless}}'   # должно быть true
podman run --rm docker.io/library/alpine:3 echo podman-ok
```

Если rootless не настроен (нет записей в `/etc/subuid` и `/etc/subgid` для `$USER`) — скажи
пользователю и с его согласия: `sudo usermod --add-subuids 100000-165535 --add-subgids 100000-165535 "$USER"`,
затем `podman system migrate`.

### 5.2. Образ песочницы

По умолчанию Hermes использует `nousresearch/hermes-sandbox:desktop` (Debian, Python + Node,
дисплейный стек для браузера и Bot Screen). Podman не любит короткие имена образов без реестра —
укажи полное имя и скачай заранее (образ большой):

```bash
podman pull docker.io/nousresearch/hermes-sandbox:desktop
podman run --rm docker.io/nousresearch/hermes-sandbox:desktop sh -c 'command -v ssh bash python3 node'
```

Если в образе нет `ssh` — запомни это: в шаге 7.3 поставим `openssh-client` внутрь контейнера.

### 5.3. Конфигурация терминального бэкенда

```bash
mkdir -p "$WORK"

hermes config set terminal.backend docker
hermes config set terminal.docker_image docker.io/nousresearch/hermes-sandbox:desktop
hermes config set terminal.docker_network true
hermes config set terminal.docker_extra_args '["--network=host"]'
hermes config set HERMES_DOCKER_BINARY "$(command -v podman)"
```

`HERMES_DOCKER_BINARY` попадает в `~/.hermes/.env` и заставляет Hermes использовать Podman, даже
если на машине стоит и `docker`.

Тома (общий workspace + клиент моста) пропишем одним списком в шаге 7.1, когда клиентский каталог
моста уже будет создан.

### 5.4. Прокси и корпоративные сертификаты (только если нужны)

С `--network=host` контейнер видит ту же сеть, но **не** переменные окружения хоста и не
системное хранилище сертификатов хоста. Если в шаге 2.1 нашлись `*_proxy`-переменные:

```bash
hermes config set terminal.docker_forward_env '["HTTP_PROXY","HTTPS_PROXY","NO_PROXY","http_proxy","https_proxy","no_proxy"]'
```

Если внутренние сервисы используют корпоративный/национальный корневой сертификат (например,
«Russian Trusted Root CA»), и `curl https://<внутренний сервис>` в контейнере падает с ошибкой TLS
при том, что на хосте работает, — смонтируй CA-бандл хоста поверх бандла образа (Debian-путь) и
укажи его инструментам. Добавь в список томов шага 7.1 строку
`"<CA-бандл хоста>:/etc/ssl/certs/ca-certificates.crt:ro"` (Fedora:
`/etc/pki/tls/certs/ca-bundle.crt`, Debian/Ubuntu: `/etc/ssl/certs/ca-certificates.crt`) и:

```bash
hermes config set terminal.docker_env '{"SSL_CERT_FILE": "/etc/ssl/certs/ca-certificates.crt", "REQUESTS_CA_BUNDLE": "/etc/ssl/certs/ca-certificates.crt", "NODE_EXTRA_CA_CERTS": "/etc/ssl/certs/ca-certificates.crt"}'
```

> Не включай `proxy.enabled` (egress-прокси Hermes) в этой конфигурации: он намеренно запрещает
> переопределять сеть и прокси контейнера и конфликтует с `--network=host`.

---

## 6. Мост «контейнер → GigaCode на хосте»

### 6.1. sshd для моста

Нужен бинарь `sshd` (пакет `openssh-server`). Если его нет — с согласия пользователя:
Fedora/RHEL `sudo dnf install openssh-server`; Debian/Ubuntu `sudo apt install openssh-server`;
Arch `sudo pacman -S openssh`.

> На Debian/Ubuntu установка пакета сама включает **системный** sshd на `0.0.0.0:22`. Если до
> этого sshd у пользователя не было — спроси и выключи его: `sudo systemctl disable --now ssh`.
> Нашему мосту системный sshd не нужен.

```bash
SSHD_BIN="$(command -v sshd || ls /usr/sbin/sshd /usr/bin/sshd 2>/dev/null | head -1)"; echo "$SSHD_BIN"
```

### 6.2. Ключи и конфигурация

```bash
mkdir -p "$BRIDGE_DIR" "$CLIENT_DIR"
chmod 700 "$BRIDGE_DIR" "$(dirname "$CLIENT_DIR")" "$CLIENT_DIR"

# ключ хоста моста и клиентский ключ контейнера
[ -f "$BRIDGE_DIR/ssh_host_ed25519_key" ] || ssh-keygen -q -t ed25519 -N '' -C gigacode-bridge-host -f "$BRIDGE_DIR/ssh_host_ed25519_key"
[ -f "$CLIENT_DIR/id_ed25519" ]           || ssh-keygen -q -t ed25519 -N '' -C hermes-sandbox       -f "$CLIENT_DIR/id_ed25519"

WORK_REAL="$(realpath -e "$WORK")"

# значения для моста (подставь найденные в шаге 2.2)
cat > "$BRIDGE_DIR/bridge.conf" <<EOF
WORKSPACE='$WORK_REAL'
GIGACODE_BIN='<GIGACODE_BIN из шага 2.2>'
HOST_PATH='$PATH'
EOF
# флаги headless-запуска: по одному аргументу на строку, БЕЗ самой задачи
printf '%s\n' <флаги из шага 2.2, например: -p> > "$BRIDGE_DIR/gigacode.args"

# переменные окружения для GigaCode (авторизация, прокси), если нужны; иначе файл пустой
touch "$BRIDGE_DIR/env" && chmod 600 "$BRIDGE_DIR/env"
```

`HOST_PATH` фиксирует твой текущий `PATH`: sshd запускает команды с урезанным `PATH`, а GigaCode,
поставленный через npm/nvm, без своего `node` не стартует. Проверь, что в `bridge.conf` нет
плейсхолдеров `<...>`.

Если GigaCode берёт авторизацию из переменных окружения — запиши их в `$BRIDGE_DIR/env` строками
`export ИМЯ='значение'` (туда же `HTTPS_PROXY` и т.п., если GigaCode ходит через прокси).

**Скрипт моста** (выполняется на хосте; задачу и каталог получает только через stdin, командную
строку ssh игнорирует — подставить через неё произвольную команду нельзя):

```bash
cat > "$BRIDGE_DIR/bridge.sh" <<'EOF'
#!/usr/bin/env bash
# ForceCommand of the gigacode-bridge sshd: runs host GigaCode headless for the Hermes sandbox.
# stdin: line 1 = absolute working directory inside WORKSPACE, the rest = task prompt.
# SSH_ORIGINAL_COMMAND is ignored on purpose: the sandbox must not choose what runs here.
set -euo pipefail
dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$dir/bridge.conf"
export PATH="$HOST_PATH"
. "$dir/env"
mapfile -t args < "$dir/gigacode.args"

IFS= read -r workdir || { echo "gigacode-bridge: empty request" >&2; exit 2; }
prompt="$(cat)"
[ -n "${prompt//[[:space:]]/}" ] || { echo "gigacode-bridge: empty prompt" >&2; exit 2; }
real="$(realpath -e -- "$workdir" 2>/dev/null)" || { echo "gigacode-bridge: no such directory: $workdir" >&2; exit 2; }
case "$real/" in
  "$WORKSPACE"/*) ;;
  *) echo "gigacode-bridge: $real is outside $WORKSPACE" >&2; exit 3 ;;
esac

printf '%s cwd=%s prompt_bytes=%s\n' "$(date -Is)" "$real" "${#prompt}" >> "$dir/bridge.log"
cd "$real"
exec timeout 3600 "$GIGACODE_BIN" "${args[@]}" "$prompt" </dev/null
EOF
chmod 700 "$BRIDGE_DIR/bridge.sh"
```

**authorized_keys и sshd_config:**

```bash
printf 'restrict,command="%s" %s\n' "$BRIDGE_DIR/bridge.sh" "$(cat "$CLIENT_DIR/id_ed25519.pub")" > "$BRIDGE_DIR/authorized_keys"
chmod 600 "$BRIDGE_DIR/authorized_keys"

cat > "$BRIDGE_DIR/sshd_config" <<EOF
# gigacode-bridge: per-user sshd, loopback only, one forced command.
Port $BRIDGE_PORT
ListenAddress 127.0.0.1
HostKey $BRIDGE_DIR/ssh_host_ed25519_key
PidFile $BRIDGE_DIR/sshd.pid
AuthorizedKeysFile $BRIDGE_DIR/authorized_keys
AllowUsers $USER
PubkeyAuthentication yes
PasswordAuthentication no
KbdInteractiveAuthentication no
UsePAM no
PermitRootLogin no
PermitTTY no
PermitUserEnvironment no
AllowTcpForwarding no
AllowStreamLocalForwarding no
AllowAgentForwarding no
X11Forwarding no
PermitTunnel no
ForceCommand $BRIDGE_DIR/bridge.sh
ClientAliveInterval 60
LogLevel INFO
EOF

"$SSHD_BIN" -t -f "$BRIDGE_DIR/sshd_config" && echo "sshd_config ok"
```

### 6.3. Режим подтверждений GigaCode

В headless-режиме GigaCode не может спросить пользователя. Обсуди с пользователем и выбери:

- **безопасно:** режим, в котором GigaCode только предлагает правки/отвечает, без выполнения
  команд (если такой есть в `--help`);
- **автономно:** авто-подтверждение действий — тогда GigaCode сможет менять файлы и запускать
  команды на хосте в пределах своей сессии.

Нужный флаг добавь отдельной строкой в `$BRIDGE_DIR/gigacode.args`.

### 6.4. Автозапуск моста (systemd --user)

```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/gigacode-bridge.service <<EOF
[Unit]
Description=GigaCode bridge for the Hermes Podman sandbox (loopback sshd)

[Service]
ExecStart=$SSHD_BIN -D -e -f $BRIDGE_DIR/sshd_config
Restart=on-failure

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now gigacode-bridge.service
```

Чтобы мост работал и без открытой графической сессии — с согласия пользователя:
`sudo loginctl enable-linger "$USER"`.

`known_hosts` для контейнера (строго по ключу моста, без «доверия при первом подключении»):

```bash
printf '[127.0.0.1]:%s %s\n' "$BRIDGE_PORT" "$(cut -d' ' -f1,2 "$BRIDGE_DIR/ssh_host_ed25519_key.pub")" > "$CLIENT_DIR/known_hosts"
```

**Клиент для контейнера** (`gigacode-host`):

```bash
cat > "$CLIENT_DIR/gigacode-host" <<EOF
#!/usr/bin/env bash
# Hermes sandbox → GigaCode on the host via gigacode-bridge.
# Usage: gigacode-host [-C DIR] "task"     or     echo "task" | gigacode-host [-C DIR]
set -euo pipefail
dir="\$PWD"
if [ "\${1:-}" = "-C" ]; then dir="\$2"; shift 2; fi
dir="\$(cd "\$dir" && pwd -P)"
if [ \$# -gt 0 ]; then prompt="\$*"; else prompt="\$(cat)"; fi
{ printf '%s\n' "\$dir"; printf '%s' "\$prompt"; } | ssh -T -p $BRIDGE_PORT \\
  -i /opt/gigacode-bridge/id_ed25519 -o IdentitiesOnly=yes -o BatchMode=yes \\
  -o UserKnownHostsFile=/opt/gigacode-bridge/known_hosts -o StrictHostKeyChecking=yes \\
  -o ServerAliveInterval=30 $USER@127.0.0.1
EOF
chmod 755 "$CLIENT_DIR/gigacode-host"
```

✅ **Проверка моста с хоста** (до контейнера):

```bash
systemctl --user is-active gigacode-bridge.service
mkdir -p "$WORK/_bridge_test"
sed "s#/opt/gigacode-bridge#$CLIENT_DIR#g" "$CLIENT_DIR/gigacode-host" > "$BRIDGE_DIR/gigacode-host-local" && chmod +x "$BRIDGE_DIR/gigacode-host-local"
"$BRIDGE_DIR/gigacode-host-local" -C "$WORK/_bridge_test" "Ответь одним словом: мост"     # ответ GigaCode
"$BRIDGE_DIR/gigacode-host-local" -C /etc "test"   # должно упасть: outside $WORKSPACE (exit 3)
ssh -T -p "$BRIDGE_PORT" -i "$CLIENT_DIR/id_ed25519" -o IdentitiesOnly=yes \
  -o UserKnownHostsFile="$CLIENT_DIR/known_hosts" "$USER@127.0.0.1" 'id' </dev/null
# ↑ должно НЕ выполнить id (сработает bridge.sh и скажет "empty request")
rm -f "$BRIDGE_DIR/gigacode-host-local"
```

---

## 7. Подключение моста и workspace к песочнице

### 7.1. Тома

Workspace монтируется **по тому же пути**, что на хосте. На SELinux-системах (`getenforce` =
`Enforcing`, обычно Fedora/RHEL) к опциям тома нужен суффикс `z`, иначе контейнер не прочитает
файлы; на остальных — без него. Никогда не ставь `z`/`Z` на `$HOME` целиком или системные каталоги.

```bash
Z=""; [ "$(getenforce 2>/dev/null)" = "Enforcing" ] && Z=",z"
hermes config set terminal.docker_volumes "[
  \"$WORK_REAL:$WORK_REAL:rw$Z\",
  \"$CLIENT_DIR:/opt/gigacode-bridge:ro$Z\",
  \"$CLIENT_DIR/gigacode-host:/usr/local/bin/gigacode-host:ro$Z\"
]"
hermes config set terminal.docker_mount_cwd_to_workspace false
hermes config get terminal.docker_volumes
```

`docker_mount_cwd_to_workspace` оставь `false`: при `true` каталог запуска `hermes` монтируется
в контейнер как `/workspace` — путь, которого нет на хосте, и мост такой каталог отклонит.
Агенты работают с проектами по их «хостовым» путям внутри `$WORK` — это им объясняет навык из
шага 8.

Если на шаге 5.4 понадобился CA-бандл — добавь его строкой в этот же список (дублирующий ключ
`docker_volumes:` в YAML молча затирает предыдущий — всегда один список).

Контейнер rootless Podman работает от `root` внутри, который на хосте отображается в твоего
пользователя, — файлы, созданные агентом в `$WORK`, будут принадлежать тебе.
`docker_run_as_host_user` оставь `false`.

### 7.2. Пересоздай контейнер песочницы

Изменение образа/томов применяется к новому контейнеру. Если контейнер Hermes уже успел
создаться — удали его (данные в смонтированном `$WORK` не пострадают):

```bash
podman ps -a --filter label=hermes-agent=1 --format '{{.ID}} {{.Names}} {{.Status}}'
podman ps -aq --filter label=hermes-agent=1 | xargs -r podman rm -f
```

### 7.3. Проверка из песочницы

```bash
cd "$WORK"
hermes chat --oneshot --yolo -q "Выполни в терминале по очереди и покажи вывод как есть: (1) head -2 /etc/os-release; id; (2) ls -la $WORK_REAL /opt/gigacode-bridge; (3) command -v ssh gigacode-host; (4) curl -sS -o /dev/null -w '%{http_code}\n' https://ya.ru"
podman ps --filter label=hermes-agent=1 --format '{{.Names}} {{.Image}}'
```

Ожидаемо: команды выполнились в контейнере (`podman ps` показывает его, `/etc/os-release` —
Debian образа песочницы, а не дистрибутив ноутбука), `$WORK` виден по тому же пути, `/opt/gigacode-bridge` содержит ключ и `known_hosts`, `ssh` и `gigacode-host` найдены,
внешний HTTPS отвечает.

Если `ssh` в контейнере нет — поставь в работающий контейнер (контейнер персистентный, установка
переживёт сессии; после `podman rm` повторить):

```bash
C=$(podman ps -q --filter label=hermes-agent=1 | head -1)
podman exec "$C" sh -c 'apt-get update && apt-get install -y --no-install-recommends openssh-client'
```

Проверь сеть ноутбука и мост из контейнера:

```bash
C=$(podman ps -q --filter label=hermes-agent=1 | head -1)
podman exec "$C" sh -c 'getent hosts <внутреннее имя, которое резолвится на хосте> ; curl -sS -m5 http://127.0.0.1:<порт локального сервиса>/ | head -c 200'
podman exec -w "$WORK_REAL" "$C" gigacode-host "Ответь одним словом: из контейнера"
```

---

## 8. Навык Hermes «gigacode-host»

Чтобы агенты Hermes знали про мост, создай пользовательский навык (каталог навыков Hermes
автоматически монтируется в песочницу только для чтения):

```bash
mkdir -p ~/.hermes/skills/gigacode-host
cat > ~/.hermes/skills/gigacode-host/SKILL.md <<EOF
---
name: gigacode-host
description: "Delegate coding tasks to GigaCode running on the host via gigacode-host."
version: 1.0.0
author: local
platforms: [linux]
metadata:
  hermes:
    tags: [Coding-Agent, GigaCode, Delegation]
    related_skills: [opencode, claude-code, codex]
---

# GigaCode on the host

Terminal commands run in a Podman sandbox. GigaCode runs on the host, outside the sandbox, and
is reachable only through the \`gigacode-host\` command.

## When to Use

- The user asks to use GigaCode.
- A coding task (implement, refactor, review, explain code) on a project in $WORK_REAL.

## How

1. Work only inside $WORK_REAL; the host sees the same files at the same path.
2. Run: \`gigacode-host -C <absolute project dir> "<self-contained task>"\`.
   For long tasks pipe the prompt: \`gigacode-host -C <dir> < task.md\`.
3. GigaCode cannot ask follow-up questions: give it the goal, constraints, files and the
   expected result in one prompt.
4. It runs up to 60 minutes; start it with background=true for long tasks and poll.
5. Review its changes with \`git -C <dir> diff\` before reporting back.

## Errors

- \`outside ...\`: the directory is not under $WORK_REAL.
- \`Connection refused\`: the bridge is down; ask the user to run
  \`systemctl --user restart gigacode-bridge\`.
EOF
```

✅ **Проверка:** `hermes skills list | grep gigacode-host` (или `/skills` в чате).

---

## 9. Финальная проверка и передача пользователю

```bash
hermes doctor
cd "$WORK" && mkdir -p demo && cd demo && git init -q 2>/dev/null
hermes chat --oneshot --yolo -q 'Используя навык gigacode-host, попроси GigaCode создать в текущем каталоге hello.py, печатающий "привет", затем запусти его в терминале и покажи вывод.'
ls -la "$WORK/demo"; tail -3 "$BRIDGE_DIR/bridge.log"
```

Готово, если:

- [ ] `hermes doctor` без ошибок по модели и терминальному бэкенду;
- [ ] команды агента выполняются в контейнере Podman (`podman ps --filter label=hermes-agent=1`);
- [ ] из контейнера доступны localhost-сервисы, внутренние имена и интернет ноутбука;
- [ ] `gigacode-host` из контейнера запускает GigaCode на хосте, в `bridge.log` есть запись;
- [ ] каталог вне `$WORK` мостом отклоняется;
- [ ] файлы из демо-задачи появились в `$WORK/demo` и принадлежат пользователю.

Убери тестовые каталоги (`$WORK/_bridge_test`, временный каталог из шага 2.2; `$WORK/demo` — спроси) и
выдай пользователю памятку:

```text
Запуск:            hermes                (чат в терминале; hermes --tui — полноэкранный интерфейс)
Рабочие проекты:   ~/hermes-work         (видны и агентам в контейнере, и GigaCode на хосте)
Сменить модель:    hermes model
Веб-панель:        hermes dashboard      (только localhost)
Мессенджеры:       hermes gateway setup
Обновление:        hermes update         (берёт код из форка wild-werewolf/hermes-agent-gigacode)
Логи Hermes:       hermes logs
Мост GigaCode:     systemctl --user status gigacode-bridge ; ~/.config/gigacode-bridge/bridge.log
Песочница:         podman ps --filter label=hermes-agent=1
```

---

## Диагностика

| Симптом | Что проверить |
|---|---|
| `hermes: command not found` | `export PATH="$HOME/.local/bin:$PATH"`; в новой сессии — `source ~/.bashrc` |
| Hermes вызывает `docker`, а не Podman | `hermes config get HERMES_DOCKER_BINARY`; перезапусти `hermes` |
| `short-name resolution enforced` | в `terminal.docker_image` нужно полное имя `docker.io/...` |
| `Permission denied` на файлах внутри контейнера | SELinux: суффикс `,z` у томов (шаг 7.1), затем пересоздать контейнер (7.2) |
| Изменил тома/образ, а в контейнере старое | контейнер переиспользуется — шаг 7.2 |
| Нет сети/внутренних имён в контейнере | `podman inspect <C> --format '{{.HostConfig.NetworkMode}}'` должен быть `host`; `terminal.docker_network` = `true` |
| TLS-ошибки к внутренним сервисам | шаг 5.4 (CA-бандл), прокси через `docker_forward_env` |
| `Connection refused` из `gigacode-host` | `systemctl --user status gigacode-bridge`; `journalctl --user -u gigacode-bridge -n 50`; порт в `sshd_config` и в `gigacode-host` совпадает |
| `Permission denied (publickey)` | права: `$BRIDGE_DIR` 700, `authorized_keys` 600, `AllowUsers` = твой логин |
| `Host key verification failed` | пересоздай `known_hosts` (шаг 6.4) |
| GigaCode из моста «не найден» / падает на `node` | `GIGACODE_BIN` и `HOST_PATH` в `bridge.conf`; авторизация в `$BRIDGE_DIR/env` |
| GigaCode висит | headless-флаги неверны и он ждёт ввода — уточни `gigacode.args` по `--help` |

## Откат

```bash
systemctl --user disable --now gigacode-bridge.service
rm -f ~/.config/systemd/user/gigacode-bridge.service; systemctl --user daemon-reload
rm -rf ~/.config/gigacode-bridge ~/.local/share/gigacode-bridge ~/.hermes/skills/gigacode-host
podman ps -aq --filter label=hermes-agent=1 | xargs -r podman rm -f
hermes config set terminal.backend local      # или полное удаление: hermes uninstall
```

Данные пользователя в `~/hermes-work` откат не трогает.
