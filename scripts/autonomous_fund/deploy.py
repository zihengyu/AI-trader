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


def generate(repo, run_dir, env_file, user, mode):
    if mode not in {"local", "paper"} or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", user):
        raise RiskError("Choose local/paper and an existing non-root Linux service user")
    if user == "root":
        raise RiskError("Run trading services under a dedicated non-root user")
    repo, run_dir, env_file = (PurePosixPath(str(value)) for value in (repo, run_dir, env_file))
    if not all(value.is_absolute() for value in (repo, run_dir, env_file)):
        raise RiskError("Service paths must be absolute Linux paths")
    python = repo / ".venv" / "bin" / "python"
    units = {}
    for role in ("runner", "monitor"):
        args = [python, "-m", "scripts.autonomous_fund.service", role, "--mode", mode,
                "--run-dir", run_dir, "--env-file", env_file]
        units[f"lumibot-fund-{role}.service"] = "\n".join([
            "[Unit]", f"Description=LumiBot paper fund {role}",
            "Wants=network-online.target", "After=network-online.target", "StartLimitIntervalSec=0", "",
            "[Service]", "Type=exec", f"User={user}", f"WorkingDirectory={unit_quote(repo)}",
            "ExecStart=" + " ".join(unit_quote(arg) for arg in args),
            "Restart=always", "RestartSec=30", "KillSignal=SIGINT", "KillMode=mixed", "TimeoutStopSec=45",
            "Environment=PYTHONUNBUFFERED=1 LUMIBOT_DISABLE_DOTENV=true",
            "UMask=0077", "NoNewPrivileges=true", "PrivateTmp=true", "ProtectSystem=strict", "ProtectHome=read-only",
            "ReadWritePaths=" + " ".join(unit_quote(p) for p in sorted({repo / "artifacts", run_dir})),
            "StandardOutput=journal", "StandardError=journal", "",
            "[Install]", "WantedBy=multi-user.target", ""])
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
    (destination / "STOP").touch(mode=0o600)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    setup = sub.add_parser("prepare")
    setup.add_argument("--user", required=True)
    setup.add_argument("--mode", choices=["local", "paper"], default="local")
    setup.add_argument("--run-dir", type=Path, default=Path("artifacts/fund-local"))
    setup.add_argument("--env-file", type=Path, default=Path(".env"))
    setup.add_argument("--output-dir", type=Path, default=Path("artifacts/fund-systemd"))
    copy = sub.add_parser("backup")
    copy.add_argument("--run-dir", type=Path, required=True)
    copy.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.action == "backup":
        backup(args.run_dir, args.output_dir)
        print("Account snapshot saved in STOP. Secrets excluded; verify configuration before restoring.")
        return
    if os.name != "posix":
        raise RiskError("Generate service paths on the target Linux host, not Windows")
    args.run_dir = args.run_dir.resolve()
    args.env_file = args.env_file.resolve()
    repo = Path(__file__).resolve().parents[2]
    units = generate(repo, args.run_dir, args.env_file, args.user, args.mode)
    args.output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    args.run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    (repo / "artifacts").mkdir(exist_ok=True)
    (args.run_dir / "STOP").touch(mode=0o600)
    if not args.env_file.exists():
        args.env_file.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive creation: a simultaneous operator write must never be lost.
        with args.env_file.open("x", encoding="utf-8") as handle:
            os.chmod(args.env_file, 0o600)
            handle.write(Path(__file__).with_name(".env.example").read_text(encoding="utf-8"))
    for filename, body in units.items():
        (args.output_dir / filename).write_text(body, encoding="utf-8")
    print("Prepared STOP, secret template if absent, and two service files. Review and install explicitly; nothing was started.")


if __name__ == "__main__":
    try:
        main()
    except RiskError as exc:
        raise SystemExit(str(exc)) from None
