// OpenCode's native inference hook. Stored messages are never edited here.
// No model, network, world operation, or third-party dependency is used.
import { createHash, randomBytes } from "node:crypto";
import { readFile, writeFile, rename, lstat } from "node:fs/promises";
import { fileURLToPath } from "node:url";

const HOOK = "experimental.chat.messages.transform";
const VERSION = 1;
const AGENTS = new Set(["shared-grove", "shared-grove-executor"]);
const HISTORICAL_SUMMARY_PREFIX = "[Grove历史原生摘要：仅作背景，不证明本轮行动或当前世界状态。"
  + "本轮确认动作只按当前task_action_summary的task_id与coverage判断。原偏好、引用和原文保留如下。]\n";
const HISTORICAL_TOOL_PREFIX = "[Grove historical completed tool result: ";
const HISTORICAL_INDEX_PREFIX = "[Grove native historical tool location index]\n";
const HISTORY_BUDGET_BYTES = 32 * 1024;
const HISTORY_INDEX_MAX_BYTES = 4096;
const NATIVE_MESSAGE_ID = /^msg_[A-Za-z0-9]{1,100}$/;
const NATIVE_PART_ID = /^prt_[A-Za-z0-9]{1,100}$/;
const COMPACTED_OUTPUT = "[Old tool result content cleared]";

const bytes = value => Buffer.byteLength(value, "utf8");
const inputText = part => JSON.stringify(part.state?.input) ?? "";
const modelOutput = part => part.state?.status === "completed"
  ? part.state.time?.compacted ? COMPACTED_OUTPUT : part.state.output ?? ""
  : part.state?.status === "error" ? part.state.error ?? "" : "[Tool execution was interrupted]";
// Count both serialized message envelopes, with input string escaping as used
// by the active compatible provider, rather than counting the raw bodies alone.
const pairBytes = part => bytes(JSON.stringify([
  { role: "assistant", tool_calls: [{ id: part.callID, type: "function",
    function: { name: part.tool, arguments: inputText(part) } }], metadata: part.metadata },
  { role: "tool", tool_call_id: part.callID, name: part.tool, content: modelOutput(part),
    attachments: part.state.time?.compacted ? undefined : part.state.attachments },
]));

function objectOutput(part) {
  if (typeof part.state?.output !== "string") return null;
  try {
    const value = JSON.parse(part.state.output);
    return value && typeof value === "object" && !Array.isArray(value) ? value : null;
  } catch { return null; }
}

function readableHistoricalPart(part, message, formalTools) {
  return part.type === "tool" && formalTools.has(part.tool) && part.state?.status === "completed" &&
    !message.info.error &&
    NATIVE_MESSAGE_ID.test(message.info.id) && NATIVE_PART_ID.test(part.id) &&
    part.state.input && typeof part.state.input === "object" && !Array.isArray(part.state.input) &&
    objectOutput(part) !== null;
}

function historicalIndex(part, sessionID) {
  if (part.type !== "text" || part.synthetic !== true || typeof part.text !== "string" ||
      !part.text.startsWith(HISTORICAL_INDEX_PREFIX)) return null;
  try {
    const index = JSON.parse(part.text.slice(HISTORICAL_INDEX_PREFIX.length));
    const counters = ["omitted_tool_parts", "omitted_input_bytes", "omitted_output_bytes", "omitted_output_characters"];
    if (index.scope !== "historical_tool_location_index" || index.historical_only !== true ||
        index.session_id !== sessionID || counters.some(key => !Number.isSafeInteger(index[key]) || index[key] < 0) ||
        bytes(part.text) > HISTORY_INDEX_MAX_BYTES ||
        index.list?.tool !== "native_tool_history" || index.list?.arguments?.mode !== "list") return null;
    return index;
  } catch { return null; }
}

function sourceAnchor(candidate) {
  const { message, part } = candidate;
  const source = { message_id: message.info.id, part_id: part.id };
  // Optional source fields are never shortened or invented. The exact IDs and
  // native list/read remain sufficient when an unusual field is too large.
  for (const [key, value] of Object.entries({ tool: part.tool, call_id: part.callID,
    operation_id: objectOutput(part)?.operation_id })) {
    if (typeof value === "string" && bytes(value) <= 256) source[key] = value;
  }
  return source;
}

function locationIndex(sessionID, omitted, existing) {
  const first = existing?.source_boundary?.first ?? sourceAnchor(omitted[0]);
  const last = omitted.length ? sourceAnchor(omitted.at(-1)) : existing.source_boundary.last;
  const index = { scope: "historical_tool_location_index", historical_only: true, session_id: sessionID,
    meaning: "Source locations only, not original result bodies, current state or evidence of this task's execution. Recover the selected recorded input/output through native_tool_history; use current world tools before acting.",
    omitted_tool_parts: (existing?.omitted_tool_parts ?? 0) + omitted.length,
    omitted_input_bytes: (existing?.omitted_input_bytes ?? 0) + omitted.reduce((sum, item) => sum + item.inputBytes, 0),
    omitted_output_bytes: (existing?.omitted_output_bytes ?? 0) + omitted.reduce((sum, item) => sum + item.outputBytes, 0),
    omitted_output_characters: (existing?.omitted_output_characters ?? 0) + omitted.reduce((sum, item) => sum + item.outputCharacters, 0),
    source_boundary: { first, last },
    list: { tool: "native_tool_history", arguments: { mode: "list" } },
    read: { tool: "native_tool_history", arguments: { mode: "read", message_id: last.message_id, part_id: last.part_id } },
    input_read: { tool: "native_tool_history", arguments: { mode: "read", message_id: last.message_id,
      part_id: last.part_id, section: "input" } } };
  const text = HISTORICAL_INDEX_PREFIX + JSON.stringify(index);
  if (bytes(text) > HISTORY_INDEX_MAX_BYTES) throw new Error("Native historical location index exceeds budget");
  return { index, text };
}

async function writeDiagnostic(path, value) {
  try {
    if ((await lstat(path)).isSymbolicLink()) throw new Error("Linked context diagnostic");
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
  }
  const temporary = `${path}.${randomBytes(8).toString("hex")}.tmp`;
  await writeFile(temporary, JSON.stringify(value), { mode: 0o600, flag: "wx" });
  await rename(temporary, path);
}

// Retained history keeps the complete input/result and all authored references.
// The marker changes only the model copy; it does not confer observation authority.
function historicalOutput(part, messageID, sessionID, formalTools) {
  if (!formalTools.has(part.tool) || part.state?.status !== "completed" ||
      part.state.time?.compacted || typeof part.state.output !== "string") return null;
  const original = { session_id: sessionID, message_id: messageID, part_id: part.id };
  const historical = { historical_only: true,
    meaning: "Historical result, not current state. Only current task_action_summary with matching task_id and coverage establishes this task's actions.",
    original,
    read: { tool: "native_tool_history", arguments: {
      mode: "read", message_id: messageID, part_id: part.id } } };
  let output;
  try { output = JSON.parse(part.state.output); } catch { /* Preserve a non-JSON body verbatim below its marker. */ }
  if (!output || typeof output !== "object" || Array.isArray(output)) {
    if (part.state.output.startsWith(HISTORICAL_TOOL_PREFIX)) return { output: part.state.output, reduced: false };
    return { output: `${HISTORICAL_TOOL_PREFIX}${JSON.stringify({ ...historical,
      scope: "historical_unstructured_tool_result", original_body_follows_unchanged: true })}]\n${part.state.output}`,
      reduced: false };
  }
  const previous = output.native_context_projection;
  if (previous?.historical_only === true && previous.original?.session_id === sessionID &&
      previous.original?.message_id === messageID && previous.original?.part_id === part.id &&
      ["historical_tool_result", "historical_tool_result_without_later_scene"].includes(previous.scope)) {
    return { output: part.state.output };
  }
  output.native_context_projection = {
    ...historical,
    scope: "historical_tool_result", omitted_sections: [],
  };
  return { output: JSON.stringify(output) };
}

export default async function GroveNativeContext(_input, options = {}) {
  const source = await readFile(fileURLToPath(import.meta.url));
  const sourceSha = createHash("sha256").update(source).digest("hex");
  if (options.version !== VERSION || sourceSha !== options.source_sha256 ||
      typeof options.nonce !== "string" || !/^[a-f0-9]{32}$/.test(options.nonce) ||
      typeof options.role_id !== "string" || !options.role_id ||
      typeof options.session_id !== "string" || !Array.isArray(options.machine_context_prefixes) ||
      options.machine_context_prefixes.some(prefix => typeof prefix !== "string" || !prefix) ||
      typeof options.diagnostic_path !== "string" || !options.diagnostic_path.startsWith("/") ||
      !Array.isArray(options.formal_tools) || options.formal_tools.some(name => typeof name !== "string")) {
    throw new Error("Invalid managed Grove context hook");
  }
  const formalTools = new Set(options.formal_tools);
  const proof = { version: VERSION, status: "loaded", hook: HOOK,
    source_sha256: sourceSha, nonce: options.nonce, role_id: options.role_id,
    process_id: process.pid, loaded_at_ms: Date.now() };
  await writeDiagnostic(options.diagnostic_path, proof);
  return {
    [HOOK]: async (_hookInput, output) => {
      const messages = output.messages;
      if (!Array.isArray(messages)) throw new Error("Invalid native messages");
      const current = messages.findLast(message => message?.info?.role === "user");
      if (!current || !AGENTS.has(current.info.agent) ||
          current.parts.some(part => part.type === "compaction")) return;
      const sessionID = current.info.sessionID;
      if (typeof sessionID !== "string" || typeof current.info.id !== "string" ||
          (options.session_id && sessionID !== options.session_id) ||
          messages.some(message => message?.info?.sessionID !== sessionID ||
            !Array.isArray(message.parts) || message.parts.some(part =>
              part.sessionID !== sessionID || part.messageID !== message.info.id))) {
        throw new Error("Mixed native session in Grove context hook");
      }
      const currentPosition = messages.indexOf(current);
      const isCurrent = (message, position) => position >= currentPosition || message.info.parentID === current.info.id;
      const isSummary = message => message.info.summary === true &&
        (message.info.agent === "compaction" || message.info.mode === "compaction");
      const candidates = [], candidateParts = new Map();
      let existingIndex = null;
      for (const [position, message] of messages.entries()) {
        if (message.info.role !== "assistant" || isCurrent(message, position) || isSummary(message)) continue;
        for (const part of message.parts) {
          const index = historicalIndex(part, sessionID);
          if (index && !message.info.error) {
            if (existingIndex) throw new Error("Multiple native historical location indexes");
            existingIndex = { message, part, index };
          } else if (readableHistoricalPart(part, message, formalTools)) {
            const historical = historicalOutput(part, message.info.id, sessionID, formalTools);
            const projected = historical ? { ...part, state: { ...part.state, output: historical.output } } : part;
            const candidate = { message, part, projected, marked: historical !== null,
              inputBytes: bytes(inputText(part)), outputBytes: bytes(modelOutput(part)),
              outputCharacters: modelOutput(part).length, pairBytes: pairBytes(projected) };
            candidates.push(candidate);
            candidateParts.set(part, candidate);
          }
        }
      }
      const retainedCandidates = new Set();
      let retainedPairBytes = 0;
      const existingIndexBytes = existingIndex ? bytes(existingIndex.part.text) : 0;
      const allPairBytes = candidates.reduce((sum, item) => sum + item.pairBytes, 0);
      const overflow = allPairBytes + existingIndexBytes > HISTORY_BUDGET_BYTES;
      const allowance = HISTORY_BUDGET_BYTES - (overflow ? HISTORY_INDEX_MAX_BYTES : existingIndexBytes);
      // Keep a contiguous recent suffix of complete native parts. One native
      // tool part becomes BOTH the SDK call and result; never shorten its input
      // or remove just one side of that pair. A single oversized pair is indexed.
      for (let position = candidates.length - 1; position >= 0; position -= 1) {
        const candidate = candidates[position];
        if (retainedPairBytes + candidate.pairBytes > allowance) break;
        retainedCandidates.add(candidate);
        retainedPairBytes += candidate.pairBytes;
      }
      const omitted = candidates.filter(candidate => !retainedCandidates.has(candidate));
      const indexProjection = omitted.length
        ? locationIndex(sessionID, omitted, existingIndex?.index)
        : existingIndex ? { index: existingIndex.index, text: existingIndex.part.text } : null;
      const indexPart = indexProjection ? { id: existingIndex?.part.id ?? omitted[0].part.id,
        sessionID, messageID: existingIndex?.message.info.id ?? omitted[0].message.info.id,
        type: "text", synthetic: true, text: indexProjection.text } : null;
      let proseParts = 0, proseCharacters = 0;
      let historicalTools = 0, historicalSummaries = 0;
      let snapshotParts = 0, snapshotCharacters = 0;
      let unfoldableParts = 0, unfoldableInputBytes = 0, unfoldableOutputBytes = 0;
      let currentToolParts = 0, currentInputBytes = 0, currentOutputBytes = 0;
      for (const [position, message] of messages.entries()) {
        if (isCurrent(message, position)) {
          for (const part of message.parts) {
            if (part.type !== "tool") continue;
            currentToolParts += 1;
            currentInputBytes += bytes(inputText(part));
            currentOutputBytes += bytes(modelOutput(part));
          }
          continue;
        }
        if (message.info.role === "user" && message.info.id !== current.info.id &&
            AGENTS.has(message.info.agent)) {
          message.parts = message.parts.filter(part => {
            const machine = part.type === "text" && typeof part.text === "string" &&
              options.machine_context_prefixes.some(prefix => part.text.startsWith(`${prefix}\n`));
            if (machine && !part.ignored) {
              snapshotParts += 1;
              snapshotCharacters += part.text.length;
            }
            return !machine;
          });
        }
        if (message.info.role !== "assistant") continue;
        // Native compaction is identified by native metadata, not by guessing
        // whether its prose is a summary. Keep its mixed memory in full.
        const nativeSummary = isSummary(message);
        const retained = [];
        for (const part of message.parts) {
          if (part === existingIndex?.part) {
            retained.push(omitted.length ? indexPart : part);
            continue;
          }
          if (nativeSummary && part.type === "text" && typeof part.text === "string") {
            historicalSummaries += 1;
            retained.push(part.text.startsWith(HISTORICAL_SUMMARY_PREFIX) ? part :
              { ...part, text: HISTORICAL_SUMMARY_PREFIX + part.text });
            continue;
          }
          if (AGENTS.has(message.info.agent) && (part.type === "text" || part.type === "reasoning")) {
            proseParts += 1;
            proseCharacters += typeof part.text === "string" ? part.text.length : 0;
            continue;
          }
          const candidate = candidateParts.get(part);
          if (candidate) {
            if (retainedCandidates.has(candidate)) {
              retained.push(candidate.projected);
              if (candidate.marked) historicalTools += 1;
            } else if (!existingIndex && candidate === omitted[0]) retained.push(indexPart);
            continue;
          }
          const historical = part.type === "tool"
            ? historicalOutput(part, message.info.id, sessionID, formalTools) : null;
          if (part.type === "tool" && formalTools.has(part.tool)) {
            unfoldableParts += 1;
            unfoldableInputBytes += bytes(inputText(part));
            unfoldableOutputBytes += bytes(modelOutput(historical
              ? { ...part, state: { ...part.state, output: historical.output } } : part));
          }
          if (historical !== null) {
            historicalTools += 1;
            // The host loaded these messages from storage into this inference.
            // Replace only the model copy. The persisted state/input is intact.
            retained.push({ ...part, state: { ...part.state, output: historical.output } });
          } else retained.push(part);
        }
        message.parts = retained;
      }
      const kept = [...retainedCandidates];
      const indexBytes = indexProjection ? bytes(indexProjection.text) : 0;
      const omittedStats = indexProjection?.index;
      const retainedInputBytes = kept.reduce((sum, item) => sum + item.inputBytes, 0);
      const retainedOutputBytes = kept.reduce((sum, item) => sum + bytes(modelOutput(item.projected)), 0);
      await writeDiagnostic(options.diagnostic_path, { ...proof,
        projection: { session_id: sessionID, current_request_message_id: current.info.id,
          observed_at_ms: Date.now(), omitted_assistant_parts: proseParts,
          omitted_assistant_characters: proseCharacters, reduced_tool_parts: omittedStats?.omitted_tool_parts ?? 0,
          historically_marked_tool_parts: historicalTools,
          marked_native_summary_parts: historicalSummaries,
          omitted_machine_snapshot_parts: snapshotParts,
          omitted_machine_snapshot_characters: snapshotCharacters,
          historical_tool_characters_before: candidates.reduce((sum, item) => sum + item.outputCharacters, 0) +
            (existingIndex?.index.omitted_output_characters ?? 0),
          historical_tool_characters_after: kept.reduce((sum, item) => sum + modelOutput(item.projected).length, 0),
          historical_tool_history_budget_bytes: HISTORY_BUDGET_BYTES,
          retained_historical_tool_parts: kept.length,
          omitted_historical_tool_parts: omittedStats?.omitted_tool_parts ?? 0,
          historical_tool_input_bytes_before: candidates.reduce((sum, item) => sum + item.inputBytes, 0) +
            (existingIndex?.index.omitted_input_bytes ?? 0),
          historical_tool_input_bytes_after: retainedInputBytes,
          historical_tool_output_bytes_before: candidates.reduce((sum, item) => sum + item.outputBytes, 0) +
            (existingIndex?.index.omitted_output_bytes ?? 0),
          historical_tool_output_bytes_after: retainedOutputBytes,
          omitted_historical_tool_input_bytes: omittedStats?.omitted_input_bytes ?? 0,
          omitted_historical_tool_output_bytes: omittedStats?.omitted_output_bytes ?? 0,
          historical_tool_index_bytes: indexBytes,
          retained_historical_tool_pair_bytes: retainedPairBytes,
          historical_tool_history_bytes_after: retainedPairBytes + indexBytes,
          unfoldable_historical_tool_parts: unfoldableParts,
          unfoldable_historical_tool_input_bytes: unfoldableInputBytes,
          unfoldable_historical_tool_output_bytes: unfoldableOutputBytes,
          current_tool_parts: currentToolParts,
          current_tool_input_bytes: currentInputBytes,
          current_tool_output_bytes: currentOutputBytes,
          budget_scope: "readable_completed_formal_tool_pairs_only",
          byte_unit: "utf8",
          output_byte_meaning: "Native model-copy output strings; native-compacted outputs count their host placeholder, not the stored original body. Pair budget also counts serialized tool/call/input/output overhead. This is not provider token usage or a total context limit.",
          character_unit: "utf16_code_units",
          stored_originals_changed: false } });
    },
  };
}
