import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import GroveNativeContext from "../agent_world/grove_native_context.mjs";

const HOOK = "experimental.chat.messages.transform";
const SOURCE = new URL("../agent_world/grove_native_context.mjs", import.meta.url);

async function fixture(t, overrides = {}) {
  const directory = await mkdtemp(join(tmpdir(), "grove-native-context-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const options = { version: 2, model: "provider/model", nonce: "1".repeat(32), role_id: "same-player", session_id: "same-session",
    source_sha256: createHash("sha256").update(await readFile(SOURCE)).digest("hex"),
    diagnostic_path: join(directory, "native-context.json"),
    machine_context_prefixes: ["Exact machine facts header", "Exact private context header"],
    formal_tools: ["grove_world_grove_definition_publish", "grove_world_grove_definition_read", "grove_world_grove_interact", "grove_world_native_tool_history"],
    ...overrides };
  const hooks = await GroveNativeContext({}, options);
  return { options, hooks, transform: hooks[HOOK], diagnostic: () => readFile(options.diagnostic_path, "utf8").then(JSON.parse) };
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
    execution_readback: { entities: [{ id: "actual", padding: "x".repeat(80000) }] } };
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
  assert.deepEqual(inference.messages[0].parts, []);
  assert.deepEqual(inference.messages[2], original[2]);
  assert.deepEqual(inference.messages[3], original[3]);
  assert.deepEqual(inference.messages[4], original[4]);
  const actual = inference.messages[1].parts;
  assert.equal(actual.length, 2);
  const index = indexBody(actual[0]);
  assert.equal(index.historical_only, true);
  assert.equal(index.omitted_tool_parts, 1);
  assert.equal(index.omitted_user_parts, 1);
  assert.equal(index.source_boundary.first.part_id, partID("human"));
  assert.equal(index.source_boundary.last.call_id, "original-call");
  assert.equal(index.source_boundary.last.operation_id, "original-op");
  assert.deepEqual(index.read, { tool: "native_tool_history", arguments: {
    mode: "read", message_id: messageID("old-agent"), part_id: partID("original-publish") } });
  assert.deepEqual(index.list, { tool: "native_tool_history", arguments: { mode: "list", kind: "all" } });
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
  assert.ok(proof.projection.omitted_historical_tool_output_bytes > 80000);
  assert.ok(proof.projection.historical_tool_history_bytes_after <= 64 * 1024);
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
      tool("old-two", 30000), tool("recent-one", 2000), tool("recent-two", 2000)], { parentID: "old-user" }),
    message("current", "user", [{ id: "goal", type: "text", text: "继续原目标" }]),
    // Position after the current user also protects tools with anomalous parent metadata.
    message("current-tools", "assistant", [tool("current-large", 2000)], { parentID: "other-parent" }),
  ];
  const before = JSON.stringify(original);
  const inference = { messages: structuredClone(original) };
  await transform({}, inference);
  assert.equal(JSON.stringify(original), before);
  assert.deepEqual(inference.messages[0].parts, []);
  assert.deepEqual(inference.messages.slice(2), original.slice(2));
  const parts = inference.messages[1].parts;
  assert.equal(parts.length, 3);
  const index = indexBody(parts[0]);
  assert.equal(index.omitted_tool_parts, 2);
  assert.equal(index.source_boundary.first.part_id, partID("human"));
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
  assert.ok(proof.historical_tool_history_bytes_after >=
    proof.retained_historical_tool_pair_bytes + proof.historical_tool_index_bytes);
  assert.ok(proof.historical_tool_history_bytes_after <= proof.historical_tool_history_budget_bytes);
  assert.ok(proof.current_tool_input_bytes > 6000);
  assert.equal(proof.budget_scope, "historical_native_inference_projection");
  assert.equal(proof.byte_unit, "utf8");
  const once = structuredClone(inference.messages);
  await transform({}, inference);
  assert.deepEqual(inference.messages, once);
  assert.equal(indexBody(inference.messages[1].parts[0]).omitted_tool_parts, 2);
});

test("old terminal, unreadable and in-flight parts clear together when their envelopes exceed the historical allowance", async t => {
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
  assert.deepEqual(inference.messages[0].parts, []);
  assert.deepEqual(inference.messages[1].parts, []);
  assert.deepEqual(inference.messages[2], original[2]);
  const proof = (await diagnostic()).projection;
  assert.equal(proof.unfoldable_historical_tool_parts, 0);
  assert.equal(proof.budget_omitted_historical_tool_parts, 3);
  assert.equal(proof.budget_omitted_unsupported_historical_tool_parts, 3);
  assert.equal(proof.omitted_historical_tool_parts, 4);
  assert.equal(proof.history_source_index_sent, false);
  assert.equal(proof.history_all_old_parts_omitted, true);
  assert.equal(proof.retained_historical_tool_parts, 0);
  assert.equal(proof.historical_tool_history_bytes_after, 0);
  const once = structuredClone(inference.messages);
  await transform({}, inference);
  assert.deepEqual(inference.messages, once);
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

test("old original people and native summaries share the same budget and remain readable by exact source", async t => {
  const { transform, diagnostic } = await fixture(t);
  const original = [
    message("old-user", "user", [{ id: "preference", type: "text", text: "原偏好".repeat(10000) }]),
    // Native's summary flag is the authority; its agent name need not be compaction.
    message("old-summary", "assistant", [{ id: "summary", type: "text", text: "原引用".repeat(10000) }],
      { summary: true, agent: "other-native-agent" }),
    message("current", "user", [{ id: "intent", type: "text", text: "按我本轮明确要求做" },
      { id: "file", type: "file", mime: "text/plain", url: "file:///original-current-file" }]),
  ];
  const before = JSON.stringify(original), inference = { messages: structuredClone(original) };
  await transform({}, inference);
  assert.equal(JSON.stringify(original), before);
  assert.deepEqual(inference.messages[2], original[2]);
  assert.deepEqual(inference.messages[1].parts, []);
  const index = indexBody(inference.messages[0].parts[0]);
  assert.equal(index.omitted_user_parts, 1);
  assert.equal(index.omitted_summary_parts, 1);
  assert.equal(index.source_boundary.first.part_id, partID("preference"));
  assert.equal(index.source_boundary.last.part_id, partID("summary"));
  assert.equal(index.read.arguments.section, "text");
  assert.equal(index.input_read, undefined);
  const proof = (await diagnostic()).projection;
  assert.equal(proof.historical_message_budget_bytes, 65536);
  assert.ok(proof.historical_tool_history_bytes_after <= 65536);
  assert.ok(proof.omitted_historical_text_bytes > 150000);
  const once = structuredClone(inference.messages);
  await transform({}, inference);
  assert.deepEqual(inference.messages, once);
  assert.equal(indexBody(inference.messages[0].parts[0]).omitted_user_parts, 1);
  assert.equal(indexBody(inference.messages[0].parts[0]).omitted_summary_parts, 1);
});

test("without a declared history tool the original pair remains and the SDK refuses an unmet historical allowance", async t => {
  const { hooks, transform, diagnostic } = await fixture(t, { formal_tools: ["grove_world_grove_interact"] });
  const original = [message("only-error", "assistant", [{ id: "recoverable-input", type: "tool",
    tool: "grove_world_grove_interact", callID: "actual-call", state: { status: "error",
      input: { authored: "x".repeat(80000) }, error: "Actual failed attempt" } }],
    { error: { name: "APIError", data: { message: "Native provider failure" } } }),
    message("current", "user", [{ id: "current", type: "text", text: "本轮输入" }])];
  const inference = { messages: structuredClone(original) };
  await transform({}, inference);
  assert.deepEqual(inference.messages, original);
  const proof = (await diagnostic()).projection;
  assert.equal(proof.history_index_unavailable_parts, 1);
  assert.equal(proof.history_recovery_tool_available, false);
  assert.equal(proof.history_recovery_unavailable_parts, 1);
  assert.equal(proof.history_source_index_sent, false);
  assert.equal(proof.history_all_old_parts_omitted, false);
  assert.equal(proof.omitted_historical_tool_parts, 0);
  assert.ok(proof.managed_message_bytes_after > 65536);
  let dispatched = 0;
  const config = { model: "provider/model", provider: { provider: { options: {
    fetch: async () => { dispatched++; return new Response("must not dispatch"); } } } } };
  await hooks.config(config);
  await hooks["chat.headers"]({ agent: "shared-grove-executor", sessionID: "same-session",
    model: { providerID: "provider", id: "model" }, message: { id: messageID("current") } });
  assert.equal((await config.provider.provider.options.fetch("https://provider.invalid/messages", {
    method: "POST", body: JSON.stringify({ messages: [{ role: "user", content: "small actual body" }] }) })).status, 422);
  assert.equal(dispatched, 0);
  assert.equal((await diagnostic()).sdk_guard.rejection, "history_recovery_unavailable");
});

test("old invalid parts expose only source metadata while current invalid input remains untouched", async t => {
  const { hooks, transform, diagnostic } = await fixture(t);
  const invalid = { id: "old-invalid", type: "tool", tool: "invalid", callID: "actual-invalid-call",
    state: { status: "completed", input: { tool: "fabricated", error: "private validation text" },
      output: "unsafe raw validation output" } };
  const original = [message("old", "assistant", [invalid]),
    message("current", "user", [{ id: "intent", type: "text", text: "继续" }]),
    message("current-assistant", "assistant", [{ ...invalid, id: "current-invalid" }], { parentID: "current" })];
  const inference = { messages: structuredClone(original) };
  await transform({}, inference);
  assert.deepEqual(inference.messages.slice(1), original.slice(1));
  assert.equal(inference.messages[0].parts.length, 1);
  const index = indexBody(inference.messages[0].parts[0]);
  assert.equal(index.source_boundary.last.tool, "invalid");
  assert.equal(index.source_boundary.last.native_status, "completed");
  assert.equal(index.read, undefined);
  assert.equal(index.input_read, undefined);
  assert.equal(index.omitted_tool_parts, 1);
  assert.equal(JSON.stringify(index).includes("private validation text"), false);
  assert.equal(JSON.stringify(index).includes("unsafe raw validation output"), false);
  assert.equal((await diagnostic()).projection.current_tool_parts, 1);
  const once = structuredClone(inference.messages);
  await transform({}, inference);
  assert.deepEqual(inference.messages, once);

  // An error assistant cannot carry a visible index. Preserve this source copy,
  // but never send arbitrary invalid input.error even when all byte limits fit.
  const unavailable = { messages: [message("old-error", "assistant", [invalid],
    { error: { name: "APIError", data: { message: "Actual native failure" } } }),
    message("current", "user", [{ id: "intent", type: "text", text: "继续" }])] };
  const protectedOriginal = structuredClone(unavailable.messages);
  await transform({}, unavailable);
  assert.deepEqual(unavailable.messages, protectedOriginal);
  assert.equal((await diagnostic()).projection.history_index_unavailable_parts, 1);
  assert.ok((await diagnostic()).projection.managed_message_bytes_after < 65536);
  let dispatched = 0;
  const config = { model: "provider/model", provider: { provider: { options: {
    fetch: async () => { dispatched++; return new Response("must not dispatch"); } } } } };
  await hooks.config(config);
  await hooks["chat.headers"]({ agent: "shared-grove-executor", sessionID: "same-session",
    model: { providerID: "provider", id: "model" }, message: { id: messageID("current") } });
  const body = JSON.stringify({ messages: [{ role: "user", content: "small actual request" }] });
  assert.equal((await config.provider.provider.options.fetch("https://provider.invalid/messages",
    { method: "POST", body })).status, 422);
  assert.equal(dispatched, 0);
  assert.equal((await diagnostic()).sdk_guard.rejection, "history_source_index_unavailable");
  assert.equal(JSON.stringify(await diagnostic()).includes("private validation text"), false);
});

test("the configured SDK fetch measures the exact whole body and refuses excess without dispatch or role reuse", async t => {
  const { hooks, transform, diagnostic } = await fixture(t);
  const calls = [], otherFetch = async () => new Response("other");
  const delegate = async (url, init) => { calls.push({ url, init }); return new Response("actual delegate response"); };
  const config = { model: "provider/model", provider: {
    provider: { options: { fetch: delegate } }, other: { options: { fetch: otherFetch } } } };
  await hooks.config(config);
  const guarded = config.provider.provider.options.fetch;
  await hooks.config(config);
  assert.equal(config.provider.provider.options.fetch, guarded);
  assert.equal(config.provider.other.options.fetch, otherFetch);
  assert.equal((await diagnostic()).sdk_guard.status, "installed");
  assert.equal((await diagnostic()).sdk_guard.sent_count, 0);
  const arm = agent => hooks["chat.headers"]({ agent, sessionID: "same-session",
    model: { providerID: "provider", id: "model" }, message: { id: messageID("current") } });
  const small = { messages: [{ role: "user", content: "fresh" }], system: [{ text: "fixed" }], tools: [] };
  const body = JSON.stringify(small), init = { method: "POST", body,
    headers: { authorization: "Bearer TEST_ONLY_SENTINEL" } };
  const inference = { messages: [message("current", "user", [{ id: "intent", type: "text", text: "fresh" }])] };
  await transform({}, inference); await arm("shared-grove-executor");
  assert.equal((await guarded("https://provider.invalid/messages", init)).status, 200);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].init, init);
  let proof = await diagnostic();
  assert.equal(proof.sdk_guard.status, "sent");
  assert.equal(proof.sdk_guard.body_bytes, Buffer.byteLength(body));
  assert.equal(proof.sdk_guard.body_sha256, createHash("sha256").update(body).digest("hex"));
  assert.equal(proof.sdk_guard.system_bytes, Buffer.byteLength(JSON.stringify(small.system)));
  assert.equal(proof.sdk_guard.messages_bytes, Buffer.byteLength(JSON.stringify(small.messages)));
  assert.equal(proof.sdk_guard.tools_bytes, 2);
  assert.equal(proof.sdk_guard.current_request_message_id, inference.messages[0].info.id);
  assert.equal(JSON.stringify(proof).includes("TEST_ONLY_SENTINEL"), false);

  // The current request does not share the old history's 64KiB cap.
  const hugeCurrent = [message("current", "user", [{ id: "intent", type: "text", text: "x".repeat(70000) },
    { id: "ignored-current", type: "text", ignored: true, text: "Still retained in the current native copy" }])];
  const protectedInference = { messages: structuredClone(hugeCurrent) };
  await transform({}, protectedInference); await arm("shared-grove-executor");
  assert.deepEqual(protectedInference.messages, hugeCurrent);
  const currentBody = JSON.stringify({ ...small, messages: [{ role: "user", content: "x".repeat(70000) }] });
  assert.equal((await guarded("https://provider.invalid/messages", { ...init, body: currentBody })).status, 200);
  assert.equal(calls.length, 2);
  proof = await diagnostic();
  assert.equal(proof.sdk_guard.status, "sent");
  assert.equal(proof.sdk_guard.rejection, null);
  assert.ok(proof.projection.protected_message_bytes > 65536);
  assert.ok(proof.projection.managed_message_bytes_after > 65536);
  assert.equal(proof.projection.managed_message_budget_bytes, undefined);
  assert.ok(proof.projection.historical_tool_history_bytes_after <= 65536);

  const overCurrent = { messages: [message("current", "user", [{ id: "intent", type: "text", text: "x".repeat(140000) }])] };
  const exactCurrent = structuredClone(overCurrent.messages);
  await transform({}, overCurrent); await arm("shared-grove-executor");
  assert.deepEqual(overCurrent.messages, exactCurrent);
  const overCurrentBody = JSON.stringify({ ...small, messages: [{ role: "user", content: "x".repeat(140000) }] });
  assert.equal((await guarded("https://provider.invalid/messages", { ...init, body: overCurrentBody })).status, 422);
  assert.equal((await diagnostic()).sdk_guard.rejection, "provider_json_over_budget");
  assert.equal(calls.length, 2);

  await transform({}, inference); await arm("shared-grove-executor");
  const hugeSystem = JSON.stringify({ ...small, system: [{ text: "x".repeat(140000) }] });
  assert.equal((await guarded("https://provider.invalid/messages", { method: "POST", body: hugeSystem })).status, 422);
  assert.equal((await diagnostic()).sdk_guard.rejection, "provider_json_over_budget");
  assert.equal(calls.length, 2);

  // Compaction/blueprint use the actual whole-body limit, not a 64KiB message cap.
  for (const role of ["compaction", "shared-grove-blueprint"]) {
    await arm(role);
    const hugeMessages = JSON.stringify({ ...small, messages: [{ role: "user", content: "x".repeat(70000) }] });
    assert.equal((await guarded("https://provider.invalid/messages", { method: "POST", body: hugeMessages })).status, 200);
    assert.equal((await diagnostic()).sdk_guard.rejection, null);
    const overRequest = JSON.stringify({ ...small, messages: [{ role: "user", content: "x".repeat(140000) }] });
    assert.equal((await guarded("https://provider.invalid/messages", { method: "POST", body: overRequest })).status, 422);
    assert.equal((await diagnostic()).sdk_guard.rejection, "provider_json_over_budget");
  }
  assert.equal(calls.length, 4);
  await arm("shared-grove-executor");
  assert.equal((await guarded("https://provider.invalid/messages", { method: "POST", body: "unrecognized body" })).status, 422);
  assert.equal((await diagnostic()).sdk_guard.rejection, "unsupported_sdk_json");
  assert.equal(calls.length, 4);
  await arm("shared-grove");
  assert.equal((await guarded("https://provider.invalid/messages", init)).status, 422);
  assert.equal((await diagnostic()).sdk_guard.rejection, "managed_projection_unavailable");
  assert.equal(calls.length, 4);
});

test("the managed hook refuses a Native LLM runtime that bypasses the SDK guard", async t => {
  const { options } = await fixture(t);
  const previous = process.env.OPENCODE_EXPERIMENTAL_NATIVE_LLM;
  try {
    process.env.OPENCODE_EXPERIMENTAL_NATIVE_LLM = "true";
    await assert.rejects(() => GroveNativeContext({}, options), /does not admit the native LLM runtime/);
  } finally {
    if (previous === undefined) delete process.env.OPENCODE_EXPERIMENTAL_NATIVE_LLM;
    else process.env.OPENCODE_EXPERIMENTAL_NATIVE_LLM = previous;
  }
});

test("complete current catalog and preflight results above 64KiB coexist with a bounded historical suffix and actual SDK overhead", async t => {
  const { hooks, transform, diagnostic } = await fixture(t);
  const tool = (id, padding) => ({ id, type: "tool", tool: "grove_world_grove_definition_read", callID: `call-${id}`,
    state: { status: "completed", input: { definition_ref: `original-${id}` }, output: JSON.stringify({
      result: { definition_ref: `original-${id}`, padding: "x".repeat(padding) } }) } });
  const original = [message("old-person", "user", [{ id: "preference", type: "text", text: "按原偏好，保留引用" }]),
    message("old-tools", "assistant", [tool("old-large", 22000), tool("recent-one", 4000), tool("recent-two", 4000)]),
    message("current", "user", [{ id: "intent", type: "text", text: "按当前目录和preflight继续" },
      { id: "facts", type: "text", text: "Exact machine facts header\n{current:true}" }]),
    message("current-tools", "assistant", [tool("catalog", 39000), tool("preflight", 39000)], { parentID: "current" })];
  const originalText = JSON.stringify(original), inference = { messages: structuredClone(original) };
  await transform({}, inference);
  assert.equal(JSON.stringify(original), originalText);
  assert.deepEqual(inference.messages.slice(2), original.slice(2));
  const projection = (await diagnostic()).projection;
  assert.ok(projection.protected_message_bytes > 65536);
  assert.ok(projection.managed_message_bytes_after > 65536);
  assert.equal(projection.historical_message_budget_bytes, 65536);
  assert.equal(projection.fixed_request_reserve_bytes, 32768);
  assert.equal(projection.historical_message_allowance_bytes,
    Math.min(65536, Math.max(0, 131072 - 32768 - projection.protected_message_bytes)));
  assert.ok(projection.historical_tool_history_bytes_after <= projection.historical_message_allowance_bytes);
  assert.equal(projection.omitted_historical_tool_parts, 1);
  assert.equal(projection.retained_historical_tool_parts, 2);
  assert.ok(projection.historical_tool_index_bytes > 0);
  const oldParts = inference.messages[1].parts;
  assert.equal(indexBody(oldParts[0]).source_boundary.last.part_id, partID("old-large"));
  assert.deepEqual(oldParts.slice(1).map(part => part.state.input), original[1].parts.slice(1).map(part => part.state.input));
  assert.deepEqual(oldParts.slice(1).map(part => part.callID), original[1].parts.slice(1).map(part => part.callID));
  const once = structuredClone(inference.messages);
  await transform({}, inference);
  assert.deepEqual(inference.messages, once);

  let dispatched = 0;
  const config = { model: "provider/model", provider: { provider: { options: {
    fetch: async () => { dispatched++; return new Response("mock SDK delegate"); } } } } };
  await hooks.config(config);
  await hooks["chat.headers"]({ agent: "shared-grove-executor", sessionID: "same-session",
    model: { providerID: "provider", id: "model" }, message: { id: messageID("current") } });
  const body = JSON.stringify({ model: "model", system: [{ type: "text", text: "s".repeat(2100) }],
    tools: [{ name: "formal_tools", description: "t".repeat(28000), input_schema: { type: "object" } }],
    messages: [{ role: "user", content: JSON.stringify(inference.messages.slice(0, 2).map(item => item.parts)) },
      { role: "user", content: original[2].parts[0].text },
      { role: "user", content: original[3].parts.map(part => ({ type: "tool_result", tool_use_id: part.callID, content: part.state.output })) }] });
  assert.ok(Buffer.byteLength(body) > 96 * 1024);
  assert.ok(Buffer.byteLength(body) <= 128 * 1024);
  assert.equal((await config.provider.provider.options.fetch("https://provider.invalid/messages", { method: "POST", body })).status, 200);
  assert.equal(dispatched, 1);
  const sent = (await diagnostic()).sdk_guard;
  assert.equal(sent.status, "sent");
  assert.equal(sent.body_bytes, Buffer.byteLength(body));
  assert.equal(sent.body_sha256, createHash("sha256").update(body).digest("hex"));
  assert.equal(sent.historical_message_budget_bytes, 65536);
  assert.equal(sent.message_budget_bytes, undefined);
});

test("historical selection counts compacted output by its actual Native placeholder", async t => {
  const { transform, diagnostic } = await fixture(t);
  const tool = (id, padding, time = {}) => ({ id, type: "tool", tool: "grove_world_grove_definition_read", callID: id,
    state: { status: "completed", input: { definition_ref: id }, output: JSON.stringify({ padding: "x".repeat(padding) }), time } });
  const original = [message("old-tools", "assistant", [tool("old-large", 80000), tool("recent-compacted", 500000, { compacted: 3 })]),
    message("current", "user", [{ id: "intent", type: "text", text: "x".repeat(80000) }])];
  const inference = { messages: structuredClone(original) };
  await transform({}, inference);
  assert.deepEqual(inference.messages[1], original[1]);
  assert.equal(indexBody(inference.messages[0].parts[0]).omitted_tool_parts, 1);
  assert.deepEqual(inference.messages[0].parts[1], original[0].parts[1]);
  const projection = (await diagnostic()).projection;
  assert.equal(projection.retained_historical_tool_parts, 1);
  assert.equal(projection.historical_tool_output_bytes_after, Buffer.byteLength("[Old tool result content cleared]"));
  assert.ok(projection.retained_historical_tool_pair_bytes < 4096);
  assert.ok(projection.historical_tool_history_bytes_after <= projection.historical_message_allowance_bytes);
});

test("the formal list entrance permits old sources to clear even without a visible index owner", async t => {
  const { transform, diagnostic } = await fixture(t);
  const original = [message("old-error", "assistant", [{ id: "original", type: "tool",
    tool: "grove_world_grove_interact", state: { status: "error", input: { authored: "x".repeat(55000) }, error: "Actual failed attempt" } }],
    { error: { name: "APIError", data: { message: "Native provider failure" } } }),
    message("current", "user", [{ id: "intent", type: "text", text: "x".repeat(78000) }])];
  const inference = { messages: structuredClone(original) };
  await transform({}, inference);
  assert.deepEqual(inference.messages[0].parts, []);
  assert.deepEqual(inference.messages[1], original[1]);
  const projection = (await diagnostic()).projection;
  assert.equal(projection.historical_tool_history_bytes_after, 0);
  assert.ok(projection.historical_tool_history_bytes_after <= projection.historical_message_allowance_bytes);
  assert.equal(projection.history_index_unavailable_parts, 0);
  assert.equal(projection.historical_tool_index_bytes, 0);
  assert.equal(projection.history_recovery_tool_available, true);
  assert.equal(projection.history_all_old_parts_omitted, true);
});

test("an adaptive zero allowance omits the complete old index without touching current parts or manufacturing another index", async t => {
  const { hooks, transform, diagnostic } = await fixture(t);
  const original = [message("old", "assistant", [{ id: "old-call", type: "tool", tool: "grove_world_grove_interact",
    callID: "actual-old-call", state: { status: "completed", input: { authored: "x".repeat(80000) },
      output: JSON.stringify({ operation_id: "actual-old-operation" }) } }]),
    // Parent binding also protects current parts before the current user's position.
    message("current-before-user", "assistant", [{ id: "current-call", type: "tool", tool: "grove_world_grove_interact",
      callID: "actual-current-call", state: { status: "completed", input: { target: "current" },
        output: JSON.stringify({ actual: "current-result" }) } }], { parentID: "current" }),
    message("current", "user", [{ id: "intent", type: "text", text: "continue" },
      { id: "ignored-current", type: "text", ignored: true, text: "Keep the full current native input" }]),
  ];
  const originalText = JSON.stringify(original), inference = { messages: structuredClone(original) };
  await transform({}, inference);
  assert.ok(indexBody(inference.messages[0].parts[0]).omitted_tool_parts > 0);
  const current = inference.messages[2];
  current.parts[0].text = "x".repeat(100000);
  const completeCurrent = structuredClone(inference.messages.slice(1));
  await transform({}, inference);
  let projection = (await diagnostic()).projection;
  assert.equal(projection.historical_message_allowance_bytes, 0);
  assert.equal(projection.historical_tool_history_bytes_after, 0);
  assert.equal(projection.historical_tool_index_bytes, 0);
  assert.equal(projection.history_source_index_sent, false);
  assert.equal(projection.history_all_old_parts_omitted, true);
  assert.equal(projection.history_recovery_unavailable_parts, 0);
  assert.deepEqual(inference.messages[0].parts, []);
  assert.deepEqual(inference.messages.slice(1), completeCurrent);
  assert.equal(JSON.stringify(original), originalText);
  const once = structuredClone(inference.messages), sameArray = inference.messages;
  await transform({}, inference);
  assert.equal(inference.messages, sameArray);
  assert.deepEqual(inference.messages, once);
  projection = (await diagnostic()).projection;
  assert.equal(projection.historical_tool_history_bytes_after, 0);
  assert.equal(projection.historical_tool_index_bytes, 0);
  assert.equal(projection.budget_omitted_historical_parts, 0);

  let dispatched = 0;
  const config = { model: "provider/model", provider: { provider: { options: {
    fetch: async () => { dispatched++; return new Response("actual delegate"); } } } } };
  await hooks.config(config);
  const arm = () => hooks["chat.headers"]({ agent: "shared-grove-executor", sessionID: "same-session",
    model: { providerID: "provider", id: "model" }, message: { id: current.info.id } });
  await arm();
  const body = JSON.stringify({ messages: [{ role: "user", content: current.parts[0].text }],
    system: [{ text: "s".repeat(2100) }], tools: [{ name: "formal-tools", description: "t".repeat(28000) }] });
  assert.ok(Buffer.byteLength(body) <= 131072);
  assert.equal((await config.provider.provider.options.fetch("https://provider.invalid/messages", { method: "POST", body })).status, 200);
  assert.equal(dispatched, 1);
  current.parts[0].text = "x".repeat(140000);
  const overCurrent = structuredClone(inference.messages.slice(1));
  await transform({}, inference); await arm();
  assert.deepEqual(inference.messages.slice(1), overCurrent);
  const overBody = JSON.stringify({ messages: [{ role: "user", content: current.parts[0].text }] });
  assert.equal((await config.provider.provider.options.fetch("https://provider.invalid/messages", { method: "POST", body: overBody })).status, 422);
  assert.equal((await diagnostic()).sdk_guard.rejection, "provider_json_over_budget");
  assert.equal(dispatched, 1);
});

test("unknown old error pairs and index envelopes clear together under a tiny allowance without changing original sources", async t => {
  const { transform, diagnostic } = await fixture(t);
  const original = [message("old", "assistant", [{ id: "formal-old", type: "tool", tool: "grove_world_grove_interact",
    callID: "formal-old-call", state: { status: "completed", input: { authored: "x".repeat(80000) }, output: "original output" } },
    { id: "unknown-old", type: "tool", tool: "grove_world_grove_world_bootstrap", callID: "unknown-old-call",
      state: { status: "error", input: {}, error: "Actual stored error".repeat(250) } }]),
    message("current", "user", [{ id: "intent", type: "text", text: "continue" }])];
  const originalText = JSON.stringify(original), inference = { messages: structuredClone(original) };
  await transform({}, inference);
  assert.equal(inference.messages[0].parts.length, 2);
  indexBody(inference.messages[0].parts[0]);
  assert.deepEqual(inference.messages[0].parts[1], original[0].parts[1]);
  const protectedOverhead = (await diagnostic()).projection.protected_message_bytes - Buffer.byteLength("continue");
  inference.messages[1].parts[0].text = "x".repeat(131072 - 32768 - protectedOverhead - 311);
  const completeCurrent = structuredClone(inference.messages[1]);
  await transform({}, inference);
  const projection = (await diagnostic()).projection;
  assert.equal(projection.historical_message_allowance_bytes, 311);
  assert.equal(projection.historical_tool_history_bytes_after, 0);
  assert.equal(projection.historical_tool_index_bytes, 0);
  assert.equal(projection.budget_omitted_historical_parts, 2);
  assert.equal(projection.budget_omitted_historical_tool_parts, 1);
  assert.equal(projection.budget_omitted_unsupported_historical_tool_parts, 1);
  assert.equal(projection.retained_historical_tool_parts, 0);
  assert.equal(projection.unfoldable_historical_tool_parts, 0);
  assert.equal(projection.history_source_index_sent, false);
  assert.equal(projection.history_recovery_tool_available, true);
  assert.ok(projection.output_byte_meaning.includes("their bodies are not promised readable"));
  assert.deepEqual(inference.messages[0].parts, []);
  assert.deepEqual(inference.messages[1], completeCurrent);
  assert.equal(JSON.stringify(original), originalText);
  const once = structuredClone(inference.messages);
  await transform({}, inference);
  assert.deepEqual(inference.messages, once);
  assert.equal((await diagnostic()).projection.historical_tool_history_bytes_after, 0);
});
