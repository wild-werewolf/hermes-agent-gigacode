"""Stand-in for the ``gigacode`` CLI used by the offline contract tests (stdlib only, run with ``-I``).

Accepts the same argv and stdin as the real launch, then follows a JSON scenario named by the
``FAKE_GIGACODE_SCENARIO`` environment variable (the test driver injects it). Scenario keys:

* ``record``: path; the fake writes {argv, stdin, env_keys, cwd, mcp_config_keys, mcp} there;
* ``mcp_calls``: [{"tool": wire_name, "arguments": {...}}] — real MCP over HTTP to the bridge
  (initialize → notifications/initialized → tools/list → tools/call ...);
* ``steps``: ordered actions — {"event": obj} one NDJSON line ({{call:N}} in strings is replaced
  by the text of MCP call N), {"raw": text} bytes as-is, {"sleep": s}, {"stderr": text},
  {"stderr_flood": n}, {"stdout_flood": n}, {"spawn_setsid_child": pidfile};
* ``chunk``: write stdout in chunks of this many bytes (exercises fragmented reads);
* ``exit_code``: process exit status (default 0).

It is a protocol simulator, not evidence about any real GigaCode build.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

REQUIRED = ["--output-format", "stream-json", "--allowed-mcp-server-names", "hermes",
            "--allowed-tools", "mcp__hermes", "--approval-mode=auto-edit"]


def _parse_argv(argv: list[str]) -> dict:
    known_with_value = {"--output-format", "--mcp-config", "--allowed-mcp-server-names", "--allowed-tools", "--model"}
    seen: dict[str, list[str]] = {}
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--approval-mode=auto-edit":
            seen.setdefault(arg, []).append("")
            i += 1
        elif arg in known_with_value and i + 1 < len(argv):
            seen.setdefault(arg, []).append(argv[i + 1])
            i += 2
        else:
            sys.stderr.write(f"Unknown argument: {arg}\n")
            sys.exit(2)
    if any(len(v) > 1 for v in seen.values()):
        sys.stderr.write("duplicate flag\n")
        sys.exit(2)
    if seen.get("--output-format") != ["stream-json"] or "--mcp-config" not in seen:
        sys.stderr.write("missing required flags\n")
        sys.exit(2)
    return {k: v[0] for k, v in seen.items()}


class _Mcp:
    def __init__(self, config_path: str) -> None:
        with open(config_path, encoding="utf-8") as fh:
            server = json.load(fh)["mcpServers"]["hermes"]
        self.url, self.headers, self.session, self.next_id = server["httpUrl"], dict(server["headers"]), None, 1

    def rpc(self, method: str, params: dict | None = None, notify: bool = False) -> dict:
        body: dict = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notify:
            body["id"] = self.next_id
            self.next_id += 1
        headers = {**self.headers, "Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        request = urllib.request.Request(self.url, data=json.dumps(body).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=30) as resp:
                self.session = resp.headers.get("Mcp-Session-Id") or self.session
                raw = resp.read()
                return json.loads(raw) if raw else {"status": resp.status}
        except urllib.error.HTTPError as exc:
            return {"http_error": exc.code}


def _run_mcp(config_path: str, calls: list[dict]) -> dict:
    mcp = _Mcp(config_path)
    init = mcp.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                  "clientInfo": {"name": "fake-gigacode", "version": "0"}})
    mcp.rpc("notifications/initialized", notify=True)
    listed = mcp.rpc("tools/list")
    results = []
    for call in calls:
        reply = mcp.rpc("tools/call", {"name": call["tool"], "arguments": call.get("arguments", {})})
        result = reply.get("result") or {}
        text = "".join(c.get("text", "") for c in result.get("content", []))
        results.append({"text": text, "isError": result.get("isError"), "raw": reply})
    return {"init": init, "tools": [t["name"] for t in (listed.get("result") or {}).get("tools", [])],
            "results": results}


def _substitute(value, results: list[dict]):
    if isinstance(value, str):
        for n, res in enumerate(results):
            value = value.replace("{{call:%d}}" % n, res["text"])
        return value
    if isinstance(value, list):
        return [_substitute(v, results) for v in value]
    if isinstance(value, dict):
        return {k: _substitute(v, results) for k, v in value.items()}
    return value


def _write(data: bytes, chunk: int) -> None:
    out = sys.stdout.buffer
    if chunk <= 0:
        out.write(data)
        out.flush()
        return
    for start in range(0, len(data), chunk):
        out.write(data[start:start + chunk])
        out.flush()
        time.sleep(0.001)


def _spawn_setsid_child(pidfile: str) -> None:
    code = ("import os,sys,time\n"
            "if os.fork(): sys.exit(0)\n"
            "os.setsid()\n"
            f"open({pidfile!r},'w').write(str(os.getpid()))\n"
            "time.sleep(600)\n")
    subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL).wait()
    deadline = time.time() + 5
    while time.time() < deadline and not os.path.exists(pidfile):
        time.sleep(0.02)


def main() -> int:
    flags = _parse_argv(sys.argv[1:])
    stdin = sys.stdin.buffer.read()
    with open(os.environ["FAKE_GIGACODE_SCENARIO"], encoding="utf-8") as fh:
        scenario = json.load(fh)
    mcp = _run_mcp(flags["--mcp-config"], scenario["mcp_calls"]) if scenario.get("mcp_calls") else None
    if scenario.get("record"):
        with open(flags["--mcp-config"], encoding="utf-8") as fh:
            mcp_keys = sorted(json.load(fh)["mcpServers"]["hermes"])
        with open(scenario["record"], "w", encoding="utf-8") as fh:
            json.dump({"argv": sys.argv[1:], "stdin": stdin.decode("utf-8"), "env_keys": sorted(os.environ),
                       "env": {k: v for k, v in os.environ.items() if k in ("HOME", "PATH")}, "cwd": os.getcwd(),
                       "mcp_config_keys": mcp_keys, "mcp": mcp}, fh)
    results = (mcp or {}).get("results", [])
    chunk = int(scenario.get("chunk", 0))
    for step in scenario.get("steps", []):
        if "event" in step:
            _write((json.dumps(_substitute(step["event"], results), ensure_ascii=False) + "\n").encode(), chunk)
        elif "raw" in step:
            _write(step["raw"].encode("utf-8"), chunk)
        elif "sleep" in step:
            time.sleep(step["sleep"])
        elif "stderr" in step:
            sys.stderr.write(step["stderr"])
            sys.stderr.flush()
        elif "stderr_flood" in step:
            block = b"e" * 65536
            for _ in range(step["stderr_flood"] // len(block)):
                sys.stderr.buffer.write(block)
            sys.stderr.flush()
        elif "stdout_flood" in step:
            line = (json.dumps({"type": "system", "subtype": "progress", "pad": "x" * 60000}) + "\n").encode()
            for _ in range(step["stdout_flood"] // len(line) + 1):
                sys.stdout.buffer.write(line)
            sys.stdout.flush()
        elif "spawn_setsid_child" in step:
            _spawn_setsid_child(step["spawn_setsid_child"])
    return int(scenario.get("exit_code", 0))


if __name__ == "__main__":
    sys.exit(main())
