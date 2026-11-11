"""API Key 管理命令。

完整 key 只在 create 时打印一次，库里只存前缀和 sha256；吊销后立即失效。

    uv run python -m app.tools.api_keys create --name partner-a --expires-in-days 365
    uv run python -m app.tools.api_keys list
    uv run python -m app.tools.api_keys revoke --prefix kt_a1b2c3d4
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from app.db import session_scope
from app.services.api_keys import create_key, revoke_key


def _format(value) -> str:
    return "-" if value is None else str(value)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="api_keys", description="管理对外接口 API Key")
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create", help="生成一个新 key，明文只打印一次")
    create.add_argument("--name", required=True, help="调用方名称")
    create.add_argument("--expires-in-days", type=int, default=None, help="有效天数，默认长期")

    sub.add_parser("list", help="列出所有 key（只显示前缀）")

    revoke = sub.add_parser("revoke", help="按前缀吊销 key")
    revoke.add_argument("--prefix", required=True, help="key 前缀，例如 kt_a1b2c3d4")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    with session_scope() as session:
        if args.command == "create":
            api_key, token = create_key(
                session,
                name=args.name,
                expires_in_days=args.expires_in_days,
            )
            print(f"id={api_key.id} name={api_key.name} prefix={api_key.key_prefix}")
            print(f"API_KEY={token}")
            print("只显示这一次，请立即保存；库里只有哈希。")
            return 0

        if args.command == "list":
            from app import crud

            rows = crud.list_api_keys(session)
            if not rows:
                print("还没有 API Key")
                return 0
            print(
                f"{'id':<6}{'name':<20}{'prefix':<14}{'enabled':<8}{'expires_at':<22}last_used_at"
            )
            for row in rows:
                print(
                    f"{_format(row.id):<6}{row.name:<20}{row.key_prefix:<14}"
                    f"{str(row.enabled):<8}{_format(row.expires_at):<22}{_format(row.last_used_at)}"
                )
            return 0

        api_key = revoke_key(session, prefix=args.prefix)
        if api_key is None:
            print(f"没有找到前缀为 {args.prefix} 的 API Key")
            return 1
        print(f"已吊销 id={api_key.id} name={api_key.name} prefix={api_key.key_prefix}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
