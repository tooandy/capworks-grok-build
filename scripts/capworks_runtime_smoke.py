#!/usr/bin/env python3
"""Smoke-test a CapWorks-pinned Grok runtime binary over real ACP stdio.

The release smoke intentionally runs without provider credentials. It validates
the binary identity and ACP initialization/capabilities, then proves session/new
reaches either a real local session or Grok's explicit authentication gate. A
credentialed session/new belongs to the CapWorks live integration gate, not the
artifact build/release trust boundary.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", required=True, type=Path)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--timeout", type=float, default=30.0)
    return parser.parse_args()


def read_stderr(stream: Any, lines: list[str]) -> None:
    for line in iter(stream.readline, ""):
        lines.append(line.rstrip())
        if len(lines) > 80:
            del lines[:-80]


def read_stdout(stream: Any, messages: queue.Queue[dict[str, Any]], raw: list[str]) -> None:
    for line in iter(stream.readline, ""):
        line = line.strip()
        if not line:
            continue
        raw.append(line)
        if len(raw) > 80:
            del raw[:-80]
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            messages.put({"__transport_error__": f"non-JSON stdout: {line!r}: {error}"})
            continue
        if not isinstance(value, dict):
            messages.put({"__transport_error__": f"non-object JSON-RPC stdout: {value!r}"})
            continue
        messages.put(value)


def send(proc: subprocess.Popen[str], payload: dict[str, Any]) -> None:
    assert proc.stdin is not None
    proc.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
    proc.stdin.flush()


def wait_for_response(
    proc: subprocess.Popen[str],
    messages: queue.Queue[dict[str, Any]],
    request_id: int,
    timeout: float,
    stderr_lines: list[str],
    stdout_lines: list[str],
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"runtime exited with {proc.returncode}; stderr={stderr_lines[-20:]}; "
                f"stdout={stdout_lines[-20:]}"
            )
        try:
            message = messages.get(timeout=min(0.25, max(0.01, deadline - time.monotonic())))
        except queue.Empty:
            continue
        if "__transport_error__" in message:
            raise RuntimeError(str(message["__transport_error__"]))
        if message.get("id") == request_id and ("result" in message or "error" in message):
            if "error" in message:
                raise RuntimeError(f"ACP request {request_id} failed: {message['error']}")
            result = message.get("result")
            if not isinstance(result, dict):
                raise RuntimeError(f"ACP request {request_id} returned non-object result: {result!r}")
            return result
        # The agent may issue reverse requests while setting up a session. Fail
        # them closed rather than letting an unrelated optional request block
        # the release smoke test.
        if "method" in message and "id" in message:
            send(
                proc,
                {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "error": {"code": -32601, "message": "unsupported in release smoke"},
                },
            )
    raise TimeoutError(
        f"timed out waiting for ACP response {request_id}; "
        f"stderr={stderr_lines[-20:]}; stdout={stdout_lines[-20:]}"
    )


def wait_for_session_new_or_auth_gate(
    proc: subprocess.Popen[str],
    messages: queue.Queue[dict[str, Any]],
    request_id: int,
    timeout: float,
    stderr_lines: list[str],
    stdout_lines: list[str],
) -> tuple[str, str | None]:
    """Return (outcome, session_id) for a no-credential session/new probe.

    The pinned runtime currently checks authentication during session/new. In
    release CI there are deliberately no provider secrets, so an explicit
    Authentication required error is evidence that the ACP request reached the
    expected auth boundary. Any other error still fails closed.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"runtime exited with {proc.returncode}; stderr={stderr_lines[-20:]}; "
                f"stdout={stdout_lines[-20:]}"
            )
        try:
            message = messages.get(timeout=min(0.25, max(0.01, deadline - time.monotonic())))
        except queue.Empty:
            continue
        if "__transport_error__" in message:
            raise RuntimeError(str(message["__transport_error__"]))
        if message.get("id") == request_id and ("result" in message or "error" in message):
            if "result" in message:
                result = message.get("result")
                if not isinstance(result, dict):
                    raise RuntimeError(
                        f"ACP request {request_id} returned non-object result: {result!r}"
                    )
                session_id = result.get("sessionId")
                if not isinstance(session_id, str) or not session_id:
                    raise RuntimeError(
                        f"session/new returned invalid sessionId: {session_id!r}"
                    )
                return "session_created", session_id

            error = message.get("error")
            if not isinstance(error, dict):
                raise RuntimeError(f"ACP request {request_id} returned malformed error: {error!r}")
            if (
                error.get("code") == -32000
                and error.get("message") == "Authentication required"
                and error.get("data") == "no auth method id provided"
            ):
                return "authentication_required", None
            raise RuntimeError(f"ACP request {request_id} failed: {error}")

        if "method" in message and "id" in message:
            send(
                proc,
                {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "error": {"code": -32601, "message": "unsupported in release smoke"},
                },
            )
    raise TimeoutError(
        f"timed out waiting for ACP response {request_id}; "
        f"stderr={stderr_lines[-20:]}; stdout={stdout_lines[-20:]}"
    )


def main() -> int:
    args = parse_args()
    binary = args.binary.resolve()
    if not binary.is_file():
        raise FileNotFoundError(binary)

    version = subprocess.run(
        [str(binary), "--version"],
        check=True,
        capture_output=True,
        text=True,
        timeout=args.timeout,
    )
    version_text = (version.stdout + version.stderr).strip()
    expected_short = args.source_revision[:12]
    if args.expected_version not in version_text or expected_short not in version_text:
        raise RuntimeError(
            "runtime version stamp mismatch: "
            f"expected version={args.expected_version!r} commit={expected_short!r}; got {version_text!r}"
        )

    help_result = subprocess.run(
        [str(binary), "agent", "--help"],
        check=True,
        capture_output=True,
        text=True,
        timeout=args.timeout,
    )
    if "stdio" not in (help_result.stdout + help_result.stderr).lower():
        raise RuntimeError("runtime agent help does not advertise stdio mode")

    with tempfile.TemporaryDirectory(prefix="capworks-grok-runtime-smoke-") as temp:
        root = Path(temp)
        workspace = root / "workspace"
        grok_home = root / "grok-home"
        workspace.mkdir()
        grok_home.mkdir()
        env = os.environ.copy()
        env["GROK_HOME"] = str(grok_home)
        env.setdefault("RUST_LOG", "warn")
        # No real provider credential is present. The release smoke validates
        # that session/new reaches the explicit auth gate; it never authenticates
        # or issues session/prompt.
        env.pop("XAI_API_KEY", None)
        env.pop("GROK_CODE_XAI_API_KEY", None)

        proc = subprocess.Popen(
            [str(binary), "--trust", "--no-auto-update", "agent", "--no-leader", "stdio"],
            cwd=workspace,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        assert proc.stdout is not None and proc.stderr is not None
        messages: queue.Queue[dict[str, Any]] = queue.Queue()
        stderr_lines: list[str] = []
        stdout_lines: list[str] = []
        threading.Thread(
            target=read_stdout, args=(proc.stdout, messages, stdout_lines), daemon=True
        ).start()
        threading.Thread(
            target=read_stderr, args=(proc.stderr, stderr_lines), daemon=True
        ).start()

        try:
            send(
                proc,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": 1,
                        "clientCapabilities": {},
                        "_meta": {
                            "startupHints": {
                                "nonInteractive": True,
                                "skipGitStatus": True,
                                "skipProjectLayout": True,
                            },
                            "clientType": "capworks-runtime-release-smoke",
                            "clientVersion": "1",
                        },
                    },
                },
            )
            initialized = wait_for_response(
                proc, messages, 1, args.timeout, stderr_lines, stdout_lines
            )
            if initialized.get("protocolVersion") != 1:
                raise RuntimeError(f"unexpected ACP version: {initialized.get('protocolVersion')!r}")
            capabilities = initialized.get("agentCapabilities") or {}
            if capabilities.get("loadSession") is not True:
                raise RuntimeError("runtime does not advertise required session/load capability")
            mcp_capabilities = capabilities.get("mcpCapabilities") or {}
            if mcp_capabilities.get("http") is not True:
                raise RuntimeError("runtime does not advertise required HTTP MCP capability")
            agent_version = (initialized.get("_meta") or {}).get("agentVersion")
            if agent_version != args.expected_version:
                raise RuntimeError(
                    f"initialize agentVersion mismatch: expected {args.expected_version!r}, got {agent_version!r}"
                )

            send(
                proc,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "session/new",
                    "params": {
                        "cwd": str(workspace),
                        "mcpServers": [],
                        "_meta": {"yoloMode": False},
                    },
                },
            )
            session_outcome, session_id = wait_for_session_new_or_auth_gate(
                proc, messages, 2, args.timeout, stderr_lines, stdout_lines
            )
            print(
                json.dumps(
                    {
                        "ok": True,
                        "version": args.expected_version,
                        "source_revision": args.source_revision,
                        "protocol_version": initialized["protocolVersion"],
                        "session_new": session_outcome,
                        "session_id": session_id,
                    },
                    sort_keys=True,
                )
            )
        finally:
            if proc.stdin is not None:
                proc.stdin.close()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001 - CLI boundary should print diagnostics.
        print(f"capworks runtime smoke failed: {exc}", file=sys.stderr)
        raise
