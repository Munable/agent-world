// Transport-neutral role client. Keep credentials in caller-managed memory, never in URLs.
export class WorldRequestError extends Error {
  constructor(status, body) {
    super(body.message || "World request failed");
    this.status = status;
    this.code = body.error;
    this.recovery = body.recovery;
  }
}

export class StaleViewDelta extends Error {}

function mapCopy(value) {
  const result = Object.create(null);
  for (const [id, item] of Object.entries(value || {})) result[id] = structuredClone(item);
  return result;
}

export function applyViewUpdate(current, update) {
  if (update.kind === "snapshot") {
    return {...update, snapshot: {entities: mapCopy(update.snapshot.entities),
      resources: mapCopy(update.snapshot.resources), meta: structuredClone(update.snapshot.meta)}};
  }
  if (!current || update.kind !== "delta" || current.cursor !== update.base_cursor ||
      current.view !== update.view || current.viewer_role_id !== update.viewer_role_id ||
      current.world_version !== update.world_version || current.view_version !== update.view_version) {
    throw new StaleViewDelta("Delta does not extend the current view; do not apply it");
  }
  const snapshot = {meta: structuredClone(update.delta.meta)};
  for (const section of ["entities", "resources"]) {
    snapshot[section] = mapCopy(current.snapshot[section]);
    for (const id of update.delta[section].remove) delete snapshot[section][id];
    for (const [id, item] of Object.entries(update.delta[section].upsert)) {
      snapshot[section][id] = structuredClone(item);
    }
  }
  const result = {...update, kind: "snapshot", snapshot};
  delete result.delta;
  delete result.base_cursor;
  return result;
}

export class WorldClient {
  constructor(baseUrl, tokenProvider, fetcher = globalThis.fetch) {
    this.baseUrl = baseUrl.replace(/\/$/, "");
    this.tokenProvider = tokenProvider;
    this.fetcher = fetcher;
    this.view = null;
    this.selector = null;
    this.viewGeneration = 0;
  }
  async request(path, body) {
    const response = await this.fetcher(this.baseUrl + path, {
      method: body === undefined ? "GET" : "POST",
      credentials: "omit",
      headers: {Authorization: "Bearer " + await this.tokenProvider(), "Content-Type": "application/json"},
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const result = await response.json().catch(() => ({error: "InvalidResponse", message: "World returned invalid JSON"}));
    if (!response.ok) {
      if ([401, 403].includes(response.status)) this.view = null;
      throw new WorldRequestError(response.status, result);
    }
    if (result.error === "InvalidResponse") throw new WorldRequestError(502, result);
    return result;
  }
  async loadView(name, argumentsObject = {}) {
    const generation = ++this.viewGeneration;
    this.selector = {name, argumentsObject: structuredClone(argumentsObject)};
    this.view = null;
    const update = await this.request("/v1/views/" + encodeURIComponent(name) + "/snapshot", {arguments: argumentsObject});
    if (generation === this.viewGeneration) this.view = applyViewUpdate(null, update);
    return this.view;
  }
  async syncView() {
    if (!this.view || !this.selector) throw new Error("Load a view first");
    const cursor = this.view.cursor;
    const generation = this.viewGeneration;
    try {
      const update = await this.request("/v1/views/sync", {cursor});
      // A concurrent newer reply may have already replaced our base.
      if (this.view && this.view.cursor === cursor) this.view = applyViewUpdate(this.view, update);
    } catch (error) {
      if (error.code === "ViewResetRequired") {
        if (generation !== this.viewGeneration || this.view?.cursor !== cursor) return this.view;
        return this.loadView(this.selector.name, this.selector.argumentsObject);
      }
      throw error;
    }
    return this.view;
  }
  query(name, argumentsObject = {}) {
    return this.request("/v1/functions/" + encodeURIComponent(name) + "/invoke", {arguments: argumentsObject});
  }
  act(name, argumentsObject, operationId, {expectedVersion, activityClaim} = {}) {
    if (typeof operationId !== "string" || !operationId.trim()) throw new Error("A stable operation ID is required");
    // Do not automatically retry uncertain writes or generate a replacement operation ID.
    return this.request("/v1/functions/" + encodeURIComponent(name) + "/invoke", {
      arguments: argumentsObject, operation_id: operationId,
      ...(expectedVersion === undefined ? {} : {expected_version: expectedVersion}),
      ...(activityClaim === undefined ? {} : {activity_claim: activityClaim}),
    });
  }
  receipt(operationId) {
    return this.request("/v1/receipts/" + encodeURIComponent(operationId));
  }
}
