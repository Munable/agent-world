// Only inference copies and the configured provider's official SDK fetch are managed.
// Native DB records, original inputs, current user and current tool round stay intact.
import { createHash, randomBytes } from "node:crypto";
import { readFile, writeFile, rename, lstat } from "node:fs/promises";
import { fileURLToPath } from "node:url";

const HOOK = "experimental.chat.messages.transform", VERSION = 2;
const AGENTS = new Set(["shared-grove", "shared-grove-executor"]);
const GROVE_AGENTS = new Set([...AGENTS, "shared-grove-blueprint", "compaction"]);
const SUMMARY_PREFIX = "[Grove历史原生摘要：仅作背景，不证明本轮行动或当前世界状态。"
  + "本轮确认动作只按当前task_action_summary的task_id与coverage判断。原偏好、引用和原文保留如下。]\n";
const USER_PREFIX = "[Grove historical user request: prior intent only; it grants no current control or task continuation.]\n";
const REQUEST_CONTRACT_PREFIX = "[Grove current request contract]";
const TOOL_PREFIX = "[Grove historical completed tool result: ";
const INDEX_PREFIX = "[Grove native historical tool location index]\n";
const HISTORY_BUDGET = 64 * 1024, REQUEST_BUDGET = 128 * 1024, FIXED_REQUEST_RESERVE = 32 * 1024, INDEX_MAX = 4096;
const MESSAGE_ID = /^msg_[A-Za-z0-9]{1,100}$/, PART_ID = /^prt_[A-Za-z0-9]{1,100}$/;
const CLEARED = "[Old tool result content cleared]";
const bytes = value => Buffer.byteLength(value, "utf8");
const jsonBytes = value => bytes(JSON.stringify(value) ?? "");
const inputText = part => JSON.stringify(part.state?.input) ?? "";
const outputText = part => part.state?.status === "completed"
  ? part.state.time?.compacted ? CLEARED : part.state.output ?? ""
  : part.state?.status === "error" ? part.state.error ?? "" : "[Tool execution was interrupted]";
const isSummary = message => message.info.role === "assistant" && message.info.summary === true;
const validSource = (message, part) => MESSAGE_ID.test(message.info.id) && PART_ID.test(part.id);
const inputObject = part => part.state?.input && typeof part.state.input === "object" && !Array.isArray(part.state.input);
function requestContract(message) {
  const part = message.parts.findLast(part => part.type === "text" && typeof part.text === "string" &&
    part.text.startsWith(REQUEST_CONTRACT_PREFIX + "\n"));
  if (!part) return null;
  try {
    const value = JSON.parse(part.text.slice(REQUEST_CONTRACT_PREFIX.length + 1));
    const execution = value.purpose === "execution";
    if (!value || value.session_id !== message.info.sessionID || !/^grove-request-[a-f0-9]{32}$/.test(value.request_id) ||
        !["entry", "explain", "execution"].includes(value.purpose) || value.world_write_authorized !== execution ||
        message.info.agent !== (execution ? "shared-grove-executor" : "shared-grove") ||
        execution && (!/^grove-task-[a-f0-9]{32}$/.test(value.task_id) || !/^grove-segment-[a-f0-9]{32}$/.test(value.segment_id))) return null;
    return value;
  } catch { return null; }
}
function objectOutput(part) {
  try {
    const value = JSON.parse(part.state?.output);
    return value && typeof value === "object" && !Array.isArray(value) ? value : null;
  } catch { return null; }
}

// A conservative native inference document, not a wire/token estimate. SDK fetch
// separately counts the actual complete provider JSON (including system/tools).
function projectionBytes(messages) {
  const selected = messages.flatMap(message => {
    const parts = message.parts.filter(part => ["text", "reasoning", "tool", "file"].includes(part.type) &&
      !(message.info.role === "user" && part.ignored));
    return parts.length ? [{ role: message.info.role, parts: parts.map(part =>
      part.type === "tool" && part.state?.time?.compacted ? { ...part,
        state: { ...part.state, output: CLEARED, attachments: undefined } } : part) }] : [];
  });
  return selected.length ? jsonBytes(selected) : 0;
}

function historicalIndex(part, sessionID) {
  if (part.type !== "text" || part.synthetic !== true || typeof part.text !== "string" || !part.text.startsWith(INDEX_PREFIX)) return null;
  try {
    const index = JSON.parse(part.text.slice(INDEX_PREFIX.length));
    const required = ["omitted_tool_parts", "omitted_input_bytes", "omitted_output_bytes", "omitted_output_characters"];
    const optional = ["omitted_user_parts", "omitted_summary_parts", "omitted_text_bytes"];
    if (index.scope !== "historical_tool_location_index" || index.historical_only !== true || index.session_id !== sessionID ||
        bytes(part.text) > INDEX_MAX || index.list?.tool !== "native_tool_history" || index.list?.arguments?.mode !== "list" ||
        required.some(key => !Number.isSafeInteger(index[key]) || index[key] < 0) ||
        optional.some(key => index[key] !== undefined && (!Number.isSafeInteger(index[key]) || index[key] < 0))) return null;
    return index;
  } catch { return null; }
}
function anchor(item) {
  const { message, part, kind } = item;
  const value = { message_id: message.info.id, part_id: part.id, kind };
  if (kind === "tool") value.native_status = part.state.status;
  for (const [key, content] of Object.entries({ tool: part.tool, call_id: part.callID,
    operation_id: part.tool === "invalid" ? undefined : objectOutput(part)?.operation_id })) {
    if (typeof content === "string" && bytes(content) <= 256) value[key] = content;
  }
  return value;
}
function omissionTotals(omitted, existing) {
  const totals = {};
  for (const [key, kind, field] of [
    ["omitted_tool_parts", "tool"], ["omitted_user_parts", "user"], ["omitted_summary_parts", "summary"],
    ["omitted_input_bytes", null, "inputBytes"], ["omitted_output_bytes", null, "outputBytes"],
    ["omitted_output_characters", null, "outputCharacters"], ["omitted_text_bytes", null, "textBytes"],
  ]) totals[key] = (existing?.[key] ?? 0) + omitted.reduce((sum, item) => sum + (kind ? Number(item.kind === kind) : item[field] ?? 0), 0);
  return totals;
}
function locationIndex(sessionID, omitted, existing) {
  const first = existing?.source_boundary?.first ?? anchor(omitted[0]);
  const last = omitted.length ? anchor(omitted.at(-1)) : existing.source_boundary.last;
  const index = { scope: "historical_tool_location_index", historical_only: true, session_id: sessionID,
    meaning: "Source locations only, not original result bodies, current state or evidence of this task's execution. Native tool status is delivery status, not a world outcome. Recover recorded formal inputs or original user/summary text through native_tool_history; invalid parts expose metadata only, and unsafe raw outputs/errors may be unavailable. Read current world facts before acting.",
    source_boundary: { first, last }, list: { tool: "native_tool_history", arguments: { mode: "list", kind: "all" } } };
  Object.assign(index, omissionTotals(omitted, existing));
  const read = { mode: "read", message_id: last.message_id, part_id: last.part_id };
  if (last.tool !== "invalid") {
    index.read = { tool: "native_tool_history", arguments: { ...read, ...(last.kind && last.kind !== "tool" ? { section: "text" } : {}) } };
    if (!last.kind || last.kind === "tool") index.input_read = { tool: "native_tool_history", arguments: { ...read, section: "input" } };
  }
  const text = INDEX_PREFIX + JSON.stringify(index);
  if (bytes(text) > INDEX_MAX) throw new Error("Native history source index exceeds its budget");
  return { index, text };
}
async function writeDiagnostic(path, value) {
  try { if ((await lstat(path)).isSymbolicLink()) throw new Error("Linked context diagnostic"); }
  catch (error) { if (error.code !== "ENOENT") throw error; }
  const temporary = `${path}.${randomBytes(8).toString("hex")}.tmp`;
  await writeFile(temporary, JSON.stringify(value), { mode: 0o600, flag: "wx" });
  await rename(temporary, path);
}
function historicalOutput(part, messageID, sessionID, tools) {
  if (!tools.has(part.tool) || part.state?.status !== "completed" || part.state.time?.compacted || typeof part.state.output !== "string") return part;
  const original = { session_id: sessionID, message_id: messageID, part_id: part.id };
  const historical = { historical_only: true,
    meaning: "Historical result, not current state. Only current task_action_summary with matching task_id and coverage establishes this task's actions.",
    original, read: { tool: "native_tool_history", arguments: { mode: "read", message_id: messageID, part_id: part.id } } };
  const value = objectOutput(part);
  if (!value) return part.state.output.startsWith(TOOL_PREFIX) ? part : { ...part,
    state: { ...part.state, output: `${TOOL_PREFIX}${JSON.stringify({ ...historical,
      scope: "historical_unstructured_tool_result", original_body_follows_unchanged: true })}]\n${part.state.output}` } };
  const previous = value.native_context_projection;
  if (previous?.historical_only === true && previous.original?.session_id === sessionID && previous.original?.message_id === messageID &&
      previous.original?.part_id === part.id && ["historical_tool_result", "historical_tool_result_without_later_scene"].includes(previous.scope)) return part;
  value.native_context_projection = { ...historical, scope: "historical_tool_result", omitted_sections: [] };
  return { ...part, state: { ...part.state, output: JSON.stringify(value) } };
}

export default async function GroveNativeContext(_input, options = {}) {
  const sourceSha = createHash("sha256").update(await readFile(fileURLToPath(import.meta.url))).digest("hex");
  if (options.version !== VERSION || sourceSha !== options.source_sha256 || typeof options.nonce !== "string" || !/^[a-f0-9]{32}$/.test(options.nonce) ||
      typeof options.role_id !== "string" || !options.role_id || typeof options.session_id !== "string" ||
      typeof options.model !== "string" || !/^[^/]+\/.+$/.test(options.model) ||
      !Array.isArray(options.machine_context_prefixes) || options.machine_context_prefixes.some(prefix => typeof prefix !== "string" || !prefix) ||
      typeof options.diagnostic_path !== "string" || !options.diagnostic_path.startsWith("/") ||
      !Array.isArray(options.formal_tools) || options.formal_tools.some(tool => typeof tool !== "string")) throw new Error("Invalid managed Grove context hook");
  const slash = options.model.indexOf("/"), providerID = options.model.slice(0, slash), modelID = options.model.slice(slash + 1);
  const tools = new Set([...options.formal_tools, "invalid"]);
  const historyAvailable = tools.has("grove_world_native_tool_history") || tools.has("native_tool_history");
  const runtime = process.env.OPENCODE_EXPERIMENTAL_NATIVE_LLM;
  if (runtime !== undefined && !["false", "0", "no", "off"].includes(runtime.toLowerCase())) throw new Error("Grove SDK guard does not admit the native LLM runtime");
  const proof = { version: VERSION, status: "loaded", hook: HOOK, source_sha256: sourceSha, nonce: options.nonce,
    role_id: options.role_id, process_id: process.pid, loaded_at_ms: Date.now() };
  let projection = null, request = null, sequence = 0, ownedFetch = null;
  // SDK envelopes vary by provider. Learn their measured non-message payload
  // and any positive message conversion expansion only for the same native
  // session/agent; the actual fetch still independently enforces both limits.
  const sdkEnvelopes = new Map();
  const envelopeKey = (sessionID, agent) => `${sessionID}\0${agent}`;
  let guard = { status: "not_installed", runtime: "ai_sdk", provider_id: providerID, model_id: modelID,
    historical_message_budget_bytes: HISTORY_BUDGET, request_budget_bytes: REQUEST_BUDGET, native_llm_allowed: false,
    attempt_count: 0, sent_count: 0, blocked_count: 0, yielded_count: 0 };
  let queue = Promise.resolve();
  const diagnostic = () => {
    const value = { ...proof, sdk_guard: { ...guard }, ...(projection ? { projection: { ...projection } } : {}) };
    queue = queue.then(() => writeDiagnostic(options.diagnostic_path, value)); return queue;
  };
  await diagnostic();
  return {
    config: async config => {
      if (config.model !== options.model) throw new Error("Grove SDK guard model configuration mismatch");
      const provider = (config.provider ??= {})[providerID] ??= {};
      const providerOptions = provider.options ??= {};
      if (ownedFetch && providerOptions.fetch === ownedFetch) { await diagnostic(); return; }
      const delegate = providerOptions.fetch ?? globalThis.fetch;
      if (typeof delegate !== "function") throw new Error("Grove SDK guard fetch unavailable");
      providerOptions.fetch = async (url, init) => {
        if (!request || !GROVE_AGENTS.has(request.agent)) throw new Error("Grove SDK guard request association unavailable");
        guard = { ...guard, ...request, observed_at_ms: Date.now(), attempt_count: guard.attempt_count + 1 };
        let body;
        try { body = typeof init?.body === "string" ? JSON.parse(init.body) : null; } catch { body = null; }
        const supported = body && typeof body === "object" && Array.isArray(body.messages) && (init?.method ?? "POST").toUpperCase() === "POST";
        guard = { ...guard, body_bytes: typeof init?.body === "string" ? bytes(init.body) : 0,
          body_sha256: typeof init?.body === "string" ? createHash("sha256").update(init.body).digest("hex") : "",
          system_bytes: supported ? jsonBytes(body.system) : 0, messages_bytes: supported ? jsonBytes(body.messages) : 0,
          tools_bytes: supported ? jsonBytes(body.tools) : 0 };
        const matched = AGENTS.has(request.agent) && projection?.agent === request.agent &&
          projection.session_id === request.session_id && projection.current_request_message_id === request.current_request_message_id;
        const projectionUnavailable = AGENTS.has(request.agent) && !matched;
        const recoveryUnavailable = matched && projection.history_recovery_unavailable_parts > 0;
        const sourceUnavailable = matched && projection.history_index_unavailable_parts > 0;
        const historyOver = matched && projection.historical_tool_history_bytes_after > HISTORY_BUDGET;
        const invalid = !supported || projectionUnavailable || recoveryUnavailable || sourceUnavailable || historyOver;
        if (!invalid && matched) {
          const key = envelopeKey(request.session_id, request.agent), previous = sdkEnvelopes.get(key);
          sdkEnvelopes.set(key, { fixedBytes: Math.max(0, guard.body_bytes - guard.messages_bytes),
            expansionBytes: Math.max(previous?.expansionBytes ?? 0,
              guard.messages_bytes - projection.managed_message_bytes_after, 0) });
        }
        // A local policy refusal ends this native POST without manufacturing a
        // model answer. Only an owned execution with complete tool pairs may
        // yield; ordinary responses and oversized first inputs still fail closed.
        const yielded = !invalid && request.agent === "shared-grove-executor" && projection.request_purpose === "execution" &&
          projection.world_write_authorized === true && projection.current_terminal_tool_parts > 0 &&
          projection.current_unfinished_tool_parts === 0 && guard.body_bytes >= REQUEST_BUDGET - FIXED_REQUEST_RESERVE;
        const rejected = invalid || !yielded && guard.body_bytes > REQUEST_BUDGET;
        const segmentYield = yielded ? { safe: true, reason: guard.body_bytes > REQUEST_BUDGET ? "request_over_budget" : "request_headroom",
          request_id: projection.request_id, task_id: projection.execution_task_id, segment_id: projection.execution_segment_id,
          current_terminal_tool_parts: projection.current_terminal_tool_parts,
          current_unfinished_tool_parts: projection.current_unfinished_tool_parts, body_bytes: guard.body_bytes } : null;
        guard = { ...guard, status: yielded ? "yielded" : rejected ? "blocked" : "sent", segment_yield: segmentYield,
          rejection: yielded ? "execution_segment_budget" : !supported ? "unsupported_sdk_json" :
          projectionUnavailable ? "managed_projection_unavailable" : recoveryUnavailable ? "history_recovery_unavailable" :
          sourceUnavailable ? "history_source_index_unavailable" :
          historyOver ? "historical_messages_over_budget" :
          guard.body_bytes > REQUEST_BUDGET ? "provider_json_over_budget" : null,
          blocked_count: guard.blocked_count + Number(rejected), yielded_count: guard.yielded_count + Number(yielded),
          sent_count: guard.sent_count + Number(!rejected && !yielded) };
        await diagnostic();
        if (yielded) return new Response(JSON.stringify({ type: "error", error: { type: "grove_execution_segment_yield",
          message: "GroveExecutionSegmentYield: 本段已到上下文接续边界；未发送本次Provider请求，原工具记录和已成立结果保留。" } }),
          { status: 422, headers: { "content-type": "application/json" } });
        if (rejected) return new Response(JSON.stringify({ type: "error", error: { type: "grove_context_budget_exceeded",
          message: "GroveContextBudgetExceeded: 原会话上下文超过字节预算或SDK格式无法核验；原记录和已确认行动保留，未发送本次Provider请求。" } }),
          // Native classifies every 413 as context overflow and may compact.
          // 422 is a nonretryable policy refusal with no overflow fallback.
          { status: 422, headers: { "content-type": "application/json" } });
        return delegate(url, init);
      };
      ownedFetch = providerOptions.fetch;
      guard = { ...guard, status: "installed", installed_at_ms: Date.now() }; await diagnostic();
    },
    "chat.headers": async input => {
      request = input.model?.providerID === providerID && input.model?.id === modelID && GROVE_AGENTS.has(input.agent) &&
        typeof input.sessionID === "string" && typeof input.message?.id === "string" ? {
          agent: input.agent, session_id: input.sessionID,
          current_request_message_id: input.message.id, request_sequence: ++sequence } : null;
    },
    [HOOK]: async (_hookInput, output) => {
      const messages = output.messages;
      if (!Array.isArray(messages)) throw new Error("Invalid native messages");
      const current = messages.findLast(message => message?.info?.role === "user");
      if (!current || !AGENTS.has(current.info.agent) || current.parts.some(part => part.type === "compaction")) return;
      const sessionID = current.info.sessionID;
      if (typeof sessionID !== "string" || typeof current.info.id !== "string" || (options.session_id && sessionID !== options.session_id) ||
          messages.some(message => message?.info?.sessionID !== sessionID || !Array.isArray(message.parts) ||
            message.parts.some(part => part.sessionID !== sessionID || part.messageID !== message.info.id))) throw new Error("Mixed native session in Grove context hook");
      const currentPosition = messages.indexOf(current);
      const contract = requestContract(current);
      const isCurrent = (message, position) => position >= currentPosition || message.info.parentID === current.info.id;
      const historicalBytes = projected => projectionBytes(projected.filter((message, position) => !isCurrent(message, position)));
      const protectedBytes = projectionBytes(messages.filter((message, position) => isCurrent(message, position)));
      // The complete current request is protected. Only the old source pool is
      // reduced before the SDK checks its actual JSON. Owned execution reserves
      // room under the same soft boundary used by fetch; recoverable old sources
      // must not force a yield immediately after each small current tool result.
      const ownedExecution = contract?.purpose === "execution" && contract.world_write_authorized === true;
      const projectionTarget = ownedExecution ? REQUEST_BUDGET - FIXED_REQUEST_RESERVE : REQUEST_BUDGET;
      const envelope = sdkEnvelopes.get(envelopeKey(sessionID, current.info.agent));
      const sdkFixedBytes = envelope?.fixedBytes ?? FIXED_REQUEST_RESERVE;
      const sdkExpansionBytes = envelope?.expansionBytes ?? 0;
      const historyAllowance = Math.min(HISTORY_BUDGET, Math.max(0,
        projectionTarget - sdkFixedBytes - sdkExpansionBytes - protectedBytes));
      const candidates = [], byPart = new Map(), base = new Map();
      let existing = null, proseParts = 0, proseCharacters = 0, snapshotParts = 0, snapshotCharacters = 0;
      for (const [position, message] of messages.entries()) {
        if (isCurrent(message, position)) { base.set(message, message.parts); continue; }
        const retained = [];
        for (const part of message.parts) {
          const index = historicalIndex(part, sessionID);
          if (index && !message.info.error) { if (existing) throw new Error("Multiple native historical location indexes"); existing = { message, part, index }; continue; }
          const machine = message.info.role === "user" && part.type === "text" && typeof part.text === "string" &&
            options.machine_context_prefixes.some(prefix => part.text.startsWith(`${prefix}\n`));
          if (machine || (message.info.role === "user" && part.ignored)) {
            if (machine && !part.ignored) { snapshotParts++; snapshotCharacters += part.text.length; } continue;
          }
          let kind = null, projected = part;
          if (validSource(message, part)) {
            if (message.info.role === "assistant" && part.type === "tool" && tools.has(part.tool) &&
                ["completed", "error"].includes(part.state?.status) && (part.tool === "invalid" || inputObject(part))) {
              kind = "tool"; projected = part.tool === "invalid" || !historyAvailable ? part : historicalOutput(part, message.info.id, sessionID, tools);
            } else if (part.type === "text" && typeof part.text === "string" && !part.synthetic && (message.info.role === "user" || isSummary(message))) {
              kind = message.info.role === "user" ? "user" : "summary";
              if (kind === "summary" && !part.text.startsWith(SUMMARY_PREFIX)) projected = { ...part, text: SUMMARY_PREFIX + part.text };
              if (kind === "user" && !part.text.startsWith(USER_PREFIX)) projected = { ...part, text: USER_PREFIX + part.text };
            }
          }
          if (kind) {
            const item = { message, part, projected, kind, inputBytes: kind === "tool" ? bytes(inputText(part)) : 0,
              outputBytes: kind === "tool" ? bytes(outputText(part)) : 0, outputCharacters: kind === "tool" ? outputText(part).length : 0,
              textBytes: kind === "tool" ? 0 : bytes(part.text) };
            candidates.push(item); byPart.set(part, item); continue;
          }
          if (!isSummary(message) && AGENTS.has(message.info.agent) && ["text", "reasoning"].includes(part.type)) {
            proseParts++; proseCharacters += typeof part.text === "string" ? part.text.length : 0; continue;
          }
          retained.push(part);
        }
        base.set(message, retained);
      }
      // Native invalid embeds arbitrary validation text in input.error; only its
      // source metadata is exposed, never a trusted recoverable formal input.
      const kept = new Set(candidates.filter(item => item.part.tool !== "invalid"));
      // Native drops some error assistant messages. Never put the only source
      // entrance there, even when their recorded structured inputs are readable.
      const safeOwner = (message, position) => !isCurrent(message, position) && !message.info.error &&
        !isSummary(message) && ["assistant", "user"].includes(message.info.role) && MESSAGE_ID.test(message.info.id);
      const indexOwner = (existing && safeOwner(existing.message, messages.indexOf(existing.message)) ? existing.message : null) ?? messages.find((message, position) =>
        message.info.role === "assistant" && safeOwner(message, position)) ?? messages.find(safeOwner);
      const indexPartID = existing?.part.id ?? `prt_grovehistory${createHash("sha256").update(sessionID).digest("hex").slice(0, 24)}`;
      if (!indexOwner || !historyAvailable) for (const item of candidates) kept.add(item);
      const assemble = index => {
        return messages.map((message, position) => {
          if (isCurrent(message, position)) return message;
          const parts = [];
          for (const part of message.parts) {
            if (part === existing?.part) continue;
            const item = byPart.get(part);
            if (item) {
              if (kept.has(item)) parts.push(item.projected);
            } else if (base.get(message).includes(part)) parts.push(part);
          }
          if (index && message === indexOwner) parts.unshift({ id: indexPartID, messageID: message.info.id,
            sessionID, type: "text", synthetic: true, text: index.text });
          return { ...message, parts };
        });
      };
      let omitted = candidates.filter(item => !kept.has(item));
      let index = !historyAvailable ? null : omitted.length ? locationIndex(sessionID, omitted, existing?.index) :
        existing ? { index: existing.index, text: existing.part.text } : null;
      let projected = assemble(index);
      const needsReduction = historicalBytes(projected) > historyAllowance && kept.size > 0;
      if (needsReduction && indexOwner && historyAvailable) {
        kept.clear();
        // Each independent envelope conservatively includes its role overhead.
        // Reserve the full index envelope once, avoiding quadratic serialization
        // of the session while choosing a complete recent source suffix.
        const allowance = Math.max(0, historyAllowance - historicalBytes(assemble(null)) - INDEX_MAX - 512);
        let retainedBytes = 0;
        for (let position = candidates.length - 1; position >= 0; position--) {
          const item = candidates[position];
          if (item.part.tool === "invalid") continue;
          const cost = projectionBytes([{ info: item.message.info, parts: [item.projected] }]);
          if (retainedBytes + cost > allowance) break;
          kept.add(item); retainedBytes += cost;
        }
        omitted = candidates.filter(item => !kept.has(item));
        index = omitted.length ? locationIndex(sessionID, omitted, existing?.index) :
          existing ? { index: existing.index, text: existing.part.text } : null;
        projected = assemble(index);
      }
      let budgetOmitted = [];
      if (historicalBytes(projected) > historyAllowance && historyAvailable) {
        // The formal history tool already exposes same-session mode=list. When
        // even an index or unsupported old envelope cannot fit, omit all old
        // inference parts rather than adding another placeholder. Native skips
        // empty parts; removing each whole tool part removes call/result together.
        budgetOmitted = projected.flatMap((message, position) => isCurrent(message, position) ? [] : message.parts);
        kept.clear(); omitted = [...candidates]; index = null;
        projected = projected.map((message, position) => isCurrent(message, position) ? message : { ...message, parts: [] });
      }
      const selected = candidates.filter(item => kept.has(item)), toolsKept = selected.filter(item => item.kind === "tool");
      const unfoldable = projected.flatMap((message, position) => isCurrent(message, position) ? [] :
        message.parts.filter(part => part.type === "tool" && !toolsKept.some(item => item.projected === part)));
      const currentTools = messages.flatMap((message, position) => isCurrent(message, position) ? message.parts.filter(part => part.type === "tool") : []);
      const ownedTools = messages.flatMap(message => message.info.role === "assistant" && message.info.parentID === current.info.id
        ? message.parts.filter(part => part.type === "tool") : []);
      const terminalTools = ownedTools.filter(part => ["completed", "error"].includes(part.state?.status));
      const total = (items, field) => items.reduce((sum, item) => sum + item[field], 0), idx = omissionTotals(omitted, existing?.index);
      const budgetOmittedTools = budgetOmitted.filter(part => part.type === "tool");
      const recoveryUnavailable = !historyAvailable && (existing || historicalBytes(projected) > historyAllowance);
      projection = { session_id: sessionID, agent: current.info.agent, current_request_message_id: current.info.id, observed_at_ms: Date.now(),
        request_purpose: contract?.purpose ?? null, request_id: contract?.request_id ?? null,
        execution_task_id: contract?.task_id ?? null, execution_segment_id: contract?.segment_id ?? null,
        world_write_authorized: contract?.world_write_authorized === true,
        request_projection_target_bytes: projectionTarget, sdk_fixed_request_bytes: sdkFixedBytes,
        sdk_message_expansion_bytes: sdkExpansionBytes,
        sdk_envelope_source: envelope ? "previous_matching_sdk_body" : "initial_fixed_reserve",
        current_terminal_tool_parts: terminalTools.length, current_unfinished_tool_parts: ownedTools.length - terminalTools.length,
        omitted_assistant_parts: proseParts, omitted_assistant_characters: proseCharacters,
        omitted_machine_snapshot_parts: snapshotParts, omitted_machine_snapshot_characters: snapshotCharacters,
        reduced_tool_parts: idx?.omitted_tool_parts ?? 0, historically_marked_tool_parts: toolsKept.filter(item => item.projected !== item.part).length,
        marked_native_summary_parts: selected.filter(item => item.kind === "summary").length,
        historical_tool_characters_before: total(candidates, "outputCharacters") + (existing?.index.omitted_output_characters ?? 0),
        historical_tool_characters_after: toolsKept.reduce((sum, item) => sum + outputText(item.projected).length, 0),
        historical_tool_history_budget_bytes: HISTORY_BUDGET, retained_historical_tool_parts: toolsKept.length,
        omitted_historical_tool_parts: idx?.omitted_tool_parts ?? 0, historical_tool_input_bytes_before: total(candidates, "inputBytes") + (existing?.index.omitted_input_bytes ?? 0),
        historical_tool_input_bytes_after: total(toolsKept, "inputBytes"), historical_tool_output_bytes_before: total(candidates, "outputBytes") + (existing?.index.omitted_output_bytes ?? 0),
        historical_tool_output_bytes_after: toolsKept.reduce((sum, item) => sum + bytes(outputText(item.projected)), 0),
        omitted_historical_tool_input_bytes: idx?.omitted_input_bytes ?? 0, omitted_historical_tool_output_bytes: idx?.omitted_output_bytes ?? 0,
        historical_tool_index_bytes: index && indexOwner ? bytes(index.text) : 0, retained_historical_tool_pair_bytes: toolsKept.reduce((sum, item) =>
          sum + projectionBytes([{ info: item.message.info, parts: [item.projected] }]), 0),
        historical_tool_history_bytes_after: historicalBytes(projected),
        history_source_index_sent: Boolean(index && indexOwner), history_all_old_parts_omitted: budgetOmitted.length > 0,
        history_recovery_tool_available: historyAvailable,
        budget_omitted_historical_parts: budgetOmitted.length, budget_omitted_historical_tool_parts: budgetOmittedTools.length,
        budget_omitted_unsupported_historical_tool_parts: budgetOmittedTools.filter(part => !tools.has(part.tool) ||
          part.tool === "invalid" || !["completed", "error"].includes(part.state?.status) || !inputObject(part)).length,
        history_recovery_unavailable_parts: recoveryUnavailable ? Math.max(1, projected.reduce((sum, message, position) =>
          sum + (isCurrent(message, position) ? 0 : message.parts.length), 0)) : 0,
        unfoldable_historical_tool_parts: unfoldable.length, unfoldable_historical_tool_input_bytes: unfoldable.reduce((sum, part) => sum + bytes(inputText(part)), 0),
        unfoldable_historical_tool_output_bytes: unfoldable.reduce((sum, part) => sum + bytes(outputText(part)), 0),
        current_tool_parts: currentTools.length, current_tool_input_bytes: currentTools.reduce((sum, part) => sum + bytes(inputText(part)), 0),
        current_tool_output_bytes: currentTools.reduce((sum, part) => sum + bytes(outputText(part)), 0),
        historical_message_budget_bytes: HISTORY_BUDGET, historical_message_allowance_bytes: historyAllowance,
        fixed_request_reserve_bytes: FIXED_REQUEST_RESERVE, managed_message_bytes_after: projectionBytes(projected),
        protected_message_bytes: protectedBytes,
        retained_historical_user_parts: selected.filter(item => item.kind === "user").length, retained_historical_summary_parts: selected.filter(item => item.kind === "summary").length,
        omitted_historical_user_parts: idx?.omitted_user_parts ?? 0, omitted_historical_summary_parts: idx?.omitted_summary_parts ?? 0,
        omitted_historical_text_bytes: idx?.omitted_text_bytes ?? 0,
        history_index_unavailable_parts: !indexOwner && budgetOmitted.length === 0 ? historicalBytes(projected) > HISTORY_BUDGET ? candidates.length :
          candidates.filter(item => item.part.tool === "invalid").length : 0,
        budget_scope: "historical_native_inference_projection", byte_unit: "utf8", character_unit: "utf16_code_units",
        output_byte_meaning: "Compacted outputs count their native placeholder. With a declared history entrance, the old pool including an optional source index fits the adaptive allowance up to 64KiB; complete current messages are protected. Owned execution budgets old sources below the 96KiB soft boundary after the complete current round and measured same-session/agent SDK envelope; initial SDK reserve is 32KiB. Ordinary requests budget below the 128KiB hard boundary. If no old parts fit, native_tool_history mode=list remains the on-demand entrance. A missing entrance keeps the sources and refuses an unmet allowance. Unsupported old tools remain only in original storage; their bodies are not promised readable through that API. Managed message bytes are an observed native projection, not a total hard limit or provider token count. SDK guard alone measures and caps the actual complete provider JSON at 128KiB.",
        stored_originals_changed: false };
      // The native loop uses this same array reference after the hook returns.
      messages.splice(0, messages.length, ...projected); await diagnostic();
    },
  };
}
