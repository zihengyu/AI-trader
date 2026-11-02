"""systemd child supervision. Recovery never clears STOP or replays an order."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
from datetime import datetime, timezone

from .risk import RiskError, instant


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


def stop_child(child, timeout=30):
    if child.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(child.pid, signal.SIGINT)
        else:
            child.terminate()
    except ProcessLookupError:
        return
    try:
        child.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            os.killpg(child.pid, signal.SIGKILL)
        else:
            child.kill()
        child.wait(timeout=5)


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
        # No bootstrap interval with unmonitored fills. The operator first
        # verifies both services and explicitly resumes the persisted account.
        (args.run_dir / "STOP").touch()
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
                (args.run_dir / "STOP").touch()
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
            # Applies even to a deliberate systemctl stop or clean child exit.
            (args.run_dir / "STOP").touch()
        stop_child(child)
    return 1 if reason else (child.returncode or 0)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RiskError as exc:
        raise SystemExit(str(exc)) from None
