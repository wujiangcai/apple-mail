const MUTATING_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);


export class ApiError extends Error {
  constructor(message, { status = 0, code = "HTTP_ERROR", payload = null } = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.payload = payload;
  }
}


function assertApiPath(path) {
  if (typeof path !== "string" || !path.startsWith("/api/") || path.startsWith("//")) {
    throw new TypeError("API path must be a same-origin /api/ path");
  }
}


async function readPayload(response, responseType) {
  if (responseType === "blob") {
    return response.blob();
  }
  if (responseType === "text") {
    return response.text();
  }
  if (response.status === 204) {
    return null;
  }
  const contentType = response.headers.get("Content-Type") || "";
  if (contentType.includes("application/json")) {
    return response.json();
  }
  const text = await response.text();
  return text || null;
}


export class ApiClient extends EventTarget {
  constructor({ csrfToken = "", timeoutMs = 10_000, fetchImpl = globalThis.fetch } = {}) {
    super();
    if (typeof fetchImpl !== "function") {
      throw new TypeError("fetch is required");
    }
    this.csrfToken = csrfToken;
    this.timeoutMs = timeoutMs;
    this.fetchImpl = fetchImpl;
    this.controllers = new Set();
  }

  get activeRequestCount() {
    return this.controllers.size;
  }

  setCsrfToken(token) {
    this.csrfToken = typeof token === "string" ? token : "";
  }

  clearCsrfToken() {
    this.csrfToken = "";
  }

  abortAll() {
    const active = [...this.controllers];
    this.controllers.clear();
    for (const controller of active) {
      controller.abort();
    }
  }

  get(path, options) {
    return this.request("GET", path, options);
  }

  post(path, body, options) {
    return this.request("POST", path, { ...options, body });
  }

  patch(path, body, options) {
    return this.request("PATCH", path, { ...options, body });
  }

  delete(path, body, options) {
    return this.request("DELETE", path, { ...options, body });
  }

  async request(
    method,
    path,
    { body, responseType = "json", signal, notifyUnauthorized = true, timeoutMs } = {},
  ) {
    assertApiPath(path);
    const requestTimeoutMs = timeoutMs ?? this.timeoutMs;
    if (!Number.isFinite(requestTimeoutMs) || requestTimeoutMs <= 0) {
      throw new TypeError("timeoutMs must be a positive number");
    }
    const normalizedMethod = method.toUpperCase();
    const controller = new AbortController();
    this.controllers.add(controller);
    const abortFromCaller = () => controller.abort();
    if (signal) {
      if (signal.aborted) {
        controller.abort();
      } else {
        signal.addEventListener("abort", abortFromCaller, { once: true });
      }
    }
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, requestTimeoutMs);
    const headers = { Accept: responseType === "json" ? "application/json" : "*/*" };
    if (body !== undefined) {
      headers["Content-Type"] = "application/json";
    }
    if (MUTATING_METHODS.has(normalizedMethod) && this.csrfToken) {
      headers["X-CSRF-Token"] = this.csrfToken;
    }

    try {
      const response = await this.fetchImpl(path, {
        method: normalizedMethod,
        credentials: "same-origin",
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: controller.signal,
      });
      const payload = await readPayload(
        response,
        response.ok ? responseType : "json",
      );
      if (!response.ok) {
        if (response.status === 401 && notifyUnauthorized) {
          this.abortAll();
          this.dispatchEvent(new Event("unauthorized"));
        }
        const message = payload && typeof payload === "object" && payload.error
          ? String(payload.error)
          : `request failed (${response.status})`;
        throw new ApiError(message, { status: response.status, payload });
      }
      return payload;
    } catch (error) {
      if (timedOut) {
        throw new ApiError("request timed out", { code: "TIMEOUT" });
      }
      if (error instanceof ApiError) {
        throw error;
      }
      if (controller.signal.aborted) {
        throw new ApiError("request aborted", { code: "ABORTED" });
      }
      throw new ApiError("network request failed", { code: "NETWORK_ERROR" });
    } finally {
      clearTimeout(timer);
      this.controllers.delete(controller);
      signal?.removeEventListener("abort", abortFromCaller);
    }
  }
}
