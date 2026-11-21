"""Operator-initiated OAuth for the fund's read-only Schwab market-data token."""
import argparse
import os
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from filelock import FileLock, Timeout

from .environment import load_environment
from .risk import RiskError
from .schwab import market_credentials, market_token_expiry, market_token_lock


def authorize_market_token(environ, *, authorize=None):
    key, secret, configured_path = market_credentials(environ)
    callback = str(environ.get("SCHWAB_BACKEND_CALLBACK_URL") or "").strip()
    if not all((key, secret, configured_path, callback)):
        raise RiskError("请填齐 SCHWAB_APP_KEY、SCHWAB_APP_SECRET、SCHWAB_TOKEN_PATH 和 SCHWAB_BACKEND_CALLBACK_URL。")
    if not callback.startswith("https://"):
        raise RiskError("请填写嘉信 App 登记的 HTTPS Callback URL。")
    target = Path(configured_path).expanduser()
    if not target.is_absolute() or target.is_dir():
        raise RiskError("SCHWAB_TOKEN_PATH 必须是令牌文件的绝对路径。")
    target = target.resolve()
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if authorize is None:
        from schwab.auth import client_from_manual_flow
        authorize = client_from_manual_flow

    # Keep the existing token usable during the browser wait. Put the new token
    # on the same filesystem and publish it only after a complete authorization.
    try:
        with FileLock(str(target) + ".authorize.lock", timeout=0):
            with TemporaryDirectory(prefix=".schwab-auth-", dir=target.parent) as directory:
                staged = Path(directory) / "token.json"
                authorize(key, secret, callback, str(staged))
                if market_token_expiry(staged, datetime.now(timezone.utc)) is None:
                    raise RiskError("嘉信授权未返回可用的刷新令牌。请用新的授权链接重试。")
                staged.chmod(0o600)
                with market_token_lock(target):
                    staged.replace(target)
    except Timeout:
        raise RiskError("已有嘉信授权或行情请求正在处理令牌。请稍后重试。") from None
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        load_environment(args.env_file)
        target = authorize_market_token(os.environ)
    except KeyboardInterrupt:
        return 130
    except RiskError as error:
        parser.exit(1, f"{error}\n")
    except OSError as error:
        parser.exit(1, f"无法保存嘉信令牌（{type(error).__name__}）。请检查令牌目录的权限。\n")
    except Exception as error:
        # OAuth exceptions can carry request or response data. Print only their type.
        parser.exit(1, f"嘉信授权失败（{type(error).__name__}）。请重跑脚本并立即粘贴本次的回调地址。\n")
    print(f"嘉信行情令牌已更新 {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
