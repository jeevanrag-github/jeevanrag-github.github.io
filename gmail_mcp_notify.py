#!/usr/bin/env python3
"""Send LinkedIn 2FA tap alerts via the configured Google/Gmail MCP server."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

MCP_CONFIG_CANDIDATES = (
    Path("/workspace/.cursor/mcp.json"),
    Path.home() / ".cursor" / "mcp.json",
)
TFA_ALERT_FILE = Path("/workspace/tfa_alert.json")
_SENT: set[str] = set()


def _load_mcp_servers() -> dict[str, dict]:
    servers: dict[str, dict] = {}
    raw = os.environ.get("GMAIL_MCP_SERVERS_JSON", "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                servers.update(parsed)
        except json.JSONDecodeError:
            pass

    for path in MCP_CONFIG_CANDIDATES:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text())
            for name, cfg in (data.get("mcpServers") or {}).items():
                servers.setdefault(name, cfg)
        except Exception:
            continue

    if not servers:
        env_cmd = os.environ.get("GMAIL_MCP_COMMAND", "npx")
        env_args = os.environ.get("GMAIL_MCP_ARGS", "-y gmail-mcp").split()
        env_block = {}
        for key in ("GOOGLE_ACCESS_TOKEN", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"):
            if os.environ.get(key):
                env_block[key] = os.environ[key]
        servers["gmail"] = {"command": env_cmd, "args": env_args, "env": env_block}

    mcp_url = os.environ.get("GMAIL_MCP_URL", "").strip()
    if mcp_url:
        headers = {}
        auth = os.environ.get("GMAIL_MCP_AUTH_HEADER", "").strip()
        if auth:
            headers["Authorization"] = auth
        servers.setdefault(
            "gmail-http",
            {"url": mcp_url, "headers": headers},
        )
    return servers


def _google_like(name: str, cfg: dict) -> bool:
    blob = " ".join(
        [
            name,
            cfg.get("command", ""),
            " ".join(cfg.get("args") or []),
            cfg.get("url", ""),
        ]
    ).lower()
    return any(k in blob for k in ("gmail", "google", "workspace"))


def _build_email(tfa_info: dict) -> tuple[str, str, str, str]:
    num = tfa_info.get("tap_number") or "???"
    devices = tfa_info.get("devices") or []
    device_str = ", ".join(devices) if devices else "your registered phone"
    prompt_type = tfa_info.get("prompt_type", "number")

    if prompt_type == "yes" or num == "YES":
        subject = "LinkedIn login: TAP YES on your phone"
        body = (
            f"LinkedIn automation needs Google 2FA approval.\n\n"
            f"Action: TAP YES on {device_str}\n"
            f"You have about 5 minutes.\n\n"
            f"This is an automated alert from your LinkedIn engagement assistant."
        )
        line = f"TAP YES — approve on {device_str} NOW. You have 5 minutes."
    else:
        subject = f"LinkedIn login: TAP NUMBER {num}"
        body = (
            f"LinkedIn automation needs Google 2FA approval.\n\n"
            f"TAP NUMBER: {num}\n"
            f"Device: {device_str}\n"
            f"You have about 5 minutes.\n\n"
            f"This is an automated alert from your LinkedIn engagement assistant."
        )
        line = f"TAP NUMBER: {num} — approve on {device_str} NOW. You have 5 minutes."

    recipient = os.environ.get("TFA_NOTIFY_EMAIL") or os.environ.get("LINKEDIN_EMAIL", "")
    return recipient, subject, body, line


def _write_alert_file(tfa_info: dict, recipient: str, subject: str, body: str) -> None:
    payload = {
        "tap_number": tfa_info.get("tap_number"),
        "devices": tfa_info.get("devices") or [],
        "prompt_type": tfa_info.get("prompt_type", "number"),
        "recipient": recipient,
        "subject": subject,
        "body": body,
        "ts": int(time.time()),
    }
    TFA_ALERT_FILE.write_text(json.dumps(payload, indent=2))


class _McpStdioClient:
    def __init__(self, command: str, args: list[str], env: dict[str, str] | None = None):
        merged = os.environ.copy()
        if env:
            merged.update({k: str(v) for k, v in env.items()})
        self._proc = subprocess.Popen(
            [command, *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=merged,
        )
        self._lock = threading.Lock()
        self._next_id = 0
        self._reader = threading.Thread(target=self._read_stderr, daemon=True)
        self._reader.start()

    def _read_stderr(self) -> None:
        try:
            assert self._proc.stderr is not None
            for line in self._proc.stderr:
                if line.strip():
                    print(f"MCP stderr: {line.rstrip()}", file=sys.stderr, flush=True)
        except Exception:
            pass

    def close(self) -> None:
        try:
            if self._proc.poll() is None:
                self._proc.terminate()
                self._proc.wait(timeout=3)
        except Exception:
            try:
                self._proc.kill()
            except Exception:
                pass

    def _send(self, payload: dict) -> None:
        assert self._proc.stdin is not None
        self._proc.stdin.write(json.dumps(payload) + "\n")
        self._proc.stdin.flush()

    def _recv(self, expect_id: int | None, timeout: float = 20.0) -> dict | None:
        assert self._proc.stdout is not None
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._proc.poll() is not None:
                return None
            line = self._proc.stdout.readline()
            if not line.strip():
                time.sleep(0.05)
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if expect_id is not None and msg.get("id") != expect_id:
                continue
            return msg
        return None

    def request(self, method: str, params: dict | None = None, timeout: float = 20.0) -> dict | None:
        with self._lock:
            self._next_id += 1
            req_id = self._next_id
            self._send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}})
            return self._recv(req_id, timeout=timeout)

    def notify(self, method: str, params: dict | None = None) -> None:
        with self._lock:
            self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def initialize(self) -> bool:
        init = self.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "linkedin-tfa-notifier", "version": "1.0.0"},
            },
            timeout=25.0,
        )
        if not init or "result" not in init:
            return False
        self.notify("notifications/initialized")
        return True

    def list_tools(self) -> list[dict]:
        resp = self.request("tools/list", {}, timeout=15.0)
        if not resp or "result" not in resp:
            return []
        tools = resp["result"].get("tools") or []
        return tools if isinstance(tools, list) else []

    def call_tool(self, name: str, arguments: dict) -> dict | None:
        return self.request("tools/call", {"name": name, "arguments": arguments}, timeout=30.0)


def _tool_send_candidates(tools: list[dict]) -> list[tuple[str, dict]]:
    names = {t.get("name", ""): t for t in tools if isinstance(t, dict)}
    preferred = (
        "send_email",
        "gmail_send",
        "gmail_message_send",
        "gmail_send_message",
    )
    ordered = [n for n in preferred if n in names] + [n for n in names if n not in preferred]
    out: list[tuple[str, dict]] = []
    for name in ordered:
        if re.search(r"(send|mail|email)", name, re.I):
            out.append((name, names[name]))
    return out


def _arguments_for_tool(tool_name: str, recipient: str, subject: str, body: str) -> list[dict]:
    base = [
        {"to": recipient, "subject": subject, "body": body},
        {"to": [recipient], "subject": subject, "body": body},
        {"recipient": recipient, "subject": subject, "body": body},
        {"to": recipient, "subject": subject, "text": body},
        {"to": recipient, "subject": subject, "message": body},
    ]
    if tool_name == "gmail_message_send":
        base.insert(0, {"to": recipient, "subject": subject, "body": body, "raw": False})
    return base


def _send_via_stdio_server(cfg: dict, recipient: str, subject: str, body: str) -> bool:
    command = cfg.get("command")
    if not command:
        return False
    args = cfg.get("args") or []
    env = cfg.get("env") or {}

    client = _McpStdioClient(command, args, env=env)
    try:
        if not client.initialize():
            return False
        tools = client.list_tools()
        candidates = _tool_send_candidates(tools)
        if not candidates:
            print("GMAIL_MCP|no_send_tool_found", flush=True)
            return False

        for tool_name, _meta in candidates:
            for arguments in _arguments_for_tool(tool_name, recipient, subject, body):
                resp = client.call_tool(tool_name, arguments)
                if resp and "result" in resp and not resp.get("error"):
                    print(f"GMAIL_MCP|sent_via={tool_name}|to={recipient}", flush=True)
                    return True
        return False
    finally:
        client.close()


def _send_via_http_server(cfg: dict, recipient: str, subject: str, body: str) -> bool:
    url = cfg.get("url")
    if not url:
        return False
    try:
        import urllib.request

        payload = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "send_email",
                    "arguments": {"to": recipient, "subject": subject, "body": body},
                },
            }
        ).encode()
        headers = {"Content-Type": "application/json"}
        for key, value in (cfg.get("headers") or {}).items():
            headers[str(key)] = str(value)
        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode())
        if data.get("result") and not data.get("error"):
            print(f"GMAIL_MCP|sent_via=http|to={recipient}", flush=True)
            return True
    except Exception as exc:
        print(f"GMAIL_MCP|http_failed|error={exc}", flush=True)
    return False


def send_tfa_email(tfa_info: dict) -> bool:
    """Send a one-time 2FA alert email via configured Google/Gmail MCP."""
    recipient, subject, body, _line = _build_email(tfa_info)
    if not recipient:
        print("GMAIL_MCP|skipped|reason=no_recipient", flush=True)
        return False

    dedupe_key = f"{tfa_info.get('tap_number')}:{tfa_info.get('prompt_type')}"
    if dedupe_key in _SENT:
        return True
    _SENT.add(dedupe_key)

    _write_alert_file(tfa_info, recipient, subject, body)

    servers = _load_mcp_servers()
    for name, cfg in servers.items():
        if not _google_like(name, cfg):
            continue
        if cfg.get("url"):
            if _send_via_http_server(cfg, recipient, subject, body):
                return True
        elif cfg.get("command"):
            if _send_via_stdio_server(cfg, recipient, subject, body):
                return True

    print(
        "GMAIL_MCP|email_not_sent|reason=no_working_server "
        "(configure .cursor/mcp.json or GMAIL_MCP_* secrets)",
        flush=True,
    )
    return False


if __name__ == "__main__":
    info = {
        "tap_number": sys.argv[1] if len(sys.argv) > 1 else "TEST",
        "devices": ["your phone"],
        "prompt_type": "number" if len(sys.argv) > 1 else "number",
    }
    ok = send_tfa_email(info)
    sys.exit(0 if ok else 1)
