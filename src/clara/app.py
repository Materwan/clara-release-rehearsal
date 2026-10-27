"""The server: build everything, check everything, then serve on 127.0.0.1 only.

    clara-server              run (systemd starts it through clara-launcher)
    clara-server --check      the start-up checks only (settings, permissions, database, key), then exit

Start-up, in order: settings (refused when wrong) → file permissions (refused when others can read .env, the data
folder or the key) → the instance lock (one server per data folder) → schema migration (a backup first; a database
newer than this code is refused) → database integrity → the secrets store (a wrong key is refused) → recovery of what a
crash left half done → serve.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import sys
from collections.abc import AsyncIterator
from datetime import time

import uvicorn
from fastapi import FastAPI

from . import __version__
from .agent.model import ModelRunner
from .agent.prompt import Persona
from .agent.turn import Agent, ChatRequest
from .api import admin as admin_api
from .api import auth as auth_api
from .api import chat as chat_api
from .api import connections as connections_api
from .api import conversations as conversations_api
from .api import discord as discord_api
from .api import events as events_api
from .api import forms as forms_api
from .api import gate as gate_api
from .api import memory as memory_api
from .api import models as models_api
from .api import packs as packs_api
from .api import projects as projects_api
from .api import time as time_api
from .api import triggers as triggers_api
from .api import vault as vault_api
from .api import web as web_api
from .api import workspace as workspace_api
from .api.common import install_error_handlers
from .api.middleware import AuthMiddleware, BodyLimitMiddleware, SecurityHeadersMiddleware, TrafficMiddleware
from .auth import FailureLimiter, Tokens, WebSessions
from .connectors import setup as connectors_setup
from .connectors.asks import Questions
from .connectors.quizzes import Quizzes
from .db import AlreadyRunning, Database, InstanceLock, SchemaTooNew, migrate
from .events import Events, delivery
from .gate import Approvals, Audit, Gate, Grants, Rules
from .gate.allowances import Allowances
from .knowledge import vault as vault_setup
from .knowledge.conversation_files import ConversationFiles
from .knowledge.github_snapshot import GitHub as GitHubSnapshots
from .knowledge.projects import Projects
from .llm import Catalog, NoModel, Providers, Router
from .llm.retry import retryable
from .memory.conversations import Conversations
from .memory.facts import Facts
from .mirror.service import Mirror
from .ops import setup as ops_setup
from .ops.alerts import Alerts
from .ops.lifecycle import Lifecycle
from .ops.logstream import LogHub
from .ops.private import check_private, harden_tree, private_dir
from .ops.systemd import notify
from .ops.tailscale import FunnelOn, check_funnel_now
from .ops.traffic import TrafficLog
from .ops.upkeep import Upkeep, late_alert
from .ops.usage import UsageLog
from .packs import install as packs_install
from .packs.daily_note import seed as seed_daily_note
from .runtime import RuntimeSettings
from .secrets import SecretError, SecretsStore, load_key_material
from .services import Services
from .settings import Settings, SettingsError, load_settings
from .timekeeping import Reminders, Schedules, Stopping, Tasks
from .timekeeping.clara_jobs import ClaraJobs
from .timekeeping.schedules import CLOSING as SCHEDULE_CLOSING
from .timekeeping.schedules import Run, Schedule, instructions_of, message_of
from .timeutil import parse_iso, zone
from .tools.base import Toolbox, ToolContext, TurnState
from .tools.discord_tools import can_say, discord_tools
from .tools.folder_tools import FolderGrants, folder_tools
from .tools.knowledge_tools import knowledge_tools
from .tools.memory_tools import memory_tools
from .tools.time_tools import time_tools
from .triggers.setup import install as triggers_install
from .workspace import setup as workspace_setup

log = logging.getLogger("clara")

RESTORE_DRILL = "Monthly check: run `clara restore --verify` on the newest backup (see docs/OPERATIONS.md)."


class StartError(RuntimeError):
    """Clara cannot start: the message says why and what to do."""


# ----------------------------------------------------------------------
# Building
# ----------------------------------------------------------------------
def open_database(settings: Settings) -> Database:
    try:
        before, after, backup = migrate(settings.database, settings.backups_dir)
    except SchemaTooNew as error:
        raise StartError(str(error)) from None
    if before != after:
        log.info("database migrated from schema %d to %d%s", before, after, f" (backup: {backup})" if backup else "")
    db = Database(settings.database)
    check = db.value("PRAGMA quick_check", (), "")
    if check != "ok":
        raise StartError(f"The database {settings.database} is damaged ({check}): restore a backup (clara restore).")
    return db


def build_services(settings: Settings, db: Database, *, provider_factory=None) -> Services:
    runtime = RuntimeSettings(db)
    try:
        secrets = SecretsStore(db, load_key_material(settings.secret_key_file, settings.secret_key))
    except SecretError as error:
        raise StartError(str(error)) from None
    tokens = Tokens(db)
    events = Events(db, settings.events_max_days)
    alerts = Alerts(events)
    debug_until = lambda: parse_iso(runtime.get("debug_until")) if runtime.get("debug_until") else None  # noqa: E731
    traffic = TrafficLog(settings.logs_dir, settings.traffic_log_days, debug_until)
    logs = LogHub()
    traffic.sink = logs.traffic
    providers = Providers(settings, secrets, runtime, provider_factory)
    catalog = Catalog(db, providers)
    rules = Rules(db)
    audit = Audit(db)
    home = settings.timezone
    s = Services(
        settings=settings,
        db=db,
        runtime=runtime,
        secrets=secrets,
        tokens=tokens,
        web_sessions=WebSessions(db, tokens),
        limiter=FailureLimiter(),
        events=events,
        alerts=alerts,
        traffic=traffic,
        logs=logs,
        usage=UsageLog(db),
        facts=Facts(db),
        conversations=Conversations(db),
        mirror=Mirror(db, events),
        conversation_files=ConversationFiles(db),
        projects=Projects(
            db,
            settings.project_max_chars,
            settings.project_max_files,
            settings.project_inline_percent,
            settings.project_inline_tokens,
        ),
        reminders=Reminders(db, events, home),
        tasks=Tasks(db, events, home, max_reminders=settings.task_max_reminders),
        schedules=Schedules(db, events, home),
        rules=rules,
        gate=Gate(db, rules, runtime, vault_push=settings.vault_push),
        audit=audit,
        approvals=Approvals(db, events, rules, audit, alerts, expire_after=settings.approval_expire),
        grants=Grants(runtime, audit),
        questions=Questions(),
        quizzes=Quizzes(db),
        providers=providers,
        catalog=catalog,
        router=Router(providers, catalog, runtime),
        allowances=Allowances(db),
        folders=FolderGrants(db, runtime),
    )
    s.github_snapshots = GitHubSnapshots()
    tools = memory_tools() + time_tools() + knowledge_tools() + discord_tools() + folder_tools()
    s.toolbox = Toolbox(tools, s.gate, audit)
    s.toolbox.approvals = s.approvals
    s.toolbox.grants = s.grants
    s.toolbox.allowances = s.allowances
    s.approvals.allowances = s.allowances
    runner = ModelRunner(
        providers,
        s.usage,
        traffic,
        alerts,
        chat_slots=settings.chat_slots,
        background_slots=settings.background_slots,
        first_token_timeout=settings.first_token_timeout,
        idle_timeout=settings.idle_timeout,
    )
    s.agent = Agent(s, runner, Persona(settings.persona_file))
    s.agent.tool_conditions["discord_say"] = can_say
    s.agent.tool_conditions["discord_forget"] = lambda state: False  # only an approved request runs it
    wire(s)
    s.lifecycle = Lifecycle(s, settings.drain_seconds)
    return s


def wire(s: Services) -> None:
    """Connect the parts that call each other: the clock services and approvals run turns through the agent."""
    jobs = ClaraJobs(s.agent, s.settings.reminder_ai_timeout)
    s.reminders.composer = jobs.announce
    s.tasks.planner = jobs.plan
    s.tasks.follower = jobs.follow
    late = late_alert(s)
    for loop in (s.reminders, s.tasks, s.schedules):
        loop.late = late
        loop.late_after = float(s.settings.late_alert_seconds)
    s.schedules.runner = lambda schedule, number, run: run_schedule(s, schedule, number, run)
    s.schedules.closer = lambda schedule, run: close_run(s, schedule, run)
    s.schedules.check_packs = lambda ids: s.packs.check(ids, "schedule") if s.packs is not None else list(ids)
    hours, minutes = s.settings.waiting_remind_at.split(":")
    s.schedules.remind_at = time(int(hours), int(minutes))
    s.schedules.waiting = s.approvals.pending_count
    s.approvals.toolbox = s.toolbox
    s.approvals.followup = s.agent.followup

    def approved_context(approval) -> ToolContext:
        conversation = s.conversations.get(approval.conversation_id) if approval.conversation_id else None
        state = TurnState(
            turn_id=approval.turn_id or "approval",
            conversation_id=approval.conversation_id or "",
            client="server",
            mode="unattended",
            timezone=s.settings.timezone,
            project_id=conversation.project_id if conversation else None,
            incognito=bool(conversation and conversation.incognito),
            local_only=bool(conversation and conversation.local_only),
        )
        return ToolContext(s, state)

    s.approvals.context_factory = approved_context


async def run_schedule(s: Services, schedule: Schedule, number: int, run: Run) -> str:
    """One run: a turn in a conversation (the schedule's own, or a new one for each run) with the schedule's packs and
    model. The run remembers its conversation, which is where your answer comes back when it asks something."""
    from .agent.turn import ServerStopping

    packs = list(schedule.packs)
    if schedule.per_run:
        day = run.due.astimezone(zone(schedule.timezone)).date().isoformat()
        conversation_id = s.conversations.create(
            "server",
            title=f"{schedule.name} — {day}",
            project_id=schedule.project_id,
            local_only=schedule.local_only,
            model=schedule.model,
            packs=packs,
        ).id
    else:
        conversation_id = schedule.conversation_id
        if conversation_id is None or s.conversations.get(conversation_id) is None:
            conversation_id = s.conversations.create(
                "server",
                kind="schedule",
                title=schedule.name,
                project_id=schedule.project_id,
                local_only=schedule.local_only,
            ).id
            s.db.execute("UPDATE schedules SET conversation_id = ? WHERE id = ?", (conversation_id, schedule.id))
        s.conversations.update(conversation_id, packs=packs, model=schedule.model)
    s.db.execute("UPDATE schedule_runs SET conversation_id = ? WHERE id = ?", (conversation_id, run.id))
    if delivery.forum(schedule.deliver):
        delivery.started(
            s,
            f"Scheduled task: {schedule.name}",
            conversation=conversation_id,
            payload={"schedule": schedule.id, "run": run.id},
        )
    request = ChatRequest(
        message=message_of(schedule),
        client="server",
        conversation=conversation_id,
        instructions=instructions_of(schedule),
        prefix=f"[Scheduled task “{schedule.name}”, run {number}]",
        timezone=schedule.timezone,
        mode="unattended",
        kind="schedule",
        quiet=True,
        model=schedule.model or None,
    )
    attempt = 1
    while True:
        before = s.conversations.message_count(conversation_id)
        try:
            return await s.agent.answer(request)
        except ServerStopping:
            raise Stopping() from None
        except Exception as error:
            # A turn that used a tool or wrote text stores what it did: only a run that stored nothing is tried again.
            done = s.conversations.message_count(conversation_id) != before
            if done or attempt >= schedule.attempts or not retryable(error):
                raise
            log.warning(
                "schedule %s: attempt %d of %d failed (%s), trying again",
                schedule.id,
                attempt,
                schedule.attempts,
                error,
            )
            await s.schedules.pause_before_retry(attempt)
            attempt += 1


async def close_run(s: Services, schedule: Schedule, run) -> None:
    """A run that waits for an answer nobody gave: Clara finishes it from what she has (in its own conversation)."""
    from .agent.turn import ServerStopping

    try:
        await s.agent.answer(
            ChatRequest(
                message=SCHEDULE_CLOSING,
                client="server",
                conversation=run["conversation_id"],
                timezone=schedule.timezone,
                mode="unattended",
                kind="schedule",
                quiet=True,
                model=schedule.model or None,
            )
        )
    except ServerStopping:
        raise Stopping() from None


def recover(s: Services) -> None:
    """What a crash (kill -9, a power cut) left half done is put straight before anything runs."""
    healed = s.conversations.heal_versions()
    failed = s.approvals.recover()
    interrupted = s.schedules.recover()
    cut = s.triggers.recover() if s.triggers is not None else 0
    if healed or failed or interrupted or cut:
        log.warning(
            "after an unclean stop: %d question(s) put back, %d approved action(s) marked to check, "
            "%d scheduled run(s) to run again, %d hook run(s) put right",
            healed,
            failed,
            interrupted,
            cut,
        )
    if s.workspace is not None:
        s.workspace.recover()


# ----------------------------------------------------------------------
# The web application
# ----------------------------------------------------------------------
def create_app(s: Services) -> FastAPI:
    debug = s.settings.debug

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        s.lifecycle.loop = asyncio.get_running_loop()
        tasks = [asyncio.create_task(loop.run(), name=f"loop-{loop.name}") for loop in s.due_loops()]
        upkeep = Upkeep(s)
        upkeep.start()
        s.monitors = upkeep
        for part in (s.tailscale, s.backups, s.vault, s.sandbox, s.discord):
            starter = getattr(part, "start", None)
            if starter is not None:
                try:
                    starter()
                except Exception:
                    log.exception("could not start %s", type(part).__name__)
        tasks.append(asyncio.create_task(first_models(s), name="models"))
        s.reminders.ensure_system(RESTORE_DRILL, _first_of_next_month(s), "monthly")
        s.events.publish(
            "server", "Clara is running", f"Clara {__version__}", payload={"state": "running", "boot": s.boot_id}
        )
        write_boot_record(s)
        notify("READY=1")
        try:
            yield
        finally:
            notify("STOPPING=1")
            for task in tasks:
                task.cancel()
            await upkeep.stop()
            for part in (s.discord, s.tailscale, s.backups, s.vault, s.sandbox, s.updater):
                stopper = getattr(part, "stop", None)
                if stopper is not None:
                    with contextlib.suppress(Exception):
                        result = stopper()
                        if asyncio.iscoroutine(result):
                            await result
            await s.agent.runs.cancel_all("the server stopped")  # saved as interrupted, before the database closes
            await s.approvals.close()
            await s.agent.drain_background()
            await s.providers.aclose()
            s.traffic.flush()

    app = FastAPI(
        title="Clara",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs" if debug else None,
        redoc_url=None,
        openapi_url="/openapi.json" if debug else None,
    )
    app.state.services = s
    install_error_handlers(app)
    for router in (
        auth_api.router,
        chat_api.router,
        conversations_api.router,
        forms_api.router,
        forms_api.questions_router,
        memory_api.router,
        time_api.router,
        events_api.router,
        projects_api.router,
        gate_api.router,
        models_api.router,
        admin_api.router,
    ):
        app.include_router(router)
    app.include_router(workspace_api.router)
    app.include_router(triggers_api.router)
    app.include_router(connections_api.router)
    app.include_router(vault_api.router)
    app.include_router(packs_api.router)
    app.include_router(discord_api.router)
    app.include_router(web_api.router)
    # outermost last: security headers → traffic log → body limit → authentication → the routes
    app.add_middleware(AuthMiddleware, services=s)
    app.add_middleware(BodyLimitMiddleware)
    app.add_middleware(TrafficMiddleware, services=s)
    app.add_middleware(SecurityHeadersMiddleware)
    return app


async def first_models(s: Services) -> None:
    """At start: list what the providers offer; with no default model yet, take the first one of a local host."""
    try:
        found = await s.catalog.listing(force=True)
    except Exception:
        log.exception("could not list the models")
        return
    if not s.runtime.get("default_model"):
        local = [info for info in found if info.local and info.listed]
        if local:
            s.runtime.set("default_model", local[0].ref)
            s.catalog.set_shown([local[0].ref], True)
            log.info("no default model was set: using %s (change it with /model)", local[0].ref)
    try:
        s.router.choose()
    except NoModel as error:
        log.warning("%s", error)


def _first_of_next_month(s: Services) -> str:
    from datetime import datetime

    now = datetime.now(zone(s.settings.timezone))
    year, month = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
    return f"{year:04d}-{month:02d}-01T10:00"


def write_boot_record(s: Services) -> None:
    """For the launcher: this boot is up (it checks the id it gave the process, then /health)."""
    record = s.settings.run_dir / "boot"
    try:
        private_dir(record.parent)
        record.write_text(os.environ.get("CLARA_BOOT_ID", s.boot_id), encoding="utf-8")
    except OSError:
        log.exception("could not write the boot record")


# ----------------------------------------------------------------------
# Running
# ----------------------------------------------------------------------
class ClaraServer(uvicorn.Server):
    """Uvicorn, with signals turned into Clara's own stop: the first SIGTERM or Ctrl+C drains (within the limit), the
    second cuts."""

    lifecycle: Lifecycle

    @contextlib.contextmanager
    def capture_signals(self):
        handled = (signal.SIGINT, signal.SIGTERM)
        original = {sig: signal.signal(sig, self.handle_exit) for sig in handled}
        try:
            yield
        finally:
            for sig, handler in original.items():
                signal.signal(sig, handler)

    def handle_exit(self, sig, frame) -> None:
        self.lifecycle.stop_threadsafe(force=self.lifecycle.stopping)


def prepare(settings: Settings) -> tuple[Database, InstanceLock]:
    if settings.check_permissions:
        problems = check_private(settings.env_file, settings.data_dir, settings.secret_key_file)
        if problems:
            raise StartError("Files that must be private are not:\n" + "\n".join(f"- {p}" for p in problems))
    if settings.tailscale == "serve":
        try:
            check_funnel_now(settings.tailscale_bin, settings.tailscale_port)
        except FunnelOn as error:
            raise StartError(str(error)) from None
    private_dir(settings.data_dir)
    harden_tree(settings.data_dir, skip=(settings.workspace_dir,))
    lock = InstanceLock(settings.lock_file)
    try:
        lock.acquire()
    except AlreadyRunning as error:
        raise StartError(str(error)) from None
    return open_database(settings), lock


def setup_logging(debug: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="clara-server", description="Clara's server (listens on 127.0.0.1 only).")
    parser.add_argument("--check", action="store_true", help="run the start-up checks, then exit")
    args = parser.parse_args(argv)
    try:
        settings = load_settings()
    except SettingsError as error:
        print(error, file=sys.stderr)
        return 2
    setup_logging(settings.debug)
    try:
        db, lock = prepare(settings)
        raise RuntimeError("rehearsal: deliberately broken release")
        services = build_services(settings, db)
    except StartError as error:
        log.error("%s", error)
        return 3
    if args.check:
        print(f"Clara {__version__}: settings, permissions, database and secrets key are fine.")
        lock.release()
        return 0
    extend(services)
    recover(services)
    seed_daily_note(services)
    services.logs.attach()
    app = create_app(services)
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=settings.port,
        log_level="warning",
        proxy_headers=False,
        timeout_graceful_shutdown=3,
        server_header=False,
    )
    server = ClaraServer(config)
    server.lifecycle = services.lifecycle

    def exit_now(code: int) -> None:
        server.should_exit = True

    services.lifecycle.on_exit = exit_now
    log.info("Clara %s listening on http://127.0.0.1:%d", __version__, settings.port)
    try:
        server.run()
    finally:
        services.logs.detach()
        services.traffic.close()
        db.close()
        lock.release()
    return services.lifecycle.exit_code


def extend(s: Services) -> None:
    """The parts that bring their own tools and services: the workspace and its script runner, the web, GitHub and
    Drive."""
    workspace_setup.install(s)
    connectors_setup.install(s)
    vault_setup.install(s)
    packs_install(s)
    triggers_install(s)
    ops_setup.install(s)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
