import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import GroveNativeContext from "../agent_world/grove_native_context.mjs";

const HOOK = "experimental.chat.messages.transform";
const SOURCE = new URL("../agent_world/grove_native_context.mjs", import.meta.url);

async function fixture(t) {
  const directory = await mkdtemp(join(tmpdir(), "grove-native-context-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const options = { version: 1, nonce: "1".repeat(32), role_id: "same-player", session_id: "same-session",
    source_sha256: createHash("sha256").update(await readFile(SOURCE)).digest("hex"),
    diagnostic_path: join(directory, "native-context.json"),
    machine_context_prefixes: ["Exact machine facts header", "Exact private context header"],
    formal_tools: ["grove_world_grove_definition_publish", "grove_world_grove_definition_read", "grove_world_grove_interact"] };
  const hooks = await GroveNativeContext({}, options);
  return { options, transform: hooks[HOOK], diagnostic: () => readFile(options.diagnostic_path, "utf8").then(JSON.parse) };
}

const messageID = value => value.startsWith("msg_") ? value : `msg_${value.replaceAll("-", "")}`;
const partID = value => value.startsWith("prt_") ? value : `prt_${value.replaceAll("-", "")}`;

function message(id, role, parts, extra = {}) {
  id = messageID(id);
  return { info: { id, role, sessionID: "same-session", agent: "shared-grove-executor", ...extra,
      ...(extra.parentID ? { parentID: messageID(extra.parentID) } : {}) },
    parts: parts.map(part => ({ messageID: id, sessionID: "same-session", ...part,
      ...(part.id ? { id: partID(part.id) } : {}) })) };
}

function indexBody(part) {
  assert.equal(part.type, "text");
  assert.equal(part.synthetic, true);
  assert.ok(part.text.startsWith("[Grove native historical tool location index]\n"));
  return JSON.parse(part.text.slice(part.text.indexOf("\n") + 1));
}

test("native hook folds an oversized historical pair and retains recent complete definitions, current turn and people", async t => {
  const { transform, diagnostic } = await fixture(t);
  const definition = { name: "Original", fields: { viewport: { type: "int", default: 1 } } };
  const published = { ok: true, operation_id: "original-op", commit_seq: 8,
    result: { definition_ref: "original-def", definition, already_published: false },
    confirmed_outcome: { operation_id: "original-op", costs: [{ material: "wood", quantity_u: 10 }] },
    execution_readback: { entities: [{ id: "actual", padding: "x".repeat(50000) }] } };
  const original = [
    message("old-user", "user", [{ id: "human", type: "text", text: "我喜欢布，做个东西。" },
      { id: "machine", type: "text", text: "Exact machine facts header\n{old:true}" }]),
    message("old-agent", "assistant", [{ id: "false-summary", type: "text", text: "只用了20单位木头。" },
      { id: "old-reason", type: "reasoning", text: "An old incorrect plan" },
      { id: "original-publish", type: "tool", tool: "grove_world_grove_definition_publish", callID: "original-call",
        state: { status: "completed", input: { definition }, output: JSON.stringify(published), time: { start: 1, end: 2 } } },
      { id: "recent-publish", type: "tool", tool: "grove_world_grove_definition_publish", callID: "recent-call",
        state: { status: "completed", input: { definition }, output: JSON.stringify({ ...published,
          operation_id: "recent-op", execution_readback: { entities: [{ id: "recent" }] } }), time: { start: 3, end: 4 } } }],
      { parentID: "old-user" }),
    message("unrelated", "assistant", [{ id: "other-text", type: "text", text: "A different agent's text" }],
      { agent: "different-agent", parentID: "old-user" }),
    message("current", "user", [{ id: "request", type: "text", text: "刚才到底用了多少？" },
      { id: "fresh", type: "text", text: "Exact machine facts header\n{current:true}" }]),
    message("current-agent", "assistant", [{ id: "current-thought", type: "reasoning", text: "Current needed reasoning" },
      { id: "current-text", type: "text", text: "Current answer" }], { parentID: "current" }),
  ];
  const before = JSON.stringify(original);
  const inference = { messages: structuredClone(original) };
  await transform({}, inference);
  assert.equal(JSON.stringify(original), before);
  assert.deepEqual(inference.messages[0].parts, [original[0].parts[0]]);
  assert.deepEqual(inference.messages[2], original[2]);
  assert.deepEqual(inference.messages[3], original[3]);
  assert.deepEqual(inference.messages[4], original[4]);
  const actual = inference.messages[1].parts;
  assert.equal(actual.length, 2);
  const index = indexBody(actual[0]);
  assert.equal(index.historical_only, true);
  assert.equal(index.omitted_tool_parts, 1);
  assert.equal(index.source_boundary.last.call_id, "original-call");
  assert.equal(index.source_boundary.last.operation_id, "original-op");
  assert.deepEqual(index.read, { tool: "native_tool_history", arguments: {
    mode: "read", message_id: messageID("old-agent"), part_id: partID("original-publish") } });
  assert.deepEqual(index.list, { tool: "native_tool_history", arguments: { mode: "list" } });
  assert.equal(index.input_read.arguments.section, "input");
  assert.equal(actual[0].state, undefined);
  assert.equal(actual[0].tool, undefined);
  assert.equal(actual[0].callID, undefined);
  assert.deepEqual(actual[1].state.input, original[1].parts[3].state.input);
  assert.equal(actual[1].callID, "recent-call");
  const projected = JSON.parse(actual[1].state.output);
  assert.deepEqual(projected.result, published.result);
  assert.deepEqual(projected.confirmed_outcome, published.confirmed_outcome);
  assert.equal(projected.operation_id, "recent-op");
  assert.equal(projected.commit_seq, 8);
  assert.deepEqual(projected.execution_readback, { entities: [{ id: "recent" }] });
  assert.equal(projected.native_context_projection.read.arguments.part_id, partID("recent-publish"));
  const proof = await diagnostic();
  assert.equal(proof.hook, HOOK);
  assert.equal(proof.projection.omitted_assistant_parts, 2);
  assert.equal(proof.projection.omitted_historical_tool_parts, 1);
  assert.equal(proof.projection.retained_historical_tool_parts, 1);
  assert.ok(proof.projection.omitted_historical_tool_output_bytes > 50000);
  assert.ok(proof.projection.historical_tool_history_bytes_after <= 32 * 1024);
  assert.ok(proof.projection.historical_tool_characters_after < 2500);
  assert.equal(proof.projection.stored_originals_changed, false);
  assert.equal(JSON.stringify(proof).includes("只用了"), false);
  assert.equal(JSON.stringify(proof).includes("我喜欢"), false);
});

test("current tools and old failures/native compact markers remain exact; non-JSON old completed output is marked", async t => {
  const { transform } = await fixture(t);
  function tool(id, state) {
    return { id, type: "tool", tool: "grove_world_grove_interact", callID: id, state };
  }
  const currentBody = JSON.stringify({ ok: true, execution_readback: { current: "actual" } });
  const original = [
    message("old-agent", "assistant", [
      tool("failed", { status: "error", error: "Actual failure", time: { start: 1, end: 2 } }),
      tool("native-compacted", { status: "completed", output: currentBody, input: { amount: 10 }, time: { compacted: 4 } }),
      tool("malformed", { status: "completed", output: "not JSON", input: {} }),
    ], { parentID: "old" }),
    message("current", "user", [{ type: "text", text: "接着试试" }]),
    message("current-agent", "assistant", [tool("current-action", {
      status: "completed", output: currentBody, input: { amount: 10 }, time: { start: 5, end: 6 },
    })], { parentID: "current" }),
  ];
  const inference = { messages: structuredClone(original) };
  await transform({}, inference);
  assert.deepEqual(inference.messages[0].parts.slice(0, 2), original[0].parts.slice(0, 2));
  assert.deepEqual(inference.messages.slice(1), original.slice(1));
  const historical = inference.messages[0].parts[2];
  assert.deepEqual(historical.state.input, original[0].parts[2].state.input);
  assert.ok(historical.state.output.startsWith("[Grove historical completed tool result: "));
  assert.ok(historical.state.output.endsWith("\nnot JSON"));
  assert.ok(historical.state.output.includes('"historical_only":true'));
  assert.equal(original[0].parts[2].state.output, "not JSON");
  const once = structuredClone(inference.messages);
  await transform({}, inference);
  assert.deepEqual(inference.messages, once);
});

test("all old formal completed results are historical, including unchanged definitions without a later scene", async t => {
  const { transform, diagnostic } = await fixture(t);
  const definition = { name: "Past definition", fields: { tone: { type: "int", default: 0 } } };
  const actual = { operation_id: "old-operation", commit_seq: 20, observation_id: "past-observation",
    result: { definition_ref: "fixed-reference", definition, fields: { tone: 1 },
      entity_ref: "#/result/entity", entity: { id: "past-object" } },
    confirmed_outcome: { kind: "server_receipt", costs: [{ material: "cloth", quantity_u: 2 }] } };
  const original = [message("old", "assistant", [{ id: "past-read", type: "tool",
    tool: "grove_world_grove_definition_read", callID: "old-read-call", state: {
      status: "completed", input: { definition_ref: "fixed-reference" }, output: JSON.stringify(actual) } },
    { id: "other-tool", type: "tool", tool: "unrelated_tool", state: {
      status: "completed", output: JSON.stringify(actual) } }], { agent: "another-agent", parentID: "past-user" }),
    message("current", "user", [{ type: "text", text: "去试试别人的牌子。" }])];
  const before = JSON.stringify(original);
  const inference = { messages: structuredClone(original) };
  await transform({}, inference);
  const tool = inference.messages[0].parts[0];
  const body = JSON.parse(tool.state.output);
  assert.deepEqual(tool.state.input, original[0].parts[0].state.input);
  assert.equal(tool.callID, "old-read-call");
  assert.deepEqual(body.result, actual.result);
  assert.deepEqual(body.confirmed_outcome, actual.confirmed_outcome);
  assert.equal(body.operation_id, "old-operation");
  assert.equal(body.commit_seq, 20);
  assert.equal(body.native_context_projection.historical_only, true);
  assert.equal(body.native_context_projection.scope, "historical_tool_result");
  assert.deepEqual(body.native_context_projection.omitted_sections, []);
  assert.ok(body.native_context_projection.meaning.includes("current task_action_summary"));
  assert.deepEqual(inference.messages[0].parts[1], original[0].parts[1]);
  assert.equal(JSON.stringify(original), before);
  const proof = (await diagnostic()).projection;
  assert.equal(proof.historically_marked_tool_parts, 1);
  assert.equal(proof.reduced_tool_parts, 0);
  const once = structuredClone(inference.messages);
  await transform({}, inference);
  assert.deepEqual(inference.messages, once);
});

test("native compaction summaries retain all preferences and references with explicit historical scope", async t => {
  const { transform, diagnostic } = await fixture(t);
  const original = [message("past-person", "user", [{ id: "human", type: "text", text: "我喜欢布，不要替我玩。" }]),
    message("old-summary", "assistant", [{ id: "summary-text", type: "text",
      text: "用户喜欢布；旧操作op-1做了布牌；引用fixed-ref。" }],
      { agent: "compaction", mode: "compaction", summary: true, parentID: "past-person" }),
    message("summary-like-text", "assistant", [{ id: "other-prose", type: "text", text: "Summary: original unrelated prose" }],
      { agent: "other-agent", parentID: "past-person" }),
    message("current", "user", [{ id: "goal", type: "text", text: "去试半夏的牌子。" }]),
    message("current-summary", "assistant", [{ type: "text", text: "Current request remains whole" }],
      { agent: "compaction", mode: "compaction", summary: true, parentID: "current" })];
  const before = JSON.stringify(original);
  const inference = { messages: structuredClone(original) };
  await transform({}, inference);
  const text = inference.messages[1].parts[0].text;
  assert.ok(text.startsWith("[Grove历史原生摘要："));
  assert.ok(text.includes("task_action_summary"));
  assert.ok(text.endsWith(original[1].parts[0].text));
  assert.deepEqual(inference.messages[0], original[0]);
  assert.deepEqual(inference.messages.slice(2), original.slice(2));
  assert.deepEqual(inference.messages[1].info, original[1].info);
  assert.equal(JSON.stringify(original), before);
  assert.equal((await diagnostic()).projection.marked_native_summary_parts, 1);
  const once = structuredClone(inference.messages);
  await transform({}, inference);
  assert.deepEqual(inference.messages, once);
});

test("recent complete historical scenes and every authored or protocol reference remain exact", async t => {
  const { transform, diagnostic } = await fixture(t);
  const definition = { fields: { entity_ref: "#/execution_readback/entity", viewport: 2 },
    program: { field_references: { scene: "#/execution_readback/entities/0" } } };
  const output = { operation_id: "original-op", commit_seq: 7,
    result: { definition, definition_ref: "immutable-def", entity: {
      id: "object", entity_ref: "#/execution_readback/entities/0",
      field_references: { state: "#/execution_readback/entities/0/state", result: "#/result/entity/id" } },
      viewport: { clock: { sequence: 50 }, entities: [{ id: "actual-current-scene" }] } },
    confirmed_outcome: { operation_id: "original-op", result: { id: "object", status: "created" } },
    execution_readback: { entities: [{ id: "actual-current-scene" }] } };
  const original = [message("old", "assistant", [{ id: "old-tool", type: "tool",
    tool: "grove_world_grove_definition_publish", state: { status: "completed", input: { definition },
      output: JSON.stringify(output), time: { start: 1, end: 2 } } }], { parentID: "previous" }),
    message("current", "user", [{ type: "text", text: "看看刚才的作品。" }])];
  const inference = { messages: structuredClone(original) };
  await transform({}, inference);
  const actual = JSON.parse(inference.messages[0].parts[0].state.output);
  assert.deepEqual(actual.result.definition, definition);
  assert.deepEqual(inference.messages[0].parts[0].state.input, { definition });
  assert.deepEqual(actual.confirmed_outcome, output.confirmed_outcome);
  assert.deepEqual(actual.result.viewport, output.result.viewport);
  assert.equal(actual.result.entity.entity_ref, output.result.entity.entity_ref);
  assert.deepEqual(actual.result.entity.field_references, output.result.entity.field_references);
  assert.deepEqual(actual.execution_readback, output.execution_readback);
  assert.deepEqual(actual.native_context_projection.omitted_sections, []);
  assert.equal(actual.native_context_projection.original.message_id, messageID("old"));
  assert.equal((await diagnostic()).projection.character_unit, "utf16_code_units");
  assert.deepEqual(JSON.parse(original[0].parts[0].state.output), output);
});

test("large original call inputs fold into one bounded source index without changing current tools", async t => {
  const { transform, diagnostic } = await fixture(t);
  const smallOutput = JSON.stringify({ ok: true, operation_id: "actual-operation", result: { definition_ref: "original-ref" } });
  const tool = (id, size, time = {}) => ({ id, type: "tool", tool: "grove_world_grove_definition_publish",
    callID: `call-${id}`, state: { status: "completed", input: { definition: { program: "字".repeat(size) } },
      output: smallOutput, time } });
  const original = [
    message("old-user", "user", [{ id: "human", type: "text", text: "保留原偏好与原ref" }]),
    message("old-tools", "assistant", [tool("old-one", 12000, { compacted: 3 }),
      tool("old-two", 12000), tool("recent-one", 2000), tool("recent-two", 2000)], { parentID: "old-user" }),
    message("current", "user", [{ id: "goal", type: "text", text: "继续原目标" }]),
    // Position after the current user also protects tools with anomalous parent metadata.
    message("current-tools", "assistant", [tool("current-large", 20000)], { parentID: "other-parent" }),
  ];
  const before = JSON.stringify(original);
  const inference = { messages: structuredClone(original) };
  await transform({}, inference);
  assert.equal(JSON.stringify(original), before);
  assert.deepEqual(inference.messages[0], original[0]);
  assert.deepEqual(inference.messages.slice(2), original.slice(2));
  const parts = inference.messages[1].parts;
  assert.equal(parts.length, 3);
  const index = indexBody(parts[0]);
  assert.equal(index.omitted_tool_parts, 2);
  assert.equal(index.source_boundary.first.part_id, partID("old-one"));
  assert.equal(index.source_boundary.last.part_id, partID("old-two"));
  assert.equal(index.source_boundary.last.operation_id, "actual-operation");
  assert.ok(index.meaning.includes("not original result bodies"));
  assert.equal(parts.filter(part => part.type === "text").length, 1);
  assert.deepEqual(parts.slice(1).map(part => part.state.input), original[1].parts.slice(2).map(part => part.state.input));
  const proof = (await diagnostic()).projection;
  assert.equal(proof.omitted_historical_tool_parts, 2);
  assert.ok(proof.omitted_historical_tool_input_bytes > 70000);
  assert.equal(proof.omitted_historical_tool_output_bytes,
    Buffer.byteLength("[Old tool result content cleared]", "utf8") + Buffer.byteLength(smallOutput, "utf8"));
  assert.equal(proof.historical_tool_history_bytes_after,
    proof.retained_historical_tool_pair_bytes + proof.historical_tool_index_bytes);
  assert.ok(proof.historical_tool_history_bytes_after <= proof.historical_tool_history_budget_bytes);
  assert.ok(proof.current_tool_input_bytes > proof.historical_tool_history_budget_bytes);
  assert.equal(proof.budget_scope, "readable_completed_formal_tool_pairs_only");
  assert.equal(proof.byte_unit, "utf8");
  const once = structuredClone(inference.messages);
  await transform({}, inference);
  assert.deepEqual(inference.messages, once);
  assert.equal(indexBody(inference.messages[1].parts[0]).omitted_tool_parts, 2);
});

test("unreadable and error-message parts remain recoverable in the native copy and cannot swallow the index", async t => {
  const { transform, diagnostic } = await fixture(t);
  const tool = (id, state) => ({ id, type: "tool", tool: "grove_world_grove_interact", callID: id, state });
  const largeInput = { authored: "x".repeat(40000) };
  const completed = { status: "completed", input: largeInput, output: JSON.stringify({ operation_id: "actual-op" }) };
  const original = [
    message("failed-assistant", "assistant", [tool("error-message-completed", completed)],
      { parentID: "old", error: { name: "APIError", data: { message: "Actual provider failure" } } }),
    message("old-tools", "assistant", [tool("foldable", completed),
      tool("failed", { status: "error", input: largeInput, error: "Real error" }),
      tool("pending", { status: "pending", input: largeInput }),
      tool("running", { status: "running", input: largeInput }),
      tool("placeholder", { ...completed, output: "[Old tool result content cleared]", time: { compacted: 4 } }),
      tool("no-original-input", { ...completed, input: null })], { parentID: "old" }),
    message("current", "user", [{ type: "text", text: "继续" }]),
  ];
  const before = JSON.stringify(original);
  const inference = { messages: structuredClone(original) };
  await transform({}, inference);
  assert.equal(JSON.stringify(original), before);
  assert.deepEqual(inference.messages[0].info.error, original[0].info.error);
  assert.equal(inference.messages[0].parts[0].type, "tool");
  assert.deepEqual(inference.messages[0].parts[0].state.input, largeInput);
  const parts = inference.messages[1].parts;
  assert.equal(indexBody(parts[0]).omitted_tool_parts, 1);
  assert.equal(parts[0].messageID, original[1].info.id);
  assert.equal(inference.messages[1].info.error, undefined);
  assert.deepEqual(parts.slice(1, 4), original[1].parts.slice(1, 4));
  assert.equal(parts[4].type, "tool");
  assert.equal(parts[4].state.time.compacted, 4);
  assert.equal(parts[5].type, "tool");
  const proof = (await diagnostic()).projection;
  assert.equal(proof.unfoldable_historical_tool_parts, 6);
  assert.ok(proof.unfoldable_historical_tool_input_bytes > 150000);
  assert.equal(proof.retained_historical_tool_parts, 0);
  assert.ok(proof.historical_tool_history_bytes_after <= 32 * 1024);
});

test("hook fails closed on a different session and skips non-Grove or compaction requests", async t => {
  const { transform } = await fixture(t);
  const old = message("old", "assistant", [{ type: "text", text: "Original" }], { parentID: "past" });
  for (const current of [
    message("current", "user", [{ type: "text", text: "Coding" }], { agent: "build" }),
    message("current", "user", [{ type: "compaction" }]),
  ]) {
    const messages = [structuredClone(old), current];
    const before = structuredClone(messages);
    await transform({}, { messages });
    assert.deepEqual(messages, before);
  }
  const mixed = { messages: [structuredClone(old), message("current", "user", [], { sessionID: "other-session" })] };
  const before = structuredClone(mixed.messages);
  await assert.rejects(() => transform({}, mixed), /Mixed native session/);
  assert.deepEqual(mixed.messages, before);
});

test("a mismatched bundled source cannot produce a loading proof", async t => {
  const { options } = await fixture(t);
  await assert.rejects(() => GroveNativeContext({}, { ...options, source_sha256: "0".repeat(64) }), /Invalid managed/);
});
