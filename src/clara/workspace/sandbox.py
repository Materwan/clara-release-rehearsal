"""Running scripts: one rootless Podman container per run, removed afterwards.

Each run gets: no network (unless the run asks and the gate allows it), a read-only root filesystem, every capability
dropped, `no-new-privileges`, the user Clara runs as (mapped with `--userns=keep-id`), limits on processes, memory and
CPU, a wall-clock timeout, and an **empty environment** (no secret, token or host path; not even the host's proxy
variables). Only the workspace is mounted, read-write, and an empty read-only tmpfs hides its `.git`.

The model gets the exit code and the first 64 KB of output; the whole log (up to 10 MB) is kept in the workspace under
`runs/`, committed with whatever the run changed. There is **no fallback** to a plain process: when rootless Podman does
not work (`clara doctor` says why), `ws_run` is not offered.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass

from ..db import Database
from ..settings import Settings
from ..timeutil import now_iso, utcnow
from .paths import clean, resolve
from .store import Workspace, WorkspaceError

log = logging.getLogger(__name__)

LOG_MAX_BYTES = 10 * 1024 * 1024
CHECK_TIMEOUT = 60.0


@dataclass(frozen=True)
class RunResult:
    run_id: int
    exit_code: int | None
    output: str  # the beginning of the output, for the model
    total_bytes: int
    timed_out: bool
    seconds: float
    log_path: str
    commit: str


def podman_args(
    settings: Settings, image: str, workspace: str, workdir: str, network: bool, timeout: int, name: str
) -> list[str]:
    uid, gid = os.getuid(), os.getgid()
    return [
        settings.podman,
        "run",
        "--rm",
        "--name",
        name,
        "--network=" + ("pasta" if network else "none"),
        "--read-only",
        "--cap-drop=all",
        "--security-opt=no-new-privileges",
        "--userns=keep-id",
        f"--user={uid}:{gid}",
        f"--pids-limit={settings.run_pids}",
        f"--memory={settings.run_memory}",
        f"--memory-swap={settings.run_memory}",
        f"--cpus={settings.run_cpus:g}",
        f"--timeout={timeout}",
        "--http-proxy=false",
        "--env=HOME=/tmp",
        "--env=LANG=C.UTF-8",
        f"--volume={workspace}:/workspace:rw",
        # notmpcopyup: podman would otherwise copy the host .git into this tmpfs, which fails ("crun: write: No space
        # left on device") once that copy needs more than 64k, and would put the repository inside the container.
        "--tmpfs=/workspace/.git:ro,size=64k,mode=000,notmpcopyup",
        "--tmpfs=/tmp:rw,size=256m",
        f"--workdir=/workspace/{workdir}".rstrip("/"),
        "--log-driver=none",
        image,
        "bash",
        "-c",
    ]


class Sandbox:
    def __init__(self, settings: Settings, workspace: Workspace, db: Database, image: str):
        self.settings = settings
        self.workspace = workspace
        self.db = db
        self.image = image
        self.ready = False
        self.problem = "not checked yet" if settings.run == "auto" else "CLARA_RUN is set to off"
        self._slots = asyncio.Semaphore(settings.run_slots)
        self._check: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Is it usable?
    # ------------------------------------------------------------------
    def start(self) -> None:
        if self.settings.run == "auto":
            self._check = asyncio.ensure_future(self.check())

    async def stop(self) -> None:
        if self._check is not None:
            self._check.cancel()

    async def _exec(self, *args: str, timeout: float = CHECK_TIMEOUT) -> tuple[int, str]:
        try:
            process = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=_minimal_env()
            )
        except FileNotFoundError:
            return 127, f"{args[0]} is not installed"
        try:
            out, _ = await asyncio.wait_for(process.communicate(), timeout)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            return 124, "timed out"
        return process.returncode or 0, out.decode(errors="replace").strip()

    async def check(self) -> list[tuple[str, bool, str]]:
        """Every check `clara doctor` shows; `ready` only when all pass."""
        results: list[tuple[str, bool, str]] = []
        code, out = await self._exec(self.settings.podman, "info", "--format", "json")
        if code != 0:
            results.append(("podman", False, out[:300] or "podman info failed"))
            return self._done(results)
        try:
            info = json.loads(out)
        except ValueError:
            results.append(("podman", False, "podman info did not answer JSON"))
            return self._done(results)
        host = info.get("host", {})
        version = info.get("version", {}).get("Version", "?")
        results.append(("podman", True, f"version {version}"))
        rootless = bool(host.get("security", {}).get("rootless"))
        results.append(("rootless", rootless, "rootless" if rootless else "podman runs as root: refused"))
        controllers = set(host.get("cgroupControllers") or [])
        cgroup2 = host.get("cgroupVersion") == "v2"
        needed = {"memory", "pids", "cpu"}
        results.append(
            (
                "cgroups",
                cgroup2 and needed <= controllers,
                f"cgroup {host.get('cgroupVersion')}, delegated: {', '.join(sorted(controllers)) or 'none'}",
            )
        )
        id_maps = host.get("idMappings", {})
        mapped = bool(id_maps.get("uidmap")) and bool(id_maps.get("gidmap"))
        results.append(
            ("subuid/subgid", mapped, "ranges found" if mapped else "add ranges to /etc/subuid and /etc/subgid")
        )
        code, out = await self._exec(self.settings.podman, "image", "exists", self.image)
        results.append(
            ("image", code == 0, self.image if code == 0 else f"{self.image} is missing (clara doctor --pull)")
        )
        if all(ok for _, ok, _ in results):
            code, out = await self._exec(
                self.settings.podman,
                "run",
                "--rm",
                "--network=none",
                "--read-only",
                "--cap-drop=all",
                self.image,
                "bash",
                "-c",
                "(getent hosts example.com || curl -sm3 http://1.1.1.1) >/dev/null 2>&1 && "
                "echo NETWORK || echo ISOLATED",
                timeout=120,
            )
            isolated = code == 0 and "ISOLATED" in out
            results.append(("isolation", isolated, "no network inside" if isolated else out[:300]))
        return self._done(results)

    def _done(self, results: list[tuple[str, bool, str]]) -> list[tuple[str, bool, str]]:
        failed = [f"{name}: {detail}" for name, ok, detail in results if not ok]
        self.ready = not failed and self.settings.run == "auto"
        self.problem = "" if self.ready else ("; ".join(failed) or self.problem)
        if not self.ready:
            log.warning("scripts are off: %s", self.problem)
        return results

    # ------------------------------------------------------------------
    # A run
    # ------------------------------------------------------------------
    async def run(
        self, command: str, workdir: str, timeout: int, network: bool, *, conversation_id: str | None, turn_id: str
    ) -> RunResult:
        if not self.ready:
            raise WorkspaceError(f"Scripts cannot run: {self.problem}.")
        if not command.strip():
            raise WorkspaceError("The command is empty.")
        timeout = max(1, min(int(timeout or self.settings.run_timeout), self.settings.run_max_timeout))
        folder = clean(workdir)
        if folder and not resolve(self.workspace.root, folder, must_exist=True).is_dir():
            raise WorkspaceError(f"{folder} is not a folder.")
        digest = hashlib.sha256(command.encode()).hexdigest()[:32]
        run_id = self.db.insert(
            "INSERT INTO ws_runs (started_at, command_digest, workdir, network, conversation_id, turn_id)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (now_iso(), digest, folder, int(network), conversation_id, turn_id),
        )
        stamp = utcnow().strftime("%Y%m%dT%H%M%SZ")
        log_name = f"runs/{stamp}-{run_id}.log"
        name = f"clara-run-{run_id}-{uuid.uuid4().hex[:6]}"
        args = podman_args(self.settings, self.image, str(self.workspace.root), folder, network, timeout, name) + [
            command
        ]
        begun = time.monotonic()
        async with self._slots:
            exit_code, captured, total, timed_out = await self._spawn(args, timeout + 15, name)
        seconds = time.monotonic() - begun
        timed_out = timed_out or (exit_code not in (0, None) and seconds >= timeout)  # podman's own --timeout killed it
        header = (
            f"$ {command}\n# workdir: /workspace/{folder}  network: {'on' if network else 'off'}  "
            f"timeout: {timeout}s\n# exit: {exit_code}{'  (timed out)' if timed_out else ''}  "
            f"took: {seconds:.1f}s  output: {total} bytes{' (log cut at 10 MB)' if total > LOG_MAX_BYTES else ''}\n\n"
        )
        log_path = resolve(self.workspace.root, log_name)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_bytes(header.encode() + captured)
        summary = command.strip().splitlines()[0][:100]
        commit = await self.workspace.commit(self.workspace.message("ws_run", summary, turn_id, conversation_id or ""))
        status = "done" if exit_code == 0 and not timed_out else "failed"
        self.db.execute(
            "UPDATE ws_runs SET finished_at = ?, exit_code = ?, timed_out = ?, duration_ms = ?, log_path = ?,"
            " commit_sha = ?, status = ? WHERE id = ?",
            (now_iso(), exit_code, int(timed_out), int(seconds * 1000), log_name, commit, status, run_id),
        )
        shown = captured[: self.settings.run_output_bytes].decode("utf-8", errors="replace")
        return RunResult(run_id, exit_code, shown, total, timed_out, seconds, log_name, commit)

    async def _spawn(self, args: list[str], hard_limit: float, name: str) -> tuple[int | None, bytes, int, bool]:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=_minimal_env(),
        )
        assert process.stdout is not None
        captured = bytearray()
        total = 0
        timed_out = False

        async def read() -> None:
            nonlocal total
            while chunk := await process.stdout.read(65536):  # type: ignore[union-attr]
                total += len(chunk)
                if len(captured) < LOG_MAX_BYTES:
                    captured.extend(chunk[: LOG_MAX_BYTES - len(captured)])

        try:
            await asyncio.wait_for(asyncio.gather(read(), process.wait()), hard_limit)
        except TimeoutError:
            timed_out = True
            await self._exec(self.settings.podman, "kill", name, timeout=15)
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()
        except asyncio.CancelledError:
            await self._exec(self.settings.podman, "kill", name, timeout=15)
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            raise
        return process.returncode, bytes(captured), total, timed_out

    def recover(self) -> int:
        rows = self.db.execute(
            "UPDATE ws_runs SET status = 'interrupted', finished_at = ? WHERE status = 'running'", (now_iso(),)
        ).rowcount
        return rows

    def runs(self, limit: int = 50) -> list[dict]:
        return [dict(row) for row in self.db.all("SELECT * FROM ws_runs ORDER BY id DESC LIMIT ?", (limit,))]


def _minimal_env() -> dict[str, str]:
    """What podman itself needs to find its runtime (never passed into the container: podman does not pass the
    environment unless asked)."""
    keep = (
        "PATH",
        "HOME",
        "XDG_RUNTIME_DIR",
        "USER",
        "LOGNAME",
        "DBUS_SESSION_BUS_ADDRESS",
        "CONTAINERS_CONF",
        "CONTAINERS_STORAGE_CONF",
        "TMPDIR",
    )
    return {key: value for key, value in os.environ.items() if key in keep}
