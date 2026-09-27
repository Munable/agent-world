from __future__ import annotations

"""Subprocess driver for the cross-world identity experiment."""

import argparse
import json
import sys

from agent_world.errors import InvalidIdentityToken
from experiments.cross_world_identity import IdentityProofDeployment


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["challenge", "authenticate", "resolve"])
    parser.add_argument("--deployment", required=True)
    parser.add_argument("--universe", required=True)
    parser.add_argument("--runtime-db", required=True)
    parser.add_argument("--identity-db", required=True)
    args = parser.parse_args()

    payload = json.loads(sys.stdin.read() or "{}")
    deployment = IdentityProofDeployment(
        deployment_id=args.deployment,
        universe=args.universe,
        runtime_db=args.runtime_db,
        identity_db=args.identity_db,
    )

    if args.command == "challenge":
        result = deployment.issue_challenge(
            ttl_seconds=float(payload.get("ttl_seconds", 60)),
        )
    elif args.command == "authenticate":
        result = deployment.authenticate(
            payload["proof"],
            display_name=payload["display_name"],
            token_ttl_seconds=float(payload.get("token_ttl_seconds", 600)),
        )
    else:
        try:
            identity = deployment.runtime.resolve_identity_token(payload["token"])
        except InvalidIdentityToken:
            result = {"valid": False, "error": "InvalidIdentityToken"}
        else:
            result = {"valid": True, "identity": identity}

    sys.stdout.write(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
