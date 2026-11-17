"""Host-owned Docker sandbox for bounded, pure JSON generated programs.

Generated Python is parsed on the host, and executed only inside a pinned local
container. Docker is a kernel-sharing containment layer, not a formal security
proof. No mounts, daemon socket, host keys, dependency install or network fallback.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..content.hash import content_hash
from .models import now

PINNED_PYTHON_IMAGE = "sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337"
ALLOWED_MODULES = frozenset(
    {"math", "re", "json", "collections", "itertools", "functools", "statistics"}
)
DENIED_CALLS = frozenset(
    {
        "eval",
        "exec",
        "compile",
        "open",
        "input",
        "getattr",
        "setattr",
        "delattr",
        "globals",
        "locals",
        "vars",
        "breakpoint",
        "help",
        "dir",
        "type",
        "object",
        "memoryview",
    }
)


class SandboxLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    cpu: float = Field(default=0.5, gt=0, le=1)
    memory_bytes: int = Field(default=128 * 1024 * 1024, ge=32 * 1024 * 1024, le=256 * 1024 * 1024)
    pids: int = Field(default=32, ge=8, le=64)
    timeout_seconds: float = Field(default=10.0, gt=0, le=30)
    max_output_bytes: int = Field(default=64 * 1024, ge=1024, le=128 * 1024)
    max_input_bytes: int = Field(default=128 * 1024, ge=1024, le=256 * 1024)


def inspect_program(source: str) -> dict[str, Any]:
    """Static surface reduction, explicitly not the execution/security boundary."""
    if not isinstance(source, str) or not 1 <= len(source.encode("utf-8")) <= 32_768:
        raise ValueError("生成工具源码必须在1至32768字节内")
    try:
        tree = ast.parse(source, mode="exec")
    except (SyntaxError, RecursionError) as exc:
        raise ValueError("生成工具不是可解析的Python源码") from exc
    nodes = list(ast.walk(tree))
    if len(nodes) > 5000:
        raise ValueError("生成工具AST过大")
    for statement in tree.body:
        if not isinstance(statement, (ast.FunctionDef, ast.Import, ast.ImportFrom)) and not (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str)
        ):
            raise ValueError("工具顶层只允许函数、受限stdlib导入与文档字符串")
    functions = [
        item for item in tree.body if isinstance(item, ast.FunctionDef) and item.name == "run"
    ]
    if len(functions) != 1:
        raise ValueError("工具必须提供唯一def run(payload)入口")
    entry = functions[0]
    if (
        len(entry.args.args) != 1
        or entry.args.posonlyargs
        or entry.args.kwonlyargs
        or entry.args.vararg
        or entry.args.kwarg
        or entry.args.defaults
    ):
        raise ValueError("run入口必须只有一个必需JSON对象参数")
    for item in nodes:
        if isinstance(item, (ast.AsyncFunctionDef, ast.ClassDef, ast.Global, ast.Nonlocal)):
            raise ValueError("当前纯JSON工具不允许异步、类或全局状态声明")
        if isinstance(item, ast.FunctionDef) and (item.decorator_list or item.args.defaults):
            raise ValueError("当前工具不允许装饰器或默认参数执行")
        if isinstance(item, ast.Name) and item.id.startswith("__"):
            raise ValueError("当前工具不允许dunder命名访问")
        if isinstance(item, ast.Attribute) and item.attr.startswith("_"):
            raise ValueError("当前工具不允许私有／dunder属性访问")
        if (
            isinstance(item, ast.Call)
            and isinstance(item.func, ast.Name)
            and item.func.id in DENIED_CALLS
        ):
            raise ValueError("当前工具包含不允许的动态执行／反射／I/O调用")
        if isinstance(item, ast.Import):
            if any(alias.name not in ALLOWED_MODULES for alias in item.names):
                raise ValueError("工具只允许指定纯计算stdlib模块，不安装依赖")
        if isinstance(item, ast.ImportFrom) and (
            item.level
            or item.module not in ALLOWED_MODULES
            or any(alias.name.startswith("_") for alias in item.names)
        ):
            raise ValueError("工具的from导入不在指定stdlib范围内")
    return {
        "source_hash": content_hash(source),
        "ast_nodes": len(nodes),
        "entrypoint": "run",
        "static_policy": "pure-json-python-v1",
    }


# Trusted fixed runner. This string is never composed from generated source.
# The only exec of generated code is here, inside the constrained container.
_RUNNER = r"""
import builtins, contextlib, json, os, socket, sys
packet = json.loads(sys.stdin.buffer.read(262145))
limit = packet["max_output_bytes"]
class Sink:
    def __init__(self): self.text = ""
    def write(self, value):
        if len((self.text + value).encode("utf-8")) > 8192:
            raise RuntimeError("program log limit exceeded")
        self.text += value
        return len(value)
    def flush(self): pass
sink = Sink()
try:
    if packet.get("probe") is True:
        status = dict(line.split(":", 1) for line in open("/proc/self/status") if ":" in line)
        try:
            open("/owc-write-probe", "w").write("probe")
            root_write_denied = False
        except OSError:
            root_write_denied = True
        connection = socket.socket()
        connection.settimeout(0.2)
        try:
            connection.connect(("1.1.1.1", 443))
            external_network_denied = False
        except OSError:
            external_network_denied = True
        finally:
            connection.close()
        value = {"uid": os.getuid(), "gid": os.getgid(),
                 "cap_eff": status["CapEff"].strip(),
                 "no_new_privs": status["NoNewPrivs"].strip(),
                 "seccomp": status["Seccomp"].strip(),
                 "root_write_denied": root_write_denied,
                 "external_network_denied": external_network_denied,
                 "docker_socket_present": os.path.exists("/var/run/docker.sock"),
                 "environment_names": sorted(os.environ)}
    else:
        allowed_modules = {
            "math", "re", "json", "collections", "itertools", "functools", "statistics"
        }
        def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
            if level or name not in allowed_modules:
                raise ImportError("module outside pure JSON contract")
            return builtins.__import__(name, globals, locals, fromlist, level)
        allowed = {name: getattr(builtins, name) for name in (
            "abs", "all", "any", "bool", "dict", "divmod", "enumerate", "filter", "float",
            "frozenset", "int", "isinstance", "issubclass", "len", "list", "map", "max", "min",
            "next", "pow", "print", "range", "reversed", "round", "set", "slice", "sorted",
            "str", "sum", "tuple", "zip", "Exception", "ValueError", "TypeError", "RuntimeError",
            "KeyError", "IndexError", "ZeroDivisionError"
        )}
        allowed["__import__"] = guarded_import
        scope = {"__builtins__": allowed, "__name__": "generated_tool"}
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            exec(compile(packet["source"], "<generated-tool>", "exec"), scope, scope)
            value = scope["run"](packet["input"])
    result = {"ok": True, "value": value, "feedback": sink.text}
    encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
    if len(encoded.encode("utf-8")) > limit:
        raise RuntimeError("program output limit exceeded")
except BaseException as exc:
    encoded = json.dumps({"ok": False, "error": type(exc).__name__ + ": " + str(exc)[:2000],
                          "feedback": sink.text[:2000]}, ensure_ascii=False)
sys.stdout.write(encoded)
"""


class DockerSandbox:
    def __init__(self, *, image_id: str = PINNED_PYTHON_IMAGE, limits: SandboxLimits | None = None):
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", image_id):
            raise ValueError("sandbox必须固定本地镜像sha256 ID，不能使用可变tag")
        self.image_id, self.limits = image_id, limits or SandboxLimits()

    @property
    def policy(self) -> dict[str, Any]:
        return {
            "image_id": self.image_id,
            "limits": self.limits.model_dump(),
            "runner_hash": content_hash(_RUNNER),
            "network": "none",
            "readonly_root": True,
            "user": "65534:65534",
            "cap_drop": ["ALL"],
            "no_new_privileges": True,
            "host_mounts": [],
            "host_environment_forwarded": False,
            "seccomp": "docker_default",
            "static_policy": "pure-json-python-v1",
        }

    @staticmethod
    def _cli(*arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["docker", *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=20,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )

    def preflight(self) -> dict[str, Any]:
        process = self._cli("image", "inspect", self.image_id)
        if process.returncode:
            raise RuntimeError(
                "固定本地sandbox镜像不可用／Docker权限不足：" + process.stderr[:2000]
            )
        image = json.loads(process.stdout)[0]
        if (
            image["Id"] != self.image_id
            or image.get("Os") != "linux"
            or image.get("Config", {}).get("Volumes")
        ):
            raise ValueError("sandbox镜像身份、Linux平台或隐式挂载不符合合同")
        return {
            "image_id": image["Id"],
            "repo_digests": image.get("RepoDigests", []),
            "image_environment_names": [
                item.split("=", 1)[0] for item in image["Config"].get("Env", [])
            ],
            "policy_hash": content_hash(self.policy),
        }

    def _inspect(self, name: str, nonce: str) -> dict[str, Any]:
        process = self._cli("inspect", name)
        if process.returncode:
            raise RuntimeError("无法核验本次sandbox容器：" + process.stderr[:1000])
        item = json.loads(process.stdout)[0]
        if item.get("Config", {}).get("Labels", {}).get("owc.generated-tool") != nonce:
            raise ValueError("容器nonce标签不一致，不操作其它容器")
        return item

    def _checked_config(self, item: dict[str, Any]) -> dict[str, Any]:
        config, host = item["Config"], item["HostConfig"]
        observed = {
            "image_id": item["Image"],
            "user": config["User"],
            "network": host["NetworkMode"],
            "readonly_root": host["ReadonlyRootfs"],
            "cap_drop": host.get("CapDrop"),
            "security_opt": host.get("SecurityOpt"),
            "memory": host["Memory"],
            "memory_swap": host["MemorySwap"],
            "nano_cpus": host["NanoCpus"],
            "pids_limit": host["PidsLimit"],
            "privileged": host["Privileged"],
            "mounts": item.get("Mounts", []),
            "environment_names": [value.split("=", 1)[0] for value in config.get("Env", [])],
        }
        if (
            observed["image_id"] != self.image_id
            or observed["user"] != "65534:65534"
            or observed["network"] != "none"
            or observed["readonly_root"] is not True
            or observed["privileged"]
            or observed["mounts"]
            or set(observed["cap_drop"] or []) != {"ALL"}
            or "no-new-privileges=true" not in (observed["security_opt"] or [])
            or any("unconfined" in option for option in observed["security_opt"] or [])
            or observed["memory"] != self.limits.memory_bytes
            or observed["memory_swap"] != self.limits.memory_bytes
            or observed["nano_cpus"] != int(self.limits.cpu * 1_000_000_000)
            or observed["pids_limit"] != self.limits.pids
        ):
            raise ValueError("Docker实际容器隔离／资源配置不符合冻结策略，未执行程序")
        if any(
            name in observed["environment_names"]
            for name in (
                "DEEPSEEK_API_KEY",
                "OPENAI_API_KEY",
                "ANTHROPIC_API_KEY",
                "OWCOPILOT_API_KEY",
            )
        ):
            raise ValueError("sandbox意外包含宿主密钥环境，未执行程序")
        return observed

    def _run(self, packet: dict[str, Any], cancelled: Callable[[], bool]) -> dict[str, Any]:
        preflight = self.preflight()
        nonce = uuid.uuid4().hex
        name = "owc-generated-" + nonce
        encoded_input = json.dumps(
            {**packet, "max_output_bytes": self.limits.max_output_bytes},
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        if len(encoded_input) > self.limits.max_input_bytes:
            raise ValueError("sandbox输入超出冻结字节上限")
        created = False
        process: subprocess.Popen[bytes] | None = None
        observed: dict[str, Any] = {}
        state: dict[str, Any] = {}
        status, error, value, feedback = "infrastructure_error", "", None, ""
        cleanup = "not_created"
        started = time.monotonic()
        try:
            result = self._cli(
                "create",
                "--name",
                name,
                "--label",
                "owc.generated-tool=" + nonce,
                "--pull=never",
                "--network=none",
                "--read-only",
                "--user=65534:65534",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges=true",
                "--ipc=none",
                "--cpus=" + str(self.limits.cpu),
                "--memory=" + str(self.limits.memory_bytes),
                "--memory-swap=" + str(self.limits.memory_bytes),
                "--pids-limit=" + str(self.limits.pids),
                "--ulimit=nofile=64:64",
                "--ulimit=fsize=65536:65536",
                "--ulimit=core=0:0",
                "--restart=no",
                "--log-driver=none",
                "--entrypoint=/usr/local/bin/python3",
                "-i",
                self.image_id,
                "-I",
                "-S",
                "-B",
                "-c",
                _RUNNER,
            )
            if result.returncode:
                raise RuntimeError("sandbox创建失败：" + result.stderr[:2000])
            created = True
            observed = self._checked_config(self._inspect(name, nonce))
            if cancelled():
                status, error = "cancelled", "cancelled before container start"
            else:
                process = subprocess.Popen(
                    ["docker", "start", "--attach", "--interactive", name],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                buffers = {"stdout": bytearray(), "stderr": bytearray()}
                lock, overflow = threading.Lock(), threading.Event()

                def read(stream: Any, target: str) -> None:
                    try:
                        while chunk := stream.read(4096):
                            with lock:
                                total = sum(len(buffer) for buffer in buffers.values())
                                available = max(0, self.limits.max_output_bytes - total)
                                buffers[target].extend(chunk[:available])
                                if len(chunk) > available:
                                    overflow.set()
                    finally:
                        stream.close()

                readers = [
                    threading.Thread(target=read, args=(stream, target), daemon=True)
                    for stream, target in ((process.stdout, "stdout"), (process.stderr, "stderr"))
                ]
                for reader in readers:
                    reader.start()
                assert process.stdin is not None
                input_stream = process.stdin

                def write_input() -> None:
                    try:
                        input_stream.write(encoded_input)
                    except OSError:
                        pass
                    finally:
                        input_stream.close()

                writer = threading.Thread(target=write_input, daemon=True)
                writer.start()
                execution_started = time.monotonic()
                interruption = None
                while process.poll() is None:
                    if cancelled():
                        interruption = "cancelled"
                    elif overflow.is_set():
                        interruption = "output_limit"
                    elif time.monotonic() - execution_started > self.limits.timeout_seconds:
                        interruption = "timeout"
                    if interruption:
                        self._cli("kill", name)
                        break
                    time.sleep(0.05)
                process.wait(timeout=5)
                for reader in readers:
                    reader.join(timeout=1)
                if interruption or overflow.is_set():
                    status, error = interruption or "output_limit", "sandbox execution interrupted"
                else:
                    try:
                        result_payload = json.loads(buffers["stdout"].decode("utf-8"))
                        if result_payload.get("ok") is True:
                            status, value = "completed", result_payload["value"]
                        else:
                            status, error = (
                                "program_error",
                                str(result_payload.get("error", "missing program result")),
                            )
                        feedback = str(result_payload.get("feedback", ""))
                    except (ValueError, KeyError, TypeError):
                        error = (
                            "sandbox返回非协议输出："
                            + buffers["stderr"].decode("utf-8", "replace")[:2000]
                        )
            state = self._inspect(name, nonce).get("State", {})
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            error = f"{type(exc).__name__}: {exc}"[:2500]
        finally:
            if created:
                try:
                    item = self._inspect(name, nonce)
                    state = item.get("State", state)
                    removal = self._cli("rm", "--force", name)
                    cleanup = "removed" if removal.returncode == 0 else "failed"
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                    cleanup = "unknown"
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=5)
        receipt = {
            "id": nonce,
            "status": status,
            "value": value,
            "error": error,
            "feedback": feedback,
            "image_id": self.image_id,
            "sandbox_policy": self.policy,
            "sandbox_policy_hash": content_hash(self.policy),
            "observed_config": observed,
            "container_state": state,
            "container_name": name,
            "preflight": preflight,
            "cleanup": cleanup,
            "duration_ms": (time.monotonic() - started) * 1000,
            "created_at": now(),
        }
        return {**receipt, "execution_hash": content_hash(receipt)}

    def run(
        self, source: str, arguments: dict[str, Any], *, cancelled: Callable[[], bool] | None = None
    ) -> dict[str, Any]:
        inspect_program(source)
        return self._run({"source": source, "input": arguments}, cancelled or (lambda: False))

    def probe(self) -> dict[str, Any]:
        return self._run({"probe": True}, lambda: False)
