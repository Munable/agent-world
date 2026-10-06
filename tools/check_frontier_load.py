"""Finite loopback Frontier transport load, using a new DevDisk database only.

No models, production service, browser automation, or existing databases. The
browser workload reproduces look/history HTTP reads; external tools initialize
real MCP sessions and call tools. This measures this fixture, not human play or
MMO capacity. Keep the resulting report and database until evidence is reviewed.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import socket
import sqlite3
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from agent_world.connect import load_identity
from examples.frontier_app import UNIVERSE, initialize_local
from examples.frontier_world import CHARACTERS, HISTORY, REQUESTS, SCENES, WORLD_ID

DEV_DISK = Path("/Volumes/DevDisk")
PROJECT_DATA = DEV_DISK / "DeveloperData/agent-world"


class LoadFailure(RuntimeError):
    """Credential-free failure code; response bodies are never logged."""

    def __init__(self, code, status=None):
        self.code, self.status = code, status
        super().__init__(code)


def source_hash():
    digest = hashlib.sha256()
    paths = [p for folder in (ROOT / "agent_world", ROOT / "examples")
             for p in folder.rglob("*") if p.suffix in {".py", ".js", ".html", ".css"}]
    paths.append(Path(__file__))
    for path in sorted(paths):
        digest.update(str(path.relative_to(ROOT)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def destination(path):
    path = path.expanduser().absolute()
    if (".." in path.parts or not path.is_relative_to(PROJECT_DATA)
            or any(p.is_symlink() for p in (path, *path.parents))):
        raise ValueError("Paths must be real paths inside DevDisk DeveloperData/agent-world.")
    return path


class OwnedServer:
    """Own exactly one newly provisioned process; never attach to a service."""

    def __init__(self, db, log_path, port):
        self.db, self.log_path, self.port = db, log_path, port
        self.proc, self.log = None, None

    def start(self):
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", self.port))
            self.port = reservation.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.log = self.log_path.open("x", encoding="utf-8")
        self.proc = subprocess.Popen(
            [sys.executable, "-u", "-m", "examples.frontier_app", "--db", str(self.db),
             "--port", str(self.port)], cwd=ROOT, stdout=self.log, stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 60
        with httpx.Client(trust_env=False, timeout=.5, follow_redirects=False) as client:
            while time.monotonic() < deadline:
                if self.proc.poll() is not None:
                    raise LoadFailure("owned_server_exited_during_startup")
                try:
                    response = client.get(self.url + "/health")
                    if response.status_code == 200 and response.json().get("universe") == UNIVERSE:
                        return
                except (httpx.HTTPError, ValueError):
                    pass
                time.sleep(.05)
        raise LoadFailure("owned_server_startup_timeout")

    def stop(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        if self.log is not None:
            self.log.close()


async def http_json(client, method, path, **kwargs):
    response = await client.request(method, path, **kwargs)
    if response.status_code != 200:
        raise LoadFailure("unexpected_http_status", response.status_code)
    try:
        value = response.json()
    except ValueError:
        raise LoadFailure("invalid_http_json", response.status_code) from None
    if not isinstance(value, dict):
        raise LoadFailure("invalid_http_shape", response.status_code)
    return value, response.status_code, len(response.content)


async def http_function(client, function, arguments=None, operation=None):
    body = {"arguments": arguments or {}}
    if operation is not None:
        body["operation_id"] = operation
    result, status, size = await http_json(
        client, "POST", "/v1/functions/" + function + "/invoke", json=body,
    )
    if result.get("ok") is not True or not isinstance(result.get("result"), dict):
        raise LoadFailure("invalid_function_result", status)
    return result, status, size


async def exchange(client, key):
    challenge, _, _ = await http_json(client, "POST", "/v1/key-identities/challenges",
                                      json={"public_key": key.public_key, "access_mode": "control"})
    message = base64.urlsafe_b64decode(challenge["message"] + "=" * (-len(challenge["message"]) % 4))
    signature = base64.urlsafe_b64encode(key.private.sign(message)).rstrip(b"=").decode("ascii")
    entered, _, _ = await http_json(client, "POST", "/v1/key-identities/exchange",
                                   json={"challenge_id": challenge["challenge_id"], "signature": signature})
    if entered.get("public_key") != key.public_key or entered["identity"]["universe"] != UNIVERSE:
        raise LoadFailure("identity_exchange_mismatch")
    return entered["identity"]


async def prepare(server, setup, count, seed_scenes, seed_history):
    participants = []
    async with httpx.AsyncClient(base_url=server.url, trust_env=False, timeout=10) as public:
        for index in range(count):
            if index < 4:
                key_path = Path(setup["participants"]["ABCD"[index]]["key_path"])
                key = load_identity(key_path)
            else:
                key_path = server.db.parent.parent / "private" / f"participant-{index:02d}.json"
                key = load_identity(key_path, new_identity=True)
            browser, external = await exchange(public, key), await exchange(public, key)
            if browser["role_id"] != external["role_id"] or browser["token_id"] == external["token_id"]:
                raise LoadFailure("same_key_credentials_mismatch")
            participant = {"index": index, "role_id": browser["role_id"],
                           "browser_token": browser["token"], "external_token": external["token"]}
            participants.append(participant)
            async with httpx.AsyncClient(base_url=server.url, trust_env=False, timeout=10,
                    headers={"Authorization": "Bearer " + browser["token"]}) as http:
                await http_function(http, "frontier.join", {"name": f"Load visitor {index:02d}",
                                    "class_id": "fighter" if index % 2 == 0 else "rogue"}, f"seed-join-{index}")

    async with httpx.AsyncClient(base_url=server.url, trust_env=False, timeout=10,
            headers={"Authorization": "Bearer " + participants[0]["browser_token"]}) as http:
        seen = (await http_function(http, "frontier.look"))[0]["result"]
        if seed_scenes > 3:
            await http_function(http, "frontier.move", {"destination_id": "bell-market",
                                "expected_character_version": seen["character"]["version"]}, "seed-move-market")
        for number in range(3, seed_scenes):
            seen = (await http_function(http, "frontier.look"))[0]["result"]
            exit_ = next(e for e in seen["scene"]["exits"] if e["destination_id"] is None and not e["blocked"])
            package = {"title": f"Load fixture waypoint {number:03d}",
                       "description": "A fixed transport fixture waypoint beside a marked path.",
                       "facts": ["This place is prebuilt for a bounded local load experiment."],
                       "npcs": [{"name": "Waypoint keeper", "description": "A stationary fixture character."}],
                       "objects": [
                           {"name": "Waypoint token", "description": "A portable fixture object.", "kind": "cache",
                            "initial_state": "open", "discovery_text": "One token at this waypoint."},
                           {"name": "Waypoint note", "description": "An openly readable fixture note.", "kind": "inscription",
                            "initial_state": "read", "discovery_text": "Continue along the marked fixture path."}],
                       "exits": [{"label": "Next fixture waypoint", "description": "An empty continuation for provisioning."}]}
            created = (await http_function(http, "frontier.expand", {
                "scene_id": seen["scene"]["id"], "exit_id": exit_["id"],
                "expected_exit_version": exit_["version"], "package": package}, f"seed-expand-{number}"))[0]["result"]
            seen = (await http_function(http, "frontier.look"))[0]["result"]
            await http_function(http, "frontier.move", {"destination_id": created["created_scene"]["id"],
                                "expected_character_version": seen["character"]["version"]}, f"seed-move-{number}")
        for number in range(seed_history):
            await http_function(http, "frontier.say", {"text": f"Fixture history entry {number:04d}."}, f"seed-history-{number}")
        baseline = (await http_function(http, "frontier.look"))[0]["result"]["history_latest_seq"]
    return participants, baseline


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(len(ordered) * fraction) - 1)], 6)


class Recorder:
    def __init__(self, label, count, duration, warmup):
        self.label, self.count, self.duration = label, count, duration
        self.created = time.monotonic()
        self.begin, self.end = self.created + warmup, self.created + warmup + duration
        self.records, self.errors, self.rss = [], [], []
        self.stop = asyncio.Event()
        self.mcp_http_statuses = Counter()
        self.missed_ticks = Counter()

    def fail(self, endpoint, user, exc):
        code = exc.code if isinstance(exc, LoadFailure) else type(exc).__name__
        self.errors.append({"endpoint": endpoint, "user": user, "code": code,
                            "status": getattr(exc, "status", None)})
        self.stop.set()

    async def measure(self, endpoint, kind, user, action, *, scheduled=None, wait_timeout=0):
        began = time.monotonic()
        result, status, size = None, "exception", 0
        failure = None
        try:
            result, status, size = await action()
            return result
        except Exception as exc:
            failure = exc
            self.fail(endpoint, user, exc)
            status = getattr(exc, "status", None) or type(exc).__name__
            raise
        finally:
            elapsed = time.monotonic() - began
            if self.begin <= began < self.end:
                lag = max(0, began - scheduled) if scheduled is not None else 0
                # An empty successful 30s wait is expected to take 30s.
                budget = max(0, elapsed - wait_timeout) if kind == "wait" else elapsed + lag
                self.records.append({"endpoint": endpoint, "kind": kind, "user": user,
                    "started_after_seconds": round(began - self.begin, 6), "latency_seconds": round(elapsed, 6),
                    "scheduled_lag_seconds": round(lag, 6), "budget_latency_seconds": round(budget, 6),
                    "requested_wait_seconds": round(wait_timeout, 6), "status": status,
                    "response_bytes": size, "failed": failure is not None})

    def finish(self):
        groups = {}
        for row in self.records:
            groups.setdefault(row["endpoint"], []).append(row)
        failures = ["unexpected_request_error"] if self.errors else []
        metrics = {}
        for endpoint, rows in sorted(groups.items()):
            values = [r["latency_seconds"] for r in rows]
            budgets = [r["budget_latency_seconds"] for r in rows]
            limit = 2 if rows[0]["kind"] == "write" else 1
            p95, p99 = percentile(budgets, .95), percentile(budgets, .99)
            if p95 > limit:
                failures.append(endpoint + "_p95_over_budget")
            if p99 > 5:
                failures.append(endpoint + "_p99_over_5s")
            completed = sum(not r["failed"] for r in rows)
            metrics[endpoint] = {"count": len(rows), "successful_count": completed,
                "completed_per_second": round(completed / self.duration, 6),
                "status_counts": dict(Counter(str(r["status"]) for r in rows)),
                "p50_seconds": percentile(values, .5), "p95_seconds": percentile(values, .95),
                "p99_seconds": percentile(values, .99), "budget_p95_seconds": p95, "budget_p99_seconds": p99,
                "max_scheduled_lag_seconds": max(r["scheduled_lag_seconds"] for r in rows),
                "response_bytes": sum(r["response_bytes"] for r in rows)}
        readers = {r["user"] for r in self.records if r["endpoint"] == "HTTP.frontier.look"}
        writers = {r["user"] for r in self.records if r["endpoint"] == "MCP.frontier.say"}
        if readers != set(range(self.count)):
            failures.append("not_every_user_has_measured_browser_read")
        if writers != set(range(self.count)):
            failures.append("not_every_user_has_measured_mcp_write")
        return {"name": self.label, "users": self.count, "requested_seconds": self.duration,
                "elapsed_seconds_including_warmup_and_drain": round(time.monotonic() - self.created, 3),
                "status": "failed" if failures else "passed", "failures": failures,
                "metrics": metrics, "unexpected_errors": self.errors, "server_rss_samples": self.rss,
                "rss_observation_available": any(sample["rss_kib"] is not None for sample in self.rss),
                "measured_browser_users": len(readers), "measured_external_users": len(writers),
                "mcp_http_status_counts": dict(self.mcp_http_statuses), "missed_scheduled_ticks": dict(self.missed_ticks),
                "requests": self.records}


async def periodic(recorder, interval, offset, name, cycle):
    due, number = recorder.begin + offset, 0
    while due < recorder.end and not recorder.stop.is_set():
        await asyncio.sleep(max(0, due - time.monotonic()))
        if recorder.stop.is_set() or time.monotonic() >= recorder.end:
            break
        await cycle(number, due)
        number += 1
        due += interval
        # Bound each user to one in-flight cycle; report missed offered work,
        # rather than silently treating a slower completion rate as capacity.
        while due < time.monotonic() and due < recorder.end:
            recorder.missed_ticks[name] += 1
            due += interval


async def user_phase(server, participant, recorder, ledger, timeout, waits):
    user = participant["index"]
    last_anchor = 0
    async with httpx.AsyncClient(base_url=server.url, trust_env=False, timeout=timeout,
            headers={"Authorization": "Bearer " + participant["browser_token"]}) as browser:
        async def transport_response(response):
            if recorder.begin <= time.monotonic() < recorder.end:
                recorder.mcp_http_statuses[str(response.status_code)] += 1

        async with httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(timeout, read=65),
                headers={"Authorization": "Bearer " + participant["external_token"]},
                event_hooks={"response": [transport_response]}) as transport:
            async with streamable_http_client(server.url + "/mcp", http_client=transport) as (read, write):
                async with ClientSession(read, write) as session:
                    async def initialize():
                        await asyncio.wait_for(session.initialize(), timeout)
                        return {}, "MCP_OK", 0
                    await recorder.measure("MCP.initialize", "read", user, initialize)

                    async def browser_cycle(number, due):
                        nonlocal last_anchor
                        envelope = await recorder.measure("HTTP.frontier.look", "read", user,
                            lambda: http_function(browser, "frontier.look"), scheduled=due)
                        snapshot = envelope["result"]
                        if snapshot.get("world_id") != WORLD_ID or snapshot["character"]["role_id"] != participant["role_id"]:
                            raise LoadFailure("look_identity_mismatch")
                        anchor = snapshot["history_latest_seq"]
                        if anchor <= last_anchor:
                            return
                        head = await recorder.measure("HTTP.frontier.history", "read", user,
                            lambda: http_function(browser, "frontier.history", {"after": anchor - 1, "limit": 1}))
                        if not head["result"]["items"] or head["result"]["items"][0]["seq"] != anchor:
                            raise LoadFailure("history_head_missing")
                        after = max(0, anchor - 40)
                        for page_number in range(3):
                            if after >= anchor:
                                break
                            page = (await recorder.measure("HTTP.frontier.history", "read", user,
                                lambda: http_function(browser, "frontier.history",
                                                      {"after": after, "limit": min(40, anchor - after)})))["result"]
                            seqs = [item["seq"] for item in page["items"]]
                            if not seqs or seqs != list(range(after + 1, page["next_after"] + 1)):
                                raise LoadFailure("history_gap_or_duplicate")
                            after = page["next_after"]
                        if after < anchor:
                            raise LoadFailure("history_tail_not_continued")
                        last_anchor = anchor

                    async def mcp_call(arguments):
                        result = await asyncio.wait_for(session.call_tool("frontier.say", arguments=arguments), timeout)
                        body = result.structured_content
                        if body is None:
                            text = next((item.text for item in result.content if item.type == "text"), "")
                            body = json.loads(text)
                        if result.is_error or not isinstance(body, dict) or body.get("ok") is not True:
                            raise LoadFailure("unexpected_mcp_tool_error")
                        return body, "MCP_OK", len(json.dumps(body, ensure_ascii=False).encode())

                    async def agent_cycle(number, due):
                        operation = f"load-{recorder.label}-{user}-{number}"
                        text = f"load:{recorder.label}:{user}:{number}"
                        intent = {"role_id": participant["role_id"], "text": text, "sequence": None, "acknowledged": False}
                        ledger[operation] = intent  # Include unknown write outcomes in the independent oracle.
                        arguments = {"operation_id": operation, "arguments": {"text": text}}
                        original = await recorder.measure("MCP.frontier.say", "write", user,
                            lambda: mcp_call(arguments), scheduled=due)
                        if (original.get("operation_id") != operation or original.get("actor_role_id") != participant["role_id"]
                                or original.get("replayed") is not False or original["result"]["entry"]["payload"]["text"] != text):
                            raise LoadFailure("original_receipt_mismatch")
                        intent.update(sequence=original["result"]["entry"]["seq"], acknowledged=True)
                        # One tenth over the global, staggered user/cycle index;
                        # short validation runs still exercise some real replay.
                        if (number * recorder.count + user) % 10 == 0:
                            replay = await recorder.measure("MCP.frontier.say.replay", "write", user, lambda: mcp_call(arguments))
                            if (replay.get("replayed") is not True or replay.get("commit_seq") != original.get("commit_seq")
                                    or replay.get("result") != original.get("result")):
                                raise LoadFailure("replay_did_not_preserve_original_result")

                    async def idle_waits():
                        first = await http_json(browser, "GET", "/v1/changes")
                        after = first[0]["next_cursor"]
                        await asyncio.sleep(max(0, recorder.begin - time.monotonic()))
                        while time.monotonic() < recorder.end and not recorder.stop.is_set():
                            requested = min(30, recorder.end - time.monotonic())
                            if requested < .01:
                                break
                            result = await recorder.measure("HTTP.world.wait_changes", "wait", user,
                                lambda: http_json(browser, "GET", "/v1/changes/wait",
                                    params={"after": after, "timeout": requested, "limit": 50}, timeout=requested + 5),
                                wait_timeout=requested)
                            if (result.get("cancelled") or result.get("timed_out") is not True
                                    or result.get("events") != [] or result["next_cursor"] < after):
                                raise LoadFailure("idle_notification_wait_contract_mismatch")
                            after = result["next_cursor"]

                    jobs = [periodic(recorder, 6, user * min(6, recorder.duration) / recorder.count,
                                     "browser", browser_cycle),
                            periodic(recorder, 10, user * min(10, recorder.duration) / recorder.count,
                                     "external", agent_cycle)]
                    if waits:
                        jobs.append(idle_waits())
                    outcomes = await asyncio.gather(*jobs, return_exceptions=True)
                    for outcome in outcomes:
                        if isinstance(outcome, BaseException):
                            recorder.fail("user_workload", user, outcome)


def rss_kib(pid):
    try:
        result = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True,
                                text=True, check=False, timeout=2)
        return int(result.stdout.strip()) if result.returncode == 0 and result.stdout.strip() else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


async def sample_rss(server, recorder, finished):
    while not finished.is_set():
        value = await asyncio.to_thread(rss_kib, server.proc.pid)
        recorder.rss.append({"elapsed_seconds": round(time.monotonic() - recorder.created, 3), "rss_kib": value})
        try:
            await asyncio.wait_for(finished.wait(), timeout=2)
        except TimeoutError:
            pass


def oracle(db_path, ledger, baseline, participants, scene_count, *, integrity=False):
    """Separate read-only connection checks actual writes, not client counters."""
    failures = []
    with sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True) as db:
        db.execute("BEGIN")
        histories = [json.loads(row[0]) for row in db.execute(
            "SELECT value_json FROM world_state WHERE universe=? AND scope=? AND deleted=0", (UNIVERSE, HISTORY))]
        actual = [item for item in histories if item["kind"] == "say"
                  and item["payload"].get("text", "").startswith("load:")]
        text_counts = Counter(item["payload"]["text"] for item in actual)
        expected_texts = Counter(intent["text"] for intent in ledger.values())
        if text_counts != expected_texts:
            failures.append("missing_or_duplicate_actual_say")
        if len(histories) != baseline + len(ledger):
            failures.append("actual_history_count_mismatch")
        if sorted(item["seq"] for item in histories) != list(range(1, len(histories) + 1)):
            failures.append("actual_history_sequence_gap")
        receipts = db.execute(
            "SELECT actor_role_id,idempotency_key,function_id,receipt_json FROM operations "
            "WHERE universe=? AND idempotency_key LIKE 'load-%'", (UNIVERSE,)).fetchall()
        stored = {row[1]: row for row in receipts}
        if len(receipts) != len(ledger) or set(stored) != set(ledger):
            failures.append("missing_or_duplicate_actual_receipt")
        for operation, intent in ledger.items():
            row = stored.get(operation)
            if row is None:
                continue
            receipt = json.loads(row[3])
            entry = receipt.get("result", {}).get("entry", {})
            if (row[0] != intent["role_id"] or row[2] != "frontier.say"
                    or receipt.get("operation_id") != operation or receipt.get("actor_role_id") != intent["role_id"]
                    or entry.get("payload", {}).get("text") != intent["text"]
                    or (intent["sequence"] is not None and entry.get("seq") != intent["sequence"])):
                failures.append("actual_receipt_content_mismatch")
                break
        counts = {scope: db.execute("SELECT COUNT(*) FROM world_state WHERE universe=? AND scope=? AND deleted=0",
                                    (UNIVERSE, scope)).fetchone()[0]
                  for scope in (CHARACTERS, SCENES, REQUESTS)}
        if counts[CHARACTERS] != participants or counts[SCENES] != scene_count or counts[REQUESTS] != 0:
            failures.append("fixture_state_counts_changed")
        integrity_result = db.execute("PRAGMA integrity_check").fetchone()[0] if integrity else None
        if integrity and integrity_result != "ok":
            failures.append("sqlite_integrity_failure")
    return {"status": "failed" if failures else "passed", "failures": failures,
            "expected_unique_writes": len(ledger), "acknowledged_unique_writes": sum(i["acknowledged"] for i in ledger.values()),
            "actual_unique_receipts": len(receipts), "actual_say_entries": len(actual),
            "history_count": len(histories), "registered_characters": counts[CHARACTERS],
            "scenes": counts[SCENES], "requests": counts[REQUESTS], "integrity_check": integrity_result}


async def stage(server, participants, ledger, args, count, label, waits, baseline):
    recorder = Recorder(label, count, args.duration if not waits else args.wait_duration, args.warmup)
    finished = asyncio.Event()
    sampler = asyncio.create_task(sample_rss(server, recorder, finished))
    try:
        outcomes = await asyncio.gather(*(user_phase(server, participant, recorder, ledger, args.timeout, waits)
                                         for participant in participants[:count]), return_exceptions=True)
        for user, outcome in enumerate(outcomes):
            if isinstance(outcome, BaseException):
                recorder.fail("MCP.session", user, outcome)
    finally:
        finished.set()
        await sampler
    result = recorder.finish()
    result["database_oracle"] = await asyncio.to_thread(oracle, server.db, ledger, baseline,
                                                       len(participants), args.seed_scenes)
    if result["database_oracle"]["status"] != "passed":
        result["status"] = "failed"
        result["failures"].append("independent_database_oracle_failed")
    if waits and not result["metrics"].get("HTTP.world.wait_changes"):
        result["status"] = "failed"
        result["failures"].append("no_measured_waits")
    return result


async def workload(server, setup, args, report):
    participants, baseline = await prepare(server, setup, max(args.levels), args.seed_scenes, args.seed_history)
    report["fixture"] = {"registered_characters": len(participants), "scenes": args.seed_scenes,
                         "initial_history_count": baseline, "requests": 0,
                         "creation": "normal public key entry, join, move, expand and say; no direct business-table writes"}
    ledger = {}
    report["ledger"], report["baseline_history_count"] = ledger, baseline
    for count in args.levels:
        result = await stage(server, participants, ledger, args, count, f"normal-{count}", False, baseline)
        report["stages"].append(result)
        print(json.dumps({"stage": result["name"], "users": count, "status": result["status"],
                          "failures": result["failures"]}), flush=True)
        if result["status"] != "passed":
            break
        report["highest_normal_passed_users"] = count
    highest = report["highest_normal_passed_users"]
    if highest is not None and args.wait_duration:
        result = await stage(server, participants, ledger, args, highest, f"wait-{highest}", True, baseline)
        report["stages"].append(result)
        print(json.dumps({"stage": result["name"], "users": highest, "status": result["status"],
                          "failures": result["failures"]}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True, help="New, absent run directory inside DevDisk Agent World data.")
    parser.add_argument("--output", type=Path, required=True, help="New report path inside DevDisk Agent World data.")
    parser.add_argument("--port", type=int, default=8812, help="Owned loopback port; 0 selects an available port. 8811 is forbidden.")
    parser.add_argument("--levels", type=lambda text: [int(i) for i in text.split(",")], default=[8, 16, 32, 64])
    parser.add_argument("--duration", type=float, default=120)
    parser.add_argument("--warmup", type=float, default=10)
    parser.add_argument("--wait-duration", type=float, default=120, help="Highest-pass idle-wait phase; 0 skips it explicitly.")
    parser.add_argument("--timeout", type=float, default=10)
    parser.add_argument("--seed-scenes", type=int, default=64)
    parser.add_argument("--seed-history", type=int, default=256)
    args = parser.parse_args()
    if (not args.levels or args.levels != sorted(set(args.levels))
            or any(level not in {8, 16, 32, 64} for level in args.levels)
            or not (.1 <= args.duration <= 180 and 0 <= args.warmup <= 30
                    and 0 <= args.wait_duration <= 180 and 1 <= args.timeout <= 30
                    and 3 <= args.seed_scenes <= 64 and 0 <= args.seed_history <= 1000)
            or not 0 <= args.port <= 65535 or args.port == 8811):
        parser.error("Use ascending 8/16/32/64 levels and bounded duration, fixture and loopback port values.")
    if not DEV_DISK.is_mount():
        parser.error("DevDisk must be mounted; no system-disk fallback is allowed.")
    try:
        args.data_dir, args.output = destination(args.data_dir), destination(args.output)
        if args.data_dir.exists() or args.output.exists() or args.data_dir == PROJECT_DATA or args.output == PROJECT_DATA:
            raise ValueError("Use an absent run directory and absent report; existing data is never reused or replaced.")
        if args.data_dir.is_relative_to(args.output):
            raise ValueError("The report cannot be the run directory or one of its parent directories.")
        if (args.output.is_relative_to(args.data_dir)
                and args.output.relative_to(args.data_dir).parts[0] in {"private", "database", "server.log"}):
            raise ValueError("The report cannot overlap the owned database, identity keys or server log.")
    except ValueError as exc:
        parser.error(str(exc))
    os.umask(0o077)
    args.data_dir.mkdir(parents=True, mode=0o700, exist_ok=False)
    report = {"schema": "frontier-load-1", "status": "running",
              "scope": "finite deterministic local-loopback HTTP browser-shaped reads and real MCP transport; no model inference",
              "not_proven": ["human enjoyment", "natural cross-day continuity", "cross-machine network", "production or MMO capacity",
                             "notification wake delivery", "large request backlogs", "maximum-length authored text"],
              "python": platform.python_version(), "platform": platform.platform(), "world_id": WORLD_ID,
              "data_dir": str(args.data_dir), "source_sha256_before": source_hash(), "stages": [],
              "highest_normal_passed_users": None,
              "plan": {"levels": args.levels, "seconds_per_level": args.duration, "warmup_seconds": args.warmup,
                       "wait_phase_seconds": args.wait_duration, "browser_interval_seconds": 6, "external_interval_seconds": 10,
                       "replay_fraction": .1, "wait_transport": "HTTP /v1/changes/wait, timeout up to 30 seconds",
                       "read_p95_budget_seconds": 1, "write_p95_budget_seconds": 2, "p99_budget_seconds": 5,
                       "wait_latency_budget": "Successful empty waits use elapsed minus requested timeout; raw latency is also retained.",
                       "failure_policy": "Stop ascending at the first failing level; any unexpected error or lost/duplicate write fails.",
                       "scheduling": "Staggered per-user deadlines, one in-flight cycle per workload, no round barrier; missed ticks retained."}}
    server = OwnedServer(args.data_dir / "database/world.sqlite3", args.data_dir / "server.log", args.port)
    started = time.monotonic()
    failure = None
    try:
        setup = initialize_local(server.db, args.data_dir / "private")
        server.start()
        report["loopback_origin"], report["owned_server_pid"] = server.url, server.proc.pid
        asyncio.run(workload(server, setup, args, report))
    except (Exception, KeyboardInterrupt) as exc:
        failure = exc
        report["error"] = exc.code if isinstance(exc, LoadFailure) else type(exc).__name__
    finally:
        try:
            server.stop()
        except Exception as exc:
            failure = failure or exc
            report["cleanup_error"] = type(exc).__name__
        report["owned_server_stopped"] = server.proc is None or server.proc.poll() is not None
        if "ledger" in report and report["owned_server_stopped"]:
            try:
                report["final_database_oracle"] = oracle(server.db, report["ledger"], report["baseline_history_count"],
                                                        max(args.levels), args.seed_scenes, integrity=True)
            except Exception as exc:
                failure = failure or exc
                report["oracle_error"] = type(exc).__name__
        report["source_sha256_after"] = source_hash()
        report["source_unchanged"] = report["source_sha256_before"] == report["source_sha256_after"]
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        report["status"] = "passed" if (failure is None and report["stages"]
            and all(stage_["status"] == "passed" for stage_ in report["stages"])
            and report.get("final_database_oracle", {}).get("status") == "passed"
            and report["source_unchanged"] and report["owned_server_stopped"]) else "failed"
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        print(json.dumps({"status": report["status"], "highest_normal_passed_users": report["highest_normal_passed_users"],
                          "report": str(args.output), "owned_server_stopped": report["owned_server_stopped"]}), flush=True)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
