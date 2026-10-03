"""Public HTTP regressions for two independently authored, unrelated worlds.

The fixture preserves its original SDK consumer source; this request consumer
uses only HTTP and its own Ed25519 encoding, with no Runtime business shortcuts.
"""
from __future__ import annotations

import asyncio
import base64
import unittest

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from tests.live_server import LiveServer


RECORDS = "tests.fixtures.independent_author_worlds:RECORDS_WORLD"
CONFIG = "tests.fixtures.independent_author_worlds:CONFIG_WORLD"
VALUE = {"mode": "off", "capacity": 2, "enabled": False}


def encode(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


async def enter(http, key=None):
    key = key or Ed25519PrivateKey.generate()
    public = encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))
    response = await http.post("/v1/key-identities/challenges", json={"public_key": public})
    response.raise_for_status()
    challenge = response.json()
    message = challenge["message"]
    raw = base64.urlsafe_b64decode(message + "=" * (-len(message) % 4))
    response = await http.post("/v1/key-identities/exchange", json={
        "challenge_id": challenge["challenge_id"], "signature": encode(key.sign(raw)),
    })
    response.raise_for_status()
    return response.json(), key


def http(server, identity=None):
    headers = {"Authorization": "Bearer " + identity["token"]} if identity else {}
    return httpx.AsyncClient(base_url=server.url, headers=headers, trust_env=False, timeout=5)


async def invoke(client, function, arguments, operation=None):
    envelope = {"arguments": arguments, "expected_version": 1}
    if operation is not None:
        envelope["operation_id"] = operation
    return await client.post(f"/v1/functions/{function}/invoke", json=envelope)


async def restart(server):
    task = asyncio.create_task(asyncio.to_thread(server.restart, abrupt=True))
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


class IndependentIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def result(self, response):
        self.assertEqual(response.status_code, 200, f"HTTP {response.status_code}")
        return response.json()["result"]

    def error(self, response, status, kind):
        self.assertEqual(response.status_code, status)
        self.assertEqual(response.json()["error"], kind)
        self.assertIs(response.json()["retryable"], False)
        return response.json()

    async def no_receipt(self, client, operation):
        self.error(await client.get(f"/v1/receipts/{operation}"), 404, "ReceiptNotFound")

    async def test_records_privacy_attribution_and_failed_writes_have_no_receipt(self):
        async with LiveServer(RECORDS) as server:
            async with http(server) as public:
                owner, _ = await enter(public)
                observer, _ = await enter(public)
            role = owner["identity"]["role_id"]
            arguments = {"record_id": "note", "text": "Owner's own fact", "visibility": "public"}
            async with http(server, owner["identity"]) as author, http(server, observer["identity"]) as reader:
                first = await invoke(author, "record.put", arguments, "create-note")
                initial = self.result(first)
                self.assertEqual(initial["author_id"], role)
                self.assertEqual(self.result(await invoke(reader, "record.get", {"author_id": role, "record_id": "note"})), initial)
                self.error(await invoke(reader, "record.put", {**arguments, "author_id": role}, "forge-author"), 422, "SchemaRejected")
                await self.no_receipt(reader, "forge-author")
                private = self.result(await invoke(author, "record.visibility", {"record_id": "note", "visibility": "private"}, "withdraw-public"))
                hidden = self.error(await invoke(reader, "record.get", {"author_id": role, "record_id": "note"}), 404, "ResourceNotFound")
                missing = self.error(await invoke(reader, "record.get", {"author_id": role, "record_id": "missing"}), 404, "ResourceNotFound")
                self.assertEqual(hidden, missing)
                self.assertEqual(self.result(await invoke(reader, "record.list", {"scope": "public"}))["records"], [])
                before = self.result(await invoke(author, "record.list", {"scope": "mine"}))
                self.error(await invoke(author, "record.visibility", {"record_id": "missing", "visibility": "public"}, "missing-record"), 404, "ResourceNotFound")
                await self.no_receipt(author, "missing-record")
                self.assertEqual(self.result(await invoke(author, "record.list", {"scope": "mine"})), before)
                replay = await invoke(author, "record.put", arguments, "create-note")
                self.assertEqual(self.result(replay), initial)
                self.assertIs(replay.json()["replayed"], True)
                self.assertEqual(self.result(await invoke(author, "record.get", {"author_id": role, "record_id": "note"})), private)

    async def test_config_owner_and_same_revision_concurrency_commit_exactly_once(self):
        async with LiveServer(CONFIG) as server:
            async with http(server) as public:
                owner, _ = await enter(public)
                observer, _ = await enter(public)
            async with http(server, owner["identity"]) as author, http(server, observer["identity"]) as reader:
                initial = self.result(await invoke(author, "config.create", {"config_id": "setting", "value": VALUE}, "create-setting"))
                self.assertEqual(initial["owner_id"], owner["identity"]["role_id"])
                self.assertEqual(self.result(await invoke(reader, "config.get", {"config_id": "setting"})), initial)
                arguments = {"config_id": "setting", "expected_revision": 1, "value": {**VALUE, "capacity": 3}}
                self.error(await invoke(reader, "config.replace", arguments, "unowned-update"), 403, "PermissionDenied")
                await self.no_receipt(reader, "unowned-update")
                self.error(await invoke(author, "config.create", {"config_id": "setting", "value": VALUE}, "duplicate-setting"), 409, "RuleViolation")
                await self.no_receipt(author, "duplicate-setting")
                self.assertEqual(self.result(await invoke(author, "config.get", {"config_id": "setting"})), initial)
                async with asyncio.TaskGroup() as group:
                    left = group.create_task(invoke(author, "config.replace", arguments, "revision-left"))
                    right = group.create_task(invoke(author, "config.replace", {**arguments, "value": {**VALUE, "capacity": 4}}, "revision-right"))
                responses = [("revision-left", left.result()), ("revision-right", right.result())]
                self.assertEqual(sorted(response.status_code for _, response in responses), [200, 409])
                winner_id, winner = next(pair for pair in responses if pair[1].status_code == 200)
                loser_id, loser = next(pair for pair in responses if pair[1].status_code == 409)
                self.error(loser, 409, "RuleViolation")
                await self.no_receipt(author, loser_id)
                final = self.result(winner)
                self.assertEqual(final["revision"], 2)
                self.assertEqual(self.result(await invoke(author, "config.get", {"config_id": "setting"})), final)
                winning_arguments = arguments if winner_id == "revision-left" else {**arguments, "value": {**VALUE, "capacity": 4}}
                replay = await invoke(author, "config.replace", winning_arguments, winner_id)
                self.assertEqual(self.result(replay), final)
                self.assertIs(replay.json()["replayed"], True)
                self.assertEqual(self.result(await invoke(author, "config.get", {"config_id": "setting"}))["revision"], 2)

    async def test_same_key_world_local_scope_and_state_survive_server_restart(self):
        key = Ed25519PrivateKey.generate()
        async with LiveServer(RECORDS) as records, LiveServer(CONFIG) as config:
            async with http(records) as public_records, http(config) as public_config:
                record_entry, _ = await enter(public_records, key)
                config_entry, _ = await enter(public_config, key)
            self.assertEqual(record_entry["public_key"], config_entry["public_key"])
            self.assertNotEqual(record_entry["identity"]["role_id"], config_entry["identity"]["role_id"])
            self.assertTrue(record_entry["identity"]["token"] != config_entry["identity"]["token"])
            async with http(records, record_entry["identity"]) as author_records, http(config, config_entry["identity"]) as author_config:
                record = self.result(await invoke(author_records, "record.put", {"record_id": "private", "text": "Persisted private fact", "visibility": "private"}, "persist-record"))
                setting = self.result(await invoke(author_config, "config.create", {"config_id": "saved", "value": VALUE}, "persist-config"))
            for target, foreign in ((records, config_entry), (config, record_entry)):
                async with http(target, foreign["identity"]) as cross_world:
                    response = await cross_world.get("/v1/bootstrap")
                    self.assertEqual(response.status_code, 401)
                    self.assertIn("error", response.json())
            async with asyncio.TaskGroup() as group:
                group.create_task(restart(records))
                group.create_task(restart(config))
            for server, original, function, arguments, expected, operation in (
                (records, record_entry, "record.get", {"author_id": record["author_id"], "record_id": "private"}, record, "persist-record"),
                (config, config_entry, "config.get", {"config_id": "saved"}, setting, "persist-config"),
            ):
                async with http(server) as new_connection:
                    returned, _ = await enter(new_connection, key)
                self.assertIs(returned["created_profile"], False)
                self.assertEqual(returned["identity"]["role_id"], original["identity"]["role_id"])
                async with http(server, returned["identity"]) as fresh_client:
                    self.assertEqual(self.result(await invoke(fresh_client, function, arguments)), expected)
                    receipt = await fresh_client.get(f"/v1/receipts/{operation}")
                    self.assertEqual(receipt.status_code, 200)
                    self.assertEqual(receipt.json()["operation_id"], operation)
                    self.assertEqual(receipt.json()["result"], expected)

    async def test_authenticated_directory_exposes_usable_domain_schemas(self):
        for profile, world_id, prefix in (
            (RECORDS, "independent.author_records", "record."),
            (CONFIG, "independent.versioned_config", "config."),
        ):
            with self.subTest(world=world_id):
                async with LiveServer(profile) as server:
                    async with http(server) as public:
                        entry, _ = await enter(public)
                    async with http(server, entry["identity"]) as client:
                        bootstrap = await client.get("/v1/bootstrap")
                        describe = await client.get("/v1/describe")
                        discovery = await client.get("/v1/discover", params={"include_schemas": "true"})
                        self.assertEqual(bootstrap.status_code, 200)
                        self.assertEqual(bootstrap.json()["role_id"], entry["identity"]["role_id"])
                        self.assertEqual(describe.status_code, 200)
                        self.assertEqual(describe.json()["world"]["id"], world_id)
                        self.assertTrue(describe.json()["entry_instructions"])
                        self.assertEqual(discovery.status_code, 200)
                        catalog = discovery.json()
                        self.assertIs(catalog["has_more"], False)
                        functions = {item["function_id"]: item for item in catalog["functions"]}
                        self.assertEqual(len(functions), 4)
                        self.assertTrue(all(name.startswith(prefix) for name in functions))
                        for item in functions.values():
                            self.assertIn(item["access"], ("read", "write"))
                            self.assertEqual(item["input_schema"]["type"], "object")
                            self.assertIs(item["input_schema"]["additionalProperties"], False)
                            self.assertTrue(item["description"])
                        create = functions[prefix + ("put" if prefix == "record." else "create")]
                        self.assertNotIn("author_id" if prefix == "record." else "owner_id", create["input_schema"]["properties"])
                        if prefix == "config.":
                            self.assertIn("expected_revision", functions["config.replace"]["input_schema"]["required"])
                        self.result(await invoke(client, prefix + "list", {"scope": "public"} if prefix == "record." else {}))
