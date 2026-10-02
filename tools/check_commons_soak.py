"""Bounded local Commons durability exercise. No model or external service calls.

Tests authenticated concurrent HTTP use, cursor continuation, replay and process
restart against disposable data. This is not production or cross-machine load.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import platform
import sqlite3
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx
from tests.live_server import LiveServer


def source_hash():
    digest = hashlib.sha256()
    paths = [p for p in (ROOT / "agent_world").rglob("*")
             if p.suffix in {".py", ".js", ".html", ".css"}]
    paths += [Path(__file__), ROOT / "tests/live_server.py"]
    for path in sorted(paths):
        digest.update(str(path.relative_to(ROOT)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def run(duration, interval, restart_interval, pairs):
    report = {"status": "running", "scope": "local-loopback HTTP deterministic clients",
              "python": platform.python_version(), "platform": platform.platform(),
              "requested_seconds": duration, "pairs": pairs, "rounds": 0,
              "messages": 0, "replays": 0, "restarts": 0,
              "source_sha256_before": source_hash(), "samples": []}
    started = time.monotonic()
    workload_started = None
    try:
        with LiveServer("commons") as server:
            participants = [server.key_identity() for _ in range(pairs * 2)]
            headers = [{"Authorization": "Bearer " + item[1]["token"]} for item in participants]
            clients = [httpx.Client(trust_env=False, timeout=10) for _ in range(pairs)]
            cursors = [0] * (pairs * 2)
            last_operations = [None] * pairs

            def invoke(pair, actor, function, arguments, operation=None):
                body = {"arguments": arguments}
                if operation is not None:
                    body["operation_id"] = operation
                response = clients[pair].post(server.url + "/v1/functions/" + function + "/invoke",
                                              headers=headers[actor], json=body)
                response.raise_for_status()
                return response.json()

            try:
                for pair in range(pairs):
                    invoke(pair, pair * 2, "commons.conversation.open",
                           {"conversation_id": f"c{pair}", "peer_role_id": participants[pair * 2 + 1][0]["role_id"]},
                           f"open-{pair}")
                    for actor in (pair * 2, pair * 2 + 1):
                        response = clients[pair].get(server.url + "/v1/changes", headers=headers[actor])
                        response.raise_for_status()
                        cursors[actor] = response.json()["next_cursor"]

                def cycle(pair, number):
                    actor = pair * 2 + number % 2
                    recipient = pair * 2 + (1 - number % 2)
                    message = f"m{pair}-{number}"
                    op = f"send-{pair}-{number}"
                    args = {"conversation_id": f"c{pair}", "message_id": message, "text": message}
                    result = invoke(pair, actor, "commons.message.send", args, op)
                    assert not result.get("replayed"), result
                    replayed = 0
                    if number % 7 == 0:
                        replay = invoke(pair, actor, "commons.message.send", args, op)
                        assert replay["replayed"], replay
                        replayed = 1
                    response = clients[pair].get(server.url + "/v1/changes", headers=headers[recipient],
                                                params={"after": cursors[recipient]})
                    response.raise_for_status()
                    page = response.json()
                    assert [e["payload"]["message_id"] for e in page["events"]] == [message], page
                    cursors[recipient] = page["next_cursor"]
                    last_operations[pair] = (actor, args, op)
                    return replayed

                workload_started = time.monotonic()
                last_restart = workload_started
                last_sample = 0
                with ThreadPoolExecutor(max_workers=pairs) as pool:
                    while time.monotonic() - workload_started < duration:
                        began = time.monotonic()
                        report["replays"] += sum(pool.map(lambda p: cycle(p, report["rounds"]), range(pairs)))
                        report["rounds"] += 1
                        report["messages"] += pairs
                        if time.monotonic() - last_restart >= restart_interval:
                            server.restart(abrupt=True)
                            for pair, (actor, args, op) in enumerate(last_operations):
                                assert invoke(pair, actor, "commons.message.send", args, op)["replayed"]
                                report["replays"] += 1
                            report["restarts"] += 1
                            last_restart = time.monotonic()
                        elapsed = time.monotonic() - workload_started
                        if elapsed - last_sample >= min(30, duration):
                            rss = None
                            proc = Path(f"/proc/{server.proc.pid}/status")
                            if proc.exists():
                                rss = next((line.split(":", 1)[1].strip() for line in proc.read_text().splitlines()
                                            if line.startswith("VmRSS:")), None)
                            report["samples"].append({"elapsed_seconds": round(elapsed, 2), "rounds": report["rounds"],
                                                      "server_rss": rss, "db_bytes": server.db.stat().st_size})
                            print(json.dumps(report["samples"][-1]), flush=True)
                            last_sample = elapsed
                        time.sleep(max(0, interval - (time.monotonic() - began)))
                assert report["rounds"] > 0, "no completed workload rounds"
                # An independent on-disk acceptance oracle catches silent duplicates/loss.
                with sqlite3.connect(server.db) as db:
                    report["integrity_check"] = db.execute("PRAGMA integrity_check").fetchone()[0]
                    report["stored_messages"] = db.execute(
                        "SELECT count(*) FROM world_state WHERE scope LIKE 'commons:messages:%'").fetchone()[0]
                assert report["integrity_check"] == "ok"
                assert report["stored_messages"] == report["messages"]
                # Each receiver's cursor must have no duplicate notifications left.
                for actor in range(pairs * 2):
                    response = clients[actor // 2].get(server.url + "/v1/changes", headers=headers[actor],
                                                       params={"after": cursors[actor]})
                    response.raise_for_status()
                    assert response.json()["events"] == []
                report["status"] = "passed"
            finally:
                for client in clients:
                    client.close()
    except BaseException as exc:
        report["status"] = "failed"
        report["error"] = f"{type(exc).__name__}: {exc}"
    report["elapsed_seconds"] = round(time.monotonic() - started, 2)
    report["workload_elapsed_seconds"] = (
        round(time.monotonic() - workload_started, 2) if workload_started is not None else 0
    )
    report["source_sha256_after"] = source_hash()
    report["source_unchanged"] = report["source_sha256_before"] == report["source_sha256_after"]
    if not report["source_unchanged"]:
        report["status"] = "failed"
        report.setdefault("error", "source changed during run; acceptance evidence invalid")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=float, default=60)
    parser.add_argument("--interval", type=float, default=1)
    parser.add_argument("--restart-interval", type=float, default=300)
    parser.add_argument("--pairs", type=int, default=4)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not (1 <= args.duration <= 86400 and .1 <= args.interval <= 60 and
            1 <= args.restart_interval <= 86400 and 1 <= args.pairs <= 16):
        parser.error("duration/restart must be 1..86400s, interval .1..60s, pairs 1..16")
    report = run(args.duration, args.interval, args.restart_interval, args.pairs)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k != "samples"}), flush=True)
    raise SystemExit(0 if report["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
