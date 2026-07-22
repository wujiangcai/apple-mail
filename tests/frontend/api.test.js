import { beforeEach, expect, test, vi } from "vitest";

import { ApiClient, ApiError } from "../../mail_manager/static/js/api.js";


beforeEach(() => {
  global.fetch = vi.fn();
});


test("state-changing requests send the in-memory CSRF token", async () => {
  fetch.mockResolvedValue(Response.json({ imported: 0 }));
  const api = new ApiClient({ csrfToken: "csrf-value", timeoutMs: 1000 });

  await api.post("/api/accounts/import", { accounts: [] });

  const [url, options] = fetch.mock.calls[0];
  expect(url).toBe("/api/accounts/import");
  expect(options.headers["X-CSRF-Token"]).toBe("csrf-value");
  expect(options.headers.Origin).toBeUndefined();
  expect(options.credentials).toBe("same-origin");
});


test("mail requests contain an account id but no credential query", async () => {
  fetch.mockResolvedValue(Response.json({ messages: [] }));
  const api = new ApiClient({ csrfToken: "csrf-value" });

  await api.post("/api/accounts/7/mail", { folder: "inbox" });

  expect(fetch.mock.calls[0][0]).toBe("/api/accounts/7/mail");
  expect(fetch.mock.calls[0][0]).not.toMatch(/refresh|token|password/i);
});


test("request timeout aborts fetch and raises a typed error", async () => {
  vi.useFakeTimers();
  fetch.mockImplementation((_url, { signal }) => new Promise((_resolve, reject) => {
    signal.addEventListener("abort", () => reject(signal.reason));
  }));
  const api = new ApiClient({ timeoutMs: 20 });

  const pending = api.get("/api/accounts");
  const rejection = expect(pending).rejects.toMatchObject({
    name: "ApiError",
    code: "TIMEOUT",
    status: 0,
  });
  await vi.advanceTimersByTimeAsync(20);

  await rejection;
  vi.useRealTimers();
});


test("401 responses emit an authentication event", async () => {
  fetch.mockResolvedValue(Response.json({ error: "authentication required" }, { status: 401 }));
  const api = new ApiClient();
  const listener = vi.fn();
  api.addEventListener("unauthorized", listener);

  await expect(api.get("/api/accounts")).rejects.toBeInstanceOf(ApiError);

  expect(listener).toHaveBeenCalledOnce();
});


test("non-success JSON responses become ApiError values", async () => {
  fetch.mockResolvedValue(Response.json({ error: "invalid request" }, { status: 400 }));
  const api = new ApiClient();

  await expect(api.post("/api/accounts/import", {})).rejects.toMatchObject({
    message: "invalid request",
    status: 400,
    payload: { error: "invalid request" },
  });
});


test("download requests still decode JSON error responses", async () => {
  fetch.mockResolvedValue(Response.json({ error: "request too large" }, { status: 413 }));
  const api = new ApiClient();

  await expect(api.post("/api/accounts/export", undefined, { responseType: "blob" }))
    .rejects.toMatchObject({
      message: "request too large",
      status: 413,
      payload: { error: "request too large" },
    });
});


test("abortAll cancels every active request", async () => {
  fetch.mockImplementation((_url, { signal }) => new Promise((_resolve, reject) => {
    signal.addEventListener("abort", () => reject(signal.reason));
  }));
  const api = new ApiClient();
  const first = expect(api.get("/api/accounts")).rejects.toMatchObject({ code: "ABORTED" });
  const second = expect(api.get("/api/check-auth")).rejects.toMatchObject({ code: "ABORTED" });

  api.abortAll();

  await Promise.all([first, second]);
});


test("a 401 aborts sibling requests before emitting unauthorized", async () => {
  fetch.mockImplementation((url, { signal }) => {
    if (url === "/api/check-auth") {
      return Promise.resolve(Response.json({ error: "authentication required" }, { status: 401 }));
    }
    return new Promise((_resolve, reject) => {
      signal.addEventListener("abort", () => reject(signal.reason));
    });
  });
  const api = new ApiClient();
  const sibling = expect(api.get("/api/accounts")).rejects.toMatchObject({ code: "ABORTED" });
  const listener = vi.fn(() => {
    expect(api.activeRequestCount).toBe(0);
  });
  api.addEventListener("unauthorized", listener);

  await expect(api.get("/api/check-auth")).rejects.toMatchObject({ status: 401 });
  await sibling;

  expect(listener).toHaveBeenCalledOnce();
});


test("callers can keep an expected 401 local to one request", async () => {
  fetch.mockImplementation((url, { signal }) => {
    if (url === "/api/login") {
      return Promise.resolve(Response.json({ error: "invalid credentials" }, { status: 401 }));
    }
    return new Promise((_resolve, reject) => {
      signal.addEventListener("abort", () => reject(signal.reason));
    });
  });
  const api = new ApiClient();
  const listener = vi.fn();
  api.addEventListener("unauthorized", listener);
  const sibling = expect(api.get("/api/accounts")).rejects.toMatchObject({ code: "ABORTED" });

  await expect(api.post("/api/login", { password: "wrong" }, {
    notifyUnauthorized: false,
  })).rejects.toMatchObject({
    message: "invalid credentials",
    status: 401,
  });

  expect(listener).not.toHaveBeenCalled();
  expect(api.activeRequestCount).toBe(1);
  api.abortAll();
  await sibling;
});
