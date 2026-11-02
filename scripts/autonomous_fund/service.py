"""Child supervision for systemd units and for `serve` (Windows/no systemd).

Recovery never replays an order. Every start writes a `service` STOP; only the
independent monitor may lift that (or a liveness fault), after its own checks.
"""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

from . import stops
from .risk import RiskError, instant

RESTART_SECONDS = 30  # Same delay as the systemd units' RestartSec.
WINDOWS_GRACE_SECONDS = 10  # Ctrl+C reaches the whole console; then the tree is ended.


FATAL_HEALTH = {"heartbeat_stale", "heartbeat_unavailable", "runner_exited", "agent_call_stalled",
                "iteration_stalled", "runner_upgrade_required"}


def supervision_reason(root, started, now, *, grace_seconds=180):
    """Require an independent, fresh monitor; never infer health from own PID."""
    if (now - started).total_seconds() < grace_seconds and (Path(root) / "STOP").exists():
        return None
    try:
        state = json.loads((Path(root) / "monitor.json").read_text(encoding="utf-8"))
        age = (now - instant(state["at"])).total_seconds()
        if not 0 <= age <= 90 or instant(state["at"]) < started:
            return "independent_monitor_stale"
        if not state.get("telegram_configured"):
            return "independent_notifications_unconfigured"
        if state["health"] in FATAL_HEALTH:
            return state["health"]
    except (OSError, ValueError, KeyError, TypeError):
        return "independent_monitor_unavailable"
    return None


def system_stopping():
    """True only while the operating system is shutting down (systemd).

    A stop signal during shutdown or reboot is not the operator closing the fund,
    so trading may resume after boot; any other signal is a manual stop. When the
    state cannot be read, the stop counts as manual: a human resumes it.
    """
    if os.name != "posix":
        return False
    try:
        state = subprocess.run(["systemctl", "is-system-running"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return False
    return str(state).strip() == "stopping"


def stopped_by_signal(root):
    """Record a signalled exit: manual unless the operating system is shutting down."""
    if system_stopping():
        return stops.write(root, "service", "system_shutdown")
    return stops.write(root, "operator", "stopped_by_operator")


def stop_children(children, timeout=30, clock=time.monotonic):
    """Stop several supervised children with ONE shared grace period, then force the rest.

    POSIX: SIGINT to each process group, then SIGKILL. Windows does not stop
    grandchildren with their parent and has no SIGINT for another process: the
    console Ctrl+C reaches every child, so wait briefly, then end each tree.
    Never raises, so one stuck child cannot leave the others running.
    """
    alive = [child for child in children if child.poll() is None]
    if not alive:
        return
    if os.name == "posix":
        for child in alive:
            try:
                os.killpg(child.pid, signal.SIGINT)
            except ProcessLookupError:
                pass
        grace = timeout
    else:
        grace = min(timeout, WINDOWS_GRACE_SECONDS)
    deadline = clock() + grace
    for child in alive:
        try:
            child.wait(timeout=max(0.0, deadline - clock()))
        except subprocess.TimeoutExpired:
            pass
    for child in alive:
        if child.poll() is not None:
            continue
        try:
            if os.name == "posix":
                os.killpg(child.pid, signal.SIGKILL)
            else:
                subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"], capture_output=True)
            child.wait(timeout=5)
        except (ProcessLookupError, subprocess.TimeoutExpired, OSError) as exc:
            print(f"Child {child.pid} did not stop: {type(exc).__name__}", flush=True)


def stop_child(child, timeout=30):
    stop_children([child], timeout)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("role", choices=["runner", "monitor"])
    parser.add_argument("--mode", choices=["local", "paper"], help="Default: FUND_MODE from --env-file (local if unset)")
    parser.add_argument("--run-dir", type=Path, help="Default: FUND_RUN_DIR or artifacts/fund-<mode>")
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.json"))
    args = parser.parse_args(argv)
    # Fail explicitly on absent keys rather than silently disabling notifications.
    from .environment import load_environment
    load_environment(args.env_file)
    if not os.environ.get("FUND_TELEGRAM_BOT_TOKEN") or not os.environ.get("FUND_TELEGRAM_CHAT_ID"):
        raise RiskError("Server services require FUND_TELEGRAM_BOT_TOKEN and FUND_TELEGRAM_CHAT_ID")
    # Every (re)start reads the mode from .env; a unit that names another mode is
    # an operator mistake, never silently resolved in either direction.
    from .settings import configured_mode, startup
    configured = configured_mode(os.environ)
    if args.mode and configured and args.mode != configured:
        raise RiskError(f"This service was generated with --mode {args.mode}, but FUND_MODE={configured} in "
                        f"{args.env_file}: regenerate the services without --mode so .env decides, or change FUND_MODE")
    derived = args.run_dir is None
    args.mode, args.run_dir, _, _ = startup(args.mode or "start", args.run_dir, os.environ)
    if derived:
        sandbox = Path(__file__).resolve().parents[2] / "artifacts"
        resolved = args.run_dir.resolve()
        if resolved != sandbox and sandbox not in resolved.parents:
            raise RiskError(f"FUND_RUN_DIR for server services must be under {sandbox}: the service may only write there")
    args.run_dir.mkdir(parents=True, exist_ok=True)
    if args.role == "runner":
        # No bootstrap interval with unmonitored fills. A stronger existing
        # STOP (operator or fault) is kept; the monitor alone may lift this one.
        # A brand-new account book (first start, or a switched FUND_MODE/FUND_RUN_DIR)
        # is verified by a person once, never resumed automatically.
        if (args.run_dir / "state.sqlite3").exists():
            stops.write(args.run_dir, "service", "service_start")
        else:
            stops.write(args.run_dir, "operator", "first_start")
    exiting = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: exiting.set())
    command = [sys.executable, "-m", "scripts.autonomous_fund.run", args.mode if args.role == "runner" else "monitor",
               "--run-dir", str(args.run_dir), "--env-file", str(args.env_file), "--config", str(args.config)]
    if args.role == "runner":
        command.append("--require-monitor")
    started = datetime.now(timezone.utc)
    # A stopped monitor must prevent fills BEFORE a model returns. The five-
    # second supervisor checks run in a different process from the strategy.
    child = subprocess.Popen(command, start_new_session=os.name == "posix")
    reason = None
    try:
        while child.poll() is None and not exiting.wait(5):
            now = datetime.now(timezone.utc)
            if args.role == "runner":
                reason = supervision_reason(args.run_dir, started, now)
            elif (now - started).total_seconds() > 180:
                # Supervise the watchdog's child too: a hung upload must not
                # leave an alive PID that systemd would mistake for progress.
                try:
                    progress = instant(json.loads((args.run_dir / "monitor.json").read_text(encoding="utf-8"))["at"])
                    if progress < started or not 0 <= (now - progress).total_seconds() <= 90:
                        reason = "monitor_child_stalled"
                except Exception:
                    reason = "monitor_child_stalled"
            if reason:
                stops.write(args.run_dir, "watchdog", reason)
                from .execution import operator_connection
                from .journal import encode
                try:
                    with operator_connection(args.run_dir) as db:
                        db.execute("INSERT INTO events(at,kind,body) VALUES(?,?,?)", (
                            str(now), "notification", encode({"kind": "server_supervision_stop", "details": reason})))
                except Exception:
                    pass  # STOP does not depend on a writable journal.
                print(f"Server supervision stopped trading: {reason}; explicit resume required.", flush=True)
                break
    finally:
        if args.role == "runner":
            # systemctl stop / Ctrl+C is a manual close that stays stopped; a
            # child that exited by itself or an OS shutdown is abnormal and may
            # resume once the monitor confirms health.
            if not reason and exiting.is_set():
                stopped_by_signal(args.run_dir)
            else:
                stops.write(args.run_dir, "service", "service_exit")
        stop_child(child)
    return 1 if reason else (child.returncode or 0)


def serve(env_file, config, run_dir=None, *, popen=subprocess.Popen, clock=time.monotonic, stop=None,
          install_signals=True):
    """Keep the runner and monitor services alive where systemd is unavailable (e.g. Windows).

    Equivalent to the two generated units: each role runs under this module's
    supervision and is restarted RESTART_SECONDS after it last started when it
    exits. Unattended running requires Telegram, like the server services.
    """
    from .environment import load_environment
    load_environment(env_file)
    if not os.environ.get("FUND_TELEGRAM_BOT_TOKEN") or not os.environ.get("FUND_TELEGRAM_CHAT_ID"):
        raise RiskError("Unattended running requires FUND_TELEGRAM_BOT_TOKEN and FUND_TELEGRAM_CHAT_ID in .env")
    from .settings import startup
    _, account_dir, _, _ = startup("start", run_dir, os.environ)
    stop = stop or threading.Event()
    if install_signals:
        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, lambda *_: stop.set())
    children, started, requested = {}, {}, False
    try:
        while True:
            for role in ("runner", "monitor"):
                child = children.get(role)
                if child is not None and child.poll() is None:
                    continue
                if child is not None:
                    if clock() - started[role] < RESTART_SECONDS:
                        continue
                    print(f"{role} exited with code {child.poll()}; restarting", flush=True)
                command = [sys.executable, "-m", "scripts.autonomous_fund.service", role, "--env-file", str(env_file),
                           "--config", str(config)] + (["--run-dir", str(run_dir)] if run_dir else [])
                children[role] = popen(command)
                started[role] = clock()
            if stop.wait(5):
                requested = True
                break
    finally:
        if requested:
            # Before the children: on Windows they may be terminated without
            # running their own cleanup, and Ctrl+C here is a manual close.
            stopped_by_signal(account_dir)
        stop_children(list(children.values()))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RiskError as exc:
        raise SystemExit(str(exc)) from None
