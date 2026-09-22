import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
const source = readFileSync(new URL("../agent_world/web/world-client.js", import.meta.url), "utf8");
const {WorldClient, applyViewUpdate, StaleViewDelta} = await import("data:text/javascript;base64," + Buffer.from(source).toString("base64"));
const base = {kind: "snapshot", cursor: "first", view: "scene", viewer_role_id: "A", world_version: 1, view_version: 1,
  snapshot: {entities: {door: {open: false}, old: {}}, resources: {old: {}}, meta: {round: 1}}};
const delta = {kind: "delta", cursor: "second", base_cursor: "first", view: "scene", viewer_role_id: "A", world_version: 1, view_version: 1,
  delta: {entities: {upsert: {door: {open: true}}, remove: ["old"]}, resources: {upsert: {}, remove: ["old"]}, meta: {round: 2}}};
let current = applyViewUpdate(null, base);
current = applyViewUpdate(current, delta);
assert.equal(current.snapshot.entities.door.open, true);
assert.equal(current.snapshot.entities.old, undefined);
assert.equal(current.snapshot.resources.old, undefined);
assert.equal(base.snapshot.entities.door.open, false);
assert.throws(() => applyViewUpdate(current, delta), StaleViewDelta);
assert.throws(() => applyViewUpdate(applyViewUpdate(null, base), {...delta, viewer_role_id: "B"}), StaleViewDelta);
const malicious = JSON.parse('{"entities":{"__proto__":{"polluted":true}},"resources":{},"meta":{}}');
current = applyViewUpdate(null, {...base, snapshot: malicious});
assert.equal(Object.getPrototypeOf(current.snapshot.entities), null);
assert.equal({}.polluted, undefined);
let requests = [];
const ok = (data) => ({ok: true, status: 200, json: async () => data});
const client = new WorldClient("http://localhost:9876", () => "TEST_ONLY", async (url, options) => {
  requests.push([url, options]); return ok(base);
});
await client.act("encounter.strike", {target: "B"}, "stable-id");
assert.equal(JSON.parse(requests[0][1].body).operation_id, "stable-id");
assert.equal(requests[0][1].credentials, "omit");
assert.equal(requests[0][0].includes("TEST_ONLY"), false);
assert.throws(() => client.act("encounter.strike", {}, ""));
let resolveFirst, resolveSecond;
const racing = new WorldClient("http://localhost:9876", () => "TEST_ONLY", () => new Promise(resolve => {
  if (!resolveFirst) resolveFirst = resolve; else resolveSecond = resolve;
}));
const first = racing.loadView("old");
await new Promise(resolve => setImmediate(resolve));
const second = racing.loadView("new");
await new Promise(resolve => setImmediate(resolve));
resolveSecond(ok({...base, view: "new", cursor: "new"}));
await second;
resolveFirst(ok({...base, view: "old", cursor: "old"}));
await first;
assert.equal(racing.view.view, "new");
const denied = new WorldClient("http://localhost:9876", () => "TEST_ONLY", async () => ({ok: false, status: 403,
  json: async () => ({error: "PermissionDenied", message: "denied"})}));
denied.view = base;
await assert.rejects(() => denied.request("/v1/views"));
assert.equal(denied.view, null);
console.log("view client: snapshot, delta, order, viewer, resource removal, safe IDs, stable intents, race, access denial PASS");
