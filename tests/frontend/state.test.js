// @vitest-environment jsdom

import { afterEach, expect, test, vi } from "vitest";

import { createState } from "../../mail_manager/static/js/state.js";


afterEach(() => {
  vi.useRealTimers();
});


test("account state retains metadata but discards credential fields", () => {
  const state = createState([{
    id: 7,
    email: "user@example.com",
    bound: false,
    tokenValid: true,
    password: "mail-secret",
    refreshToken: "refresh-secret",
  }]);

  expect(state.accounts).toEqual([{
    id: 7,
    email: "user@example.com",
    lastRefreshTime: null,
    bound: false,
    boundAt: null,
    tokenValid: true,
    rentable: true,
    rented: false,
    hasRentalHistory: false,
  }]);
  expect(JSON.stringify(state.accounts)).not.toContain("secret");
});


test("revealed credentials are removed from memory after sixty seconds", () => {
  vi.useFakeTimers();
  const state = createState();
  state.setReveal(7, {
    email: "user@example.com",
    password: "mail-secret",
    clientId: "client-id",
    refreshToken: "refresh-secret",
  });

  expect(state.getReveal(7)?.password).toBe("mail-secret");
  vi.advanceTimersByTime(59_999);
  expect(state.getReveal(7)?.password).toBe("mail-secret");
  vi.advanceTimersByTime(1);
  expect(state.getReveal(7)).toBeUndefined();
});


test("state replacement invalidates reveals for removed accounts", () => {
  const state = createState([{ id: 7, email: "user@example.com" }]);
  state.setReveal(7, {
    email: "user@example.com",
    password: "mail-secret",
    clientId: "client-id",
    refreshToken: "refresh-secret",
  });

  state.setAccounts([]);

  expect(state.getReveal(7)).toBeUndefined();
});


test("frontend state never accesses Web Storage", () => {
  const localSpy = vi.spyOn(Storage.prototype, "setItem");
  const sessionSpy = vi.spyOn(Storage.prototype, "setItem");
  const state = createState([{ id: 7, email: "user@example.com" }]);

  state.setAccounts([{ id: 8, email: "next@example.com" }]);
  state.setReveal(8, {
    email: "next@example.com",
    password: "secret",
    clientId: "client",
    refreshToken: "refresh",
  });

  expect(localSpy).not.toHaveBeenCalled();
  expect(sessionSpy).not.toHaveBeenCalled();
});
