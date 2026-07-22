import { expect, test, vi } from "vitest";

import {
  checkBound,
  deleteAccount,
  deleteAccounts,
  importAccounts,
  loadAccounts,
  parseImportText,
  updateBound,
} from "../../mail_manager/static/js/app.js";
import { createState } from "../../mail_manager/static/js/state.js";


test("legacy four-field lines are parsed entirely in memory", () => {
  const parsed = parseImportText([
    "a@example.com----mail-a----client-a----refresh-a",
    "b@example.com----mail-b----client-b----refresh-b",
  ].join("\n"));

  expect(parsed).toEqual([
    {
      email: "a@example.com",
      password: "mail-a",
      clientId: "client-a",
      refreshToken: "refresh-a",
    },
    {
      email: "b@example.com",
      password: "mail-b",
      clientId: "client-b",
      refreshToken: "refresh-b",
    },
  ]);
});


test("JSONL imports preserve the server credential contract", () => {
  const parsed = parseImportText(JSON.stringify({
    email: "a@example.com",
    password: "mail-a",
    clientId: "client-a",
    refreshToken: "refresh-a",
  }));

  expect(parsed[0].refreshToken).toBe("refresh-a");
});


test("malformed import lines fail before an API request", () => {
  expect(() => parseImportText("only----three----fields")).toThrow(/第 1 行/);
  expect(() => parseImportText(" ")).toThrow(/没有可导入/);
});


test("failed delete leaves the account state unchanged", async () => {
  const state = createState([{ id: 7, email: "u@example.com" }]);
  const api = { delete: vi.fn().mockRejectedValue(new Error("failed")) };

  await expect(deleteAccount({ api, state, id: 7 })).rejects.toThrow("failed");

  expect(state.accounts.map((item) => item.id)).toEqual([7]);
});


test("successful delete reloads authoritative metadata", async () => {
  const state = createState([{ id: 7, email: "u@example.com" }]);
  const api = {
    delete: vi.fn().mockResolvedValue({ deleted: true }),
    get: vi.fn().mockResolvedValue([{ id: 8, email: "next@example.com" }]),
  };

  await deleteAccount({ api, state, id: 7 });

  expect(api.get).toHaveBeenCalledWith("/api/accounts", {
    signal: expect.any(AbortSignal),
  });
  expect(state.accounts.map((item) => item.id)).toEqual([8]);
});


test("failed bound update does not optimistically mutate state", async () => {
  const state = createState([{ id: 7, email: "u@example.com", bound: false }]);
  const api = { patch: vi.fn().mockRejectedValue(new Error("failed")) };

  await expect(updateBound({ api, state, id: 7, bound: true })).rejects.toThrow("failed");

  expect(state.accounts[0].bound).toBe(false);
});


test("failed import keeps the previous account metadata", async () => {
  const state = createState([{ id: 7, email: "u@example.com" }]);
  const api = { post: vi.fn().mockRejectedValue(new Error("failed")) };

  await expect(importAccounts({
    api,
    state,
    accounts: [{
      email: "next@example.com",
      password: "secret",
      clientId: "client",
      refreshToken: "refresh",
    }],
  })).rejects.toThrow("failed");

  expect(state.accounts.map((item) => item.id)).toEqual([7]);
});


test("batch limits reject work before an API request", async () => {
  const state = createState();
  const api = { post: vi.fn() };

  await expect(deleteAccounts({
    api,
    state,
    ids: Array.from({ length: 1001 }, (_, index) => index + 1),
  })).rejects.toThrow(/1–1000/);
  await expect(checkBound({
    api,
    state,
    ids: Array.from({ length: 21 }, (_, index) => index + 1),
  })).rejects.toThrow(/1–20/);

  expect(api.post).not.toHaveBeenCalled();
});


test("paste imports reject more than two MiB by encoded byte length", () => {
  expect(() => parseImportText("a".repeat((2 * 1024 * 1024) + 1))).toThrow(/2 MiB/);
});


test("only the newest account load may replace metadata", async () => {
  const state = createState([{ id: 1, email: "initial@example.com" }]);
  const pending = [];
  const api = {
    get: vi.fn((_path, { signal }) => new Promise((resolve) => {
      pending.push({ resolve, signal });
    })),
  };

  const first = loadAccounts({ api, state });
  const second = loadAccounts({ api, state });
  expect(pending[0].signal.aborted).toBe(true);

  pending[1].resolve([{ id: 2, email: "newest@example.com" }]);
  await second;
  pending[0].resolve([{ id: 3, email: "stale@example.com" }]);
  await first;

  expect(state.accounts.map((account) => account.email)).toEqual(["newest@example.com"]);
});
