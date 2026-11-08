"""Generate reviewable Linux service files; never install or start a service."""
import argparse
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3

from .risk import RiskError


def unit_quote(value):
    text = str(value)
    if any(c in text for c in '\r\n\x00"$%\\'):
        raise RiskError("Service paths must not contain control characters, quotes, $, %, or backslashes")
    return '"' + text + '"'


def unit_path(value):
    """A path for a setting systemd does NOT unquote (WorkingDirectory=): validated, written as is."""
    unit_quote(value)  # Same refusal of control characters, quotes, $, % and backslashes.
    return str(value)


def generate(repo, run_dir, env_file, user, mode):
    """mode/run_dir None: every (re)start reads FUND_MODE / FUND_RUN_DIR from env_file."""
    if mode not in {None, "local", "paper"} or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", user):
        raise RiskError("Choose local/paper and an existing non-root Linux service user")
    if user == "root":
        raise RiskError("Run trading services under a dedicated non-root user")
    repo, env_file = PurePosixPath(str(repo)), PurePosixPath(str(env_file))
    run_dir = PurePosixPath(str(run_dir)) if run_dir is not None else None
    if not all(value.is_absolute() for value in (repo, env_file, run_dir or repo)):
        raise RiskError("Service paths must be absolute Linux paths")
    python = repo / ".venv" / "bin" / "python"
    units = {}
    for role in ("runner", "monitor"):
        args = [python, "-m", "scripts.autonomous_fund.service", role]
        args += ["--mode", mode] if mode else []
        args += ["--run-dir", run_dir] if run_dir else []
        args += ["--env-file", env_file]
        units[f"lumibot-fund-{role}.service"] = "\n".join([
            "[Unit]", f"Description=LumiBot paper fund {role}",
            "Wants=network-online.target", "After=network-online.target", "StartLimitIntervalSec=0", "",
            "[Service]", "Type=exec", f"User={user}", f"WorkingDirectory={unit_path(repo)}",
            "ExecStart=" + " ".join(unit_quote(arg) for arg in args),
            "Restart=always", "RestartSec=30", "KillSignal=SIGINT", "KillMode=mixed", "TimeoutStopSec=45",
            "Environment=PYTHONUNBUFFERED=1 LUMIBOT_DISABLE_DOTENV=true",
            "UMask=0077", "NoNewPrivileges=true", "PrivateTmp=true", "ProtectSystem=strict", "ProtectHome=read-only",
            "ReadWritePaths=" + " ".join(unit_quote(p) for p in sorted({repo / "artifacts", run_dir or repo / "artifacts"})),
            "StandardOutput=journal", "StandardError=journal", "",
            "[Install]", "WantedBy=multi-user.target", ""])
    # A daily journal snapshot after the close and the review; Persistent catches up after downtime.
    backup_args = [python, "-m", "scripts.autonomous_fund.deploy", "rotate-backup", "--env-file", env_file]
    backup_args += ["--run-dir", run_dir] if run_dir else []
    units["lumibot-fund-backup.service"] = "\n".join([
        "[Unit]", "Description=LumiBot paper fund daily journal backup", "",
        "[Service]", "Type=oneshot", f"User={user}", f"WorkingDirectory={unit_path(repo)}",
        "ExecStart=" + " ".join(unit_quote(arg) for arg in backup_args),
        "Environment=PYTHONUNBUFFERED=1 LUMIBOT_DISABLE_DOTENV=true",
        "UMask=0077", "NoNewPrivileges=true", "PrivateTmp=true", "ProtectSystem=strict", "ProtectHome=read-only",
        "ReadWritePaths=" + " ".join(unit_quote(p) for p in sorted({repo / "artifacts", run_dir or repo / "artifacts"})),
        "StandardOutput=journal", "StandardError=journal", ""])
    # The data desk (数据员): always on, collects and prepares research data; never trades.
    desk_args = [python, "-m", "scripts.autonomous_fund.run", "datadesk", "--env-file", env_file]
    desk_args += ["--run-dir", run_dir] if run_dir else []
    units["lumibot-fund-desk.service"] = "\n".join([
        "[Unit]", "Description=LumiBot paper fund data desk (news, prices, macro, earnings)",
        "Wants=network-online.target", "After=network-online.target", "StartLimitIntervalSec=0", "",
        "[Service]", "Type=exec", f"User={user}", f"WorkingDirectory={unit_path(repo)}",
        "ExecStart=" + " ".join(unit_quote(arg) for arg in desk_args),
        "Restart=always", "RestartSec=30", "KillSignal=SIGINT", "TimeoutStopSec=30",
        # The trading process comes first: less CPU and I/O, and the first to go under memory pressure.
        "Nice=10", "CPUWeight=20", "IOWeight=20", "OOMScoreAdjust=500",
        "Environment=PYTHONUNBUFFERED=1 LUMIBOT_DISABLE_DOTENV=true",
        "UMask=0077", "NoNewPrivileges=true", "PrivateTmp=true", "ProtectSystem=strict", "ProtectHome=read-only",
        "ReadWritePaths=" + " ".join(unit_quote(p) for p in sorted({repo / "artifacts", run_dir or repo / "artifacts"})),
        "StandardOutput=journal", "StandardError=journal", "",
        "[Install]", "WantedBy=multi-user.target", ""])
    units["lumibot-fund-backup.timer"] = "\n".join([
        "[Unit]", "Description=Daily LumiBot paper fund journal backup", "",
        "[Timer]", "OnCalendar=*-*-* 17:45:00 America/New_York", "Persistent=true", "RandomizedDelaySec=300", "",
        "[Install]", "WantedBy=timers.target", ""])
    return units


def backup(root, destination):
    """Consistent online SQLite snapshot; preserve stop state, exclude all keys."""
    root, destination = Path(root), Path(destination)
    if destination.exists():
        raise RiskError("Backup destination must be new; existing backups are never overwritten")
    if not (root / "state.sqlite3").is_file():
        raise RiskError("No account journal to back up")
    destination.mkdir(parents=True, mode=0o700)
    with sqlite3.connect((root / "state.sqlite3").resolve().as_uri() + "?mode=ro", uri=True) as source:
        with sqlite3.connect(destination / "state.sqlite3") as target:
            source.backup(target)
    os.chmod(destination / "state.sqlite3", 0o600)
    # Always restore into STOP, even if the source account was trading.
    from . import stops
    stops.write(destination, "operator", "restored_backup")


SANDBOX_KEYS = ("WorkingDirectory", "Environment", "UMask", "NoNewPrivileges", "PrivateTmp", "ProtectSystem",
                "ProtectHome", "ReadWritePaths")


def sandbox_properties(repo, run_dir, env_file, user):
    """The runner unit's filesystem and privilege settings, for running a one-off command identically."""
    unit = generate(repo, run_dir, env_file, user, None)["lumibot-fund-runner.service"]
    return [line for line in unit.splitlines() if line.split("=", 1)[0] in SANDBOX_KEYS]


BACKUP_NAME = re.compile(r"\d{8}-\d{6}")
# Free space a backup must leave: above the monitor's 200 MB low-disk STOP, with room to keep trading.
BACKUP_RESERVE_BYTES = 512_000_000


class BackupSpaceError(RiskError):
    pass


def _snapshots(backup_dir):
    return sorted(p for p in backup_dir.iterdir()
                  if p.is_dir() and BACKUP_NAME.fullmatch(p.name) and (p / "state.sqlite3").is_file())


def _remove(snapshot):
    for child in snapshot.iterdir():
        child.unlink()
    snapshot.rmdir()


def rotate(root, backup_dir, keep=14, now=None):
    """A timestamped snapshot under backup_dir, keeping the newest `keep`; returns the new directory.

    Only directories this function created (timestamp names with a journal) are
    ever pruned. Off-server copies are the operator's choice (see the guide).
    """
    from datetime import datetime, timezone
    if type(keep) is not int or not 1 <= keep <= 365:
        raise RiskError("keep must be 1..365 snapshots")
    import shutil
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    book = Path(root) / "state.sqlite3"
    needed = sum(path.stat().st_size for path in (book, Path(str(book) + "-wal")) if path.is_file())
    needed += BACKUP_RESERVE_BYTES
    free = shutil.disk_usage(backup_dir).free
    if free < needed:
        # A full copy must not fill the disk: that would stop trading (the monitor's low-disk STOP).
        # The oldest snapshots this rotation would delete anyway go first, but only when that
        # makes room; a skipped backup never costs an existing one.
        existing = _snapshots(backup_dir)
        doomed = existing[:max(0, len(existing) - (keep - 1))]
        reclaimed = sum(path.stat().st_size for old in doomed for path in old.iterdir() if path.is_file())
        if free + reclaimed < needed:
            raise BackupSpaceError(f"Backup skipped: it needs {needed} bytes free (journal plus a "
                                   f"{BACKUP_RESERVE_BYTES}-byte reserve), {free} are free")
        for old in doomed:
            _remove(old)
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M%S")
    target = backup_dir / stamp
    backup(root, target)
    for old in _snapshots(backup_dir)[:-keep]:
        _remove(old)
    return target


def _notify_backup_skipped(run_dir, reason):
    from datetime import datetime, timezone
    from .journal import encode
    book = Path(run_dir) / "state.sqlite3"
    if book.is_file():
        with sqlite3.connect(book, timeout=10) as db:
            db.execute("INSERT INTO events(at,kind,body) VALUES(?,?,?)", (datetime.now(timezone.utc).isoformat(),
                       "notification", encode({"kind": "backup_skipped", "details": {"reason": reason}})))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    setup = sub.add_parser("prepare")
    setup.add_argument("--user", required=True)
    setup.add_argument("--mode", choices=["local", "paper"], help="Fix the mode in the units; default: .env FUND_MODE")
    setup.add_argument("--run-dir", type=Path, help="Fix the account directory; default: .env FUND_RUN_DIR or artifacts/fund-<mode>")
    setup.add_argument("--env-file", type=Path, default=Path(".env"))
    setup.add_argument("--output-dir", type=Path, default=Path("artifacts/fund-systemd"))
    only = sub.add_parser("units", help="Write the unit files only: no STOP, no .env (used by fund-start.sh)")
    only.add_argument("--user", required=True)
    only.add_argument("--env-file", type=Path, default=Path(".env"))
    only.add_argument("--output-dir", type=Path, default=Path("artifacts/fund-systemd"))
    copy = sub.add_parser("backup")
    copy.add_argument("--run-dir", type=Path, required=True)
    copy.add_argument("--output-dir", type=Path, required=True)
    jail = sub.add_parser("sandbox", help="Print the runner's sandbox properties (used by fund-rehearse.sh)")
    jail.add_argument("--user", required=True)
    jail.add_argument("--env-file", type=Path, default=Path(".env"))
    daily = sub.add_parser("rotate-backup", help="Timestamped snapshot of the configured account, keeping the newest N")
    daily.add_argument("--env-file", type=Path, default=Path(".env"))
    daily.add_argument("--run-dir", type=Path)
    daily.add_argument("--backup-dir", type=Path, default=Path("artifacts/fund-backups"))
    daily.add_argument("--keep", type=int, default=14)
    args = parser.parse_args(argv)
    if args.action == "backup":
        backup(args.run_dir, args.output_dir)
        print("Account snapshot saved in STOP. Secrets excluded; verify configuration before restoring.")
        return
    if args.action == "rotate-backup":
        from dotenv import dotenv_values
        from .settings import startup
        values = dotenv_values(args.env_file) if args.env_file.is_file() else {}
        _, run_dir, _, _ = startup("start", args.run_dir, {**values, **os.environ})
        try:
            target = rotate(run_dir, args.backup_dir, args.keep)
        except BackupSpaceError as exc:
            try:
                _notify_backup_skipped(run_dir, str(exc))  # The monitor sends it; a failed timer alone is silent.
            except Exception:
                pass  # The skipped backup itself is still reported by the failing service.
            raise
        print(f"Journal snapshot {target} (restores into STOP; secrets excluded).")
        return
    if os.name != "posix":
        raise RiskError("Generate service paths on the target Linux host, not Windows")
    args.env_file = args.env_file.resolve()
    repo = Path(__file__).resolve().parents[2]
    if args.action == "sandbox":
        print("\n".join(sandbox_properties(repo, None, args.env_file, args.user)))
        return
    if args.action == "units":
        # Regenerating the units never changes trading state.
        args.output_dir.mkdir(parents=True, exist_ok=True)
        for filename, body in generate(repo, None, args.env_file, args.user, None).items():
            (args.output_dir / filename).write_text(body, encoding="utf-8")
            print(args.output_dir / filename)
        return
    units = generate(repo, args.run_dir.resolve() if args.run_dir else None, args.env_file, args.user, args.mode)
    args.output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    (repo / "artifacts").mkdir(exist_ok=True)
    if not args.env_file.exists():
        args.env_file.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive creation: a simultaneous operator write must never be lost.
        with args.env_file.open("x", encoding="utf-8") as handle:
            os.chmod(args.env_file, 0o600)
            handle.write(Path(__file__).with_name(".env.example").read_text(encoding="utf-8"))
    # Prepare the account the services will start with now (parsed, never executed).
    from dotenv import dotenv_values
    from .settings import startup
    mode, run_dir, _, _ = startup(args.mode or "start", args.run_dir, {**dotenv_values(args.env_file), **os.environ})
    run_dir = run_dir if run_dir.is_absolute() else repo / run_dir
    if not args.run_dir and repo / "artifacts" not in run_dir.resolve().parents:
        raise RiskError(f"FUND_RUN_DIR for server services must be under {repo / 'artifacts'}: the service may only write there")
    run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    from . import stops
    stops.write(run_dir, "operator", "first_install")  # The first start is verified and resumed by hand.
    print(f"Services start in mode {mode} with account directory {run_dir}"
          + ("" if args.mode else "; change FUND_MODE in .env and restart both services to switch."))
    # Files created here belong to the user running prepare; the services run as --user.
    print("Give the service user its files (the account directory, its STOP and lock files, and .env), then "
          "run fund commands as that user:")
    print(f'  sudo chown -R "{args.user}" {repo / "artifacts"} {args.env_file}')
    print(f"  sudo chmod 600 {args.env_file}")
    print(f'  sudo -u "{args.user}" bash scripts/run_autonomous_fund.sh show-config')
    for filename, body in units.items():
        (args.output_dir / filename).write_text(body, encoding="utf-8")
    print("Prepared STOP, secret template if absent, two service files and the daily backup timer. "
          "Review and install explicitly; nothing was started.")


if __name__ == "__main__":
    try:
        main()
    except RiskError as exc:
        raise SystemExit(str(exc)) from None
