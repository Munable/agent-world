"""Open the user's existing Codex CLI with a browser-issued world connection.

The downloaded JSON stays unchanged. Only the launched process receives its
bearer token. A fresh MCP name avoids inheriting a user's same-name static
headers or environment settings; personal Codex settings and login still load.
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
from urllib.parse import urlsplit
import uuid

MAX_CONFIG_BYTES = 65536
DEFAULT_MODEL = "gpt-6-luna"
MAC_CODEX = Path("/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex")


class LaunchError(ValueError):
    """Fixed, credential-free diagnostic for the person starting the client."""


class Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse normally echoes an invalid argument, which may be a secret.
        self.exit(2, "启动参数不正确；请使用 --help 查看用法。\n")


def read_connection(config_path: Path):
    path = config_path.expanduser().absolute()
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                     | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_uid != os.getuid()):
                raise LaunchError("连接文件必须是你拥有的普通文件，不能是链接。")
            if info.st_mode & 0o077:
                raise LaunchError("连接文件需要仅自己可读，请将文件权限设为 600 后重试。")
            raw = stream.read(MAX_CONFIG_BYTES + 1)
    except FileNotFoundError:
        raise LaunchError("找不到连接文件；请使用网页下载的连接 JSON 路径。") from None
    except PermissionError:
        raise LaunchError("无法读取连接文件；请检查文件归属和读取权限。") from None
    except OSError:
        raise LaunchError("连接文件无法安全打开；请使用真实的普通文件路径。") from None
    if not raw or len(raw) > MAX_CONFIG_BYTES:
        raise LaunchError("连接文件为空或超过 64 KiB；请使用网页下载的原 JSON。")
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise LaunchError("连接文件不是有效的 UTF-8 JSON；请重新选择网页下载的文件。") from None
    try:
        if (not isinstance(value, dict) or set(value) != {"$schema", "mcp"}
                or value["$schema"] != "https://opencode.ai/config.json"
                or not isinstance(value["mcp"], dict) or set(value["mcp"]) != {"agent_world"}):
            raise ValueError
        remote = value["mcp"]["agent_world"]
        if (not isinstance(remote, dict) or set(remote) != {"type", "url", "oauth", "headers"}
                or remote["type"] != "remote" or remote["oauth"] is not False
                or not isinstance(remote["headers"], dict)
                or set(remote["headers"]) != {"Authorization"}):
            raise ValueError
        url, authorization = remote["url"], remote["headers"]["Authorization"]
        if (not isinstance(url, str) or len(url) > 2048 or re.search(r"[\s\x00-\x1f\x7f]", url)
                or not isinstance(authorization, str) or len(authorization) > 8192
                or not re.fullmatch(r"Bearer [^\s\x00-\x1f\x7f]+", authorization)):
            raise ValueError
        parts = urlsplit(url)
        if not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError
        # Reading port also rejects malformed port specifications.
        parts.port
        loopback = parts.hostname.lower() == "localhost"
        if not loopback:
            try:
                loopback = ipaddress.ip_address(parts.hostname).is_loopback
            except ValueError:
                loopback = False
        if parts.scheme != "https" and not (parts.scheme == "http" and loopback):
            raise ValueError
    except (ValueError, TypeError, KeyError):
        raise LaunchError("连接格式不匹配；需要网页下载的 remote Agent World JSON，远程地址使用 HTTPS。") from None
    return url, authorization.removeprefix("Bearer "), hashlib.sha256(raw).hexdigest()


def installed_codex(requested: Path | None):
    if requested is not None:
        candidate = requested.expanduser().absolute()
    else:
        found = shutil.which("codex")
        candidate = Path(found) if found else MAC_CODEX
    if not candidate.is_file() or not os.access(candidate, os.X_OK):
        raise LaunchError("找不到已安装且可执行的 Codex CLI；可以用 --codex 指定已有程序路径。")
    return candidate


def build_launch(config_path: Path, *, codex: Path | None = None, model=DEFAULT_MODEL,
                 prompt: str | None = None, json_events=False, reasoning_effort=None):
    url, token, config_sha = read_connection(config_path)
    binary = installed_codex(codex)
    if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./:-]{0,127}", model):
        raise LaunchError("模型名称格式不正确；请指定已有 Codex 登录能够使用的模型。")
    if prompt is not None:
        if not prompt.strip() or len(prompt.encode("utf-8")) > 40000:
            raise LaunchError("--prompt 需要非空文字，长度不超过 40 KiB。")
        if token in prompt or re.search(r"(?i)Bearer\s+\S+|awid_[A-Za-z0-9_.-]+", prompt):
            raise LaunchError("目标文字不能包含令牌；连接由私密 JSON 自动读取。")
    if json_events and prompt is None:
        raise LaunchError("--json 仅用于带 --prompt 的一次性执行。")
    if reasoning_effort is not None and reasoning_effort not in {"low", "medium", "high", "xhigh", "max", "ultra"}:
        raise LaunchError("推理档位不正确；请指定已有 Codex 模型支持的档位。")
    suffix = uuid.uuid4().hex[:12]
    server = "agent_world_play_" + suffix
    token_env = "AGENT_WORLD_PLAY_TOKEN_" + suffix.upper()
    env = os.environ.copy()
    env[token_env] = token
    overrides = {
        f"mcp_servers.{server}.url": url,
        f"mcp_servers.{server}.bearer_token_env_var": token_env,
        f"mcp_servers.{server}.enabled": True,
        f"mcp_servers.{server}.required": True,
    }
    command = [str(binary)]
    if prompt is not None:
        command.extend(["exec", "--skip-git-repo-check"])
        if json_events:
            command.append("--json")
    command.extend(["--model", model])
    if reasoning_effort is not None:
        command.extend(["--config", "model_reasoning_effort=" + json.dumps(reasoning_effort)])
    for name, value in overrides.items():
        command.extend(["--config", name + "=" + json.dumps(value)])
    if prompt is not None:
        command.append(prompt)
    if any(token in argument for argument in command):
        raise LaunchError("连接令牌不能出现在地址、模型、程序路径或目标文字中。")
    public = {"codex": str(binary), "model": model, "mcp_url": url, "mcp_server": server,
              "config_sha256": config_sha, "mode": "exec" if prompt is not None else "interactive"}
    return command, env, public


def main(argv=None):
    parser = Parser(description="用网页下载的连接文件打开已有 Codex；保留个人设置、登录和本地对话。")
    parser.add_argument("--config", type=Path, required=True, help="网页下载的私密 ravenbrook-opencode.json 路径")
    parser.add_argument("--codex", type=Path, help="可选：已安装 Codex CLI 的路径；不会安装或重新登录")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="现有登录可用的模型；默认 gpt-6-luna，无隐藏 fallback")
    parser.add_argument("--reasoning-effort", help="可选推理档位；未指定时沿用个人设置，例如 medium")
    parser.add_argument("--prompt", help="可选：一次性执行此目标；原文字不增加引导或改写，默认打开交互界面")
    parser.add_argument("--json", action="store_true", help="--prompt 模式输出 Codex 原生 JSON 事件")
    parser.add_argument("--check", action="store_true", help="只检查文件权限、连接格式及程序，不联网、不调用模型")
    args = parser.parse_args(argv)
    try:
        command, env, public = build_launch(args.config, codex=args.codex, model=args.model,
                                           prompt=args.prompt, json_events=args.json,
                                           reasoning_effort=args.reasoning_effort)
        if args.check:
            print("检查通过：连接文件与已安装 Codex 可用；未联网或调用模型。")
            print("连接文件 SHA256：" + public["config_sha256"])
            return 0
        print("本次世界连接：" + public["mcp_server"] + "；请使用网页上的第一句话开始。", file=sys.stderr)
        # Replacement keeps native Codex's signal handling and ordinary local
        # conversation storage. No loop, credential file or global config write.
        os.execvpe(command[0], command, env)
    except LaunchError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except OSError:
        print("无法启动已有 Codex CLI；请检查程序路径与执行权限。", file=sys.stderr)
        return 2
    except Exception:
        print("无法安全准备连接；没有启动模型，也没有修改连接文件。", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
