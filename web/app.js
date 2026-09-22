const createCard = document.querySelector("#create-card");
const roleCard = document.querySelector("#role-card");
const createForm = document.querySelector("#create-form");
const createError = document.querySelector("#create-error");
const roleError = document.querySelector("#role-error");
const roleSummary = document.querySelector("#role-summary");
const roleIdLabel = document.querySelector("#role-id");
const generateButton = document.querySelector("#generate-package");
const newRoleButton = document.querySelector("#new-role");
const packagePanel = document.querySelector("#package-panel");
const instructions = document.querySelector("#agent-instructions");
const copyButton = document.querySelector("#copy-instructions");
const expiryLabel = document.querySelector("#ticket-expiry");
const claimedStatus = document.querySelector("#status-claimed");
const enteredStatus = document.querySelector("#status-entered");
const activityStatus = document.querySelector("#status-activity");
const universeLabel = document.querySelector("#universe-label");

let roleId = sessionStorage.getItem("agentWorldRoleId");
let pollTimer = null;

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const message = body.detail || body.message || body.error || ("HTTP " + response.status);
    throw new Error(message);
  }
  return body;
}

function showError(node, error) {
  node.textContent = (error && error.message) || String(error);
  node.hidden = false;
}

function clearError(node) {
  node.hidden = true;
  node.textContent = "";
}
function formatTime(epochSeconds) {
  if (!epochSeconds) return "";
  return new Date(epochSeconds * 1000).toLocaleString();
}

function setDone(node, done, detail) {
  node.classList.toggle("done", Boolean(done));
  if (detail) node.querySelector("small").textContent = detail;
}

function showRole(status) {
  const role = status.role_profile;
  roleId = role.role_id;
  sessionStorage.setItem("agentWorldRoleId", roleId);
  createCard.hidden = true;
  roleCard.hidden = false;
  roleSummary.textContent = role.display_name + " is a persistent role in " + status.universe + ".";
  roleIdLabel.textContent = role.role_id;
  updateStatus(status);
}

function updateStatus(status) {
  const ticket = status.join_ticket;
  setDone(
    claimedStatus,
    status.identity_claimed,
    status.identity_claimed
      ? "Identity claimed " + formatTime(ticket && ticket.used_at)
      : "Waiting for the agent to exchange the join ticket."
  );
  setDone(
    enteredStatus,
    status.entered_world,
    status.entered_world
      ? "Entered " + formatTime(status.presence && status.presence.first_bootstrap_at) + " · bootstrap count " + ((status.presence && status.presence.bootstrap_count) || 1)
      : "Waiting for the first authenticated bootstrap."
  );
  setDone(
    activityStatus,
    status.latest_recipient_event_seq > 0,
    status.latest_recipient_event_seq > 0
      ? "Durable world activity exists · latest event " + status.latest_recipient_event_seq
      : "Waiting for the role to take its first durable world action."
  );
}

async function refreshStatus() {
  if (!roleId) return;
  try {
    const status = await api("/api/roles/" + encodeURIComponent(roleId) + "/status");
    showRole(status);
  } catch (error) {
    clearInterval(pollTimer);
    pollTimer = null;
    showError(roleError, error);
  }
}

function startPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(refreshStatus, 2000);
}
createForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  clearError(createError);
  const button = createForm.querySelector("button[type=submit]");
  button.disabled = true;
  try {
    const role = await api("/api/roles", {
      method: "POST",
      body: JSON.stringify({
        display_name: document.querySelector("#display-name").value.trim(),
        avatar_ref: document.querySelector("#avatar-ref").value.trim() || null,
      }),
    });
    roleId = role.role_id;
    sessionStorage.setItem("agentWorldRoleId", roleId);
    const status = await api("/api/roles/" + encodeURIComponent(roleId) + "/status");
    showRole(status);
    startPolling();
  } catch (error) {
    showError(createError, error);
  } finally {
    button.disabled = false;
  }
});

generateButton.addEventListener("click", async () => {
  if (!roleId) return;
  clearError(roleError);
  generateButton.disabled = true;
  try {
    const pack = await api("/api/roles/" + encodeURIComponent(roleId) + "/join-package", {
      method: "POST",
      body: JSON.stringify({ ttl_seconds: 600 }),
    });
    instructions.value = pack.agent_instructions;
    expiryLabel.textContent = "Expires " + formatTime(pack.expires_at);
    packagePanel.hidden = false;
    packagePanel.scrollIntoView({ behavior: "smooth", block: "nearest" });
  } catch (error) {
    showError(roleError, error);
  } finally {
    generateButton.disabled = false;
  }
});
copyButton.addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(instructions.value);
    const original = copyButton.textContent;
    copyButton.textContent = "Copied";
    setTimeout(() => { copyButton.textContent = original; }, 1400);
  } catch {
    instructions.focus();
    instructions.select();
    document.execCommand("copy");
  }
});

newRoleButton.addEventListener("click", () => {
  sessionStorage.removeItem("agentWorldRoleId");
  roleId = null;
  packagePanel.hidden = true;
  instructions.value = "";
  roleCard.hidden = true;
  createCard.hidden = false;
  createForm.reset();
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = null;
});

async function boot() {
  try {
    const config = await api("/api/config");
    universeLabel.textContent = "Universe: " + config.universe;
    if (roleId) {
      await refreshStatus();
      startPolling();
    }
  } catch (error) {
    showError(createError, error);
  }
}

boot();
