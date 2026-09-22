from __future__ import annotations

import argparse
import json
import pathlib

from .runtime_core import WorldRuntime


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Local admin for Agent World identity tokens.")
    sub = p.add_subparsers(dest="command", required=True)

    issue = sub.add_parser("issue")
    issue.add_argument("--db", required=True)
    issue.add_argument("--universe", required=True)
    issue.add_argument("--role", required=True)
    issue.add_argument("--ttl", type=float, default=None)

    revoke = sub.add_parser("revoke")
    revoke.add_argument("--db", required=True)
    revoke.add_argument("--token-id", required=True)

    return p


def main() -> int:
    args = parser().parse_args()
    runtime = WorldRuntime(pathlib.Path(args.db))

    if args.command == "issue":
        result = runtime.issue_identity_token(
            args.universe,
            args.role,
            ttl_seconds=args.ttl,
        )
        print(
            json.dumps(
                {
                    **result,
                    "warning": "The token is shown once here. Store it as a secret.",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if args.command == "revoke":
        changed = runtime.revoke_identity_token(args.token_id)
        print(json.dumps({"revoked": changed, "token_id": args.token_id}))
        return 0 if changed else 2

    raise AssertionError("unreachable")


if __name__ == "__main__":
    raise SystemExit(main())
