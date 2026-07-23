// @vitest-environment jsdom

import { beforeEach, expect, test, vi } from "vitest";

import { bootstrapPickup } from "../../mail_manager/static/js/pickup-app.js";


beforeEach(() => {
  document.body.innerHTML = `
    <main data-pickup-root>
      <section id="pickup-login"><form id="pickup-login-form"><input id="pickup-code"><button type="submit">进入</button></form></section>
      <section id="pickup-dashboard" hidden>
        <span id="pickup-email"></span><span id="pickup-expiry"></span>
        <button id="pickup-logout"></button>
        <span id="pickup-account-email"></span>
        <span id="pickup-token-status"></span><span id="pickup-token-help"></span>
        <code id="pickup-password"></code>
        <button id="pickup-copy-email"></button>
        <button id="pickup-reveal-password"></button>
        <button id="pickup-copy-password"></button>
        <button data-pickup-folder="inbox"></button>
        <button data-pickup-folder="junkemail"></button>
        <input id="pickup-keyword">
        <button id="pickup-refresh"></button>
        <button id="pickup-verify"></button>
        <div class="mail-layout"><div id="pickup-mail-list"></div><div id="pickup-mail-detail"></div></div>
      </section>
      <p id="pickup-status"></p>
    </main>`;
});


test("anonymous pickup page remains logged out without a valid session", async () => {
  const api = new EventTarget();
  api.get = vi.fn().mockResolvedValue({ authenticated: false });
  api.post = vi.fn();
  api.setCsrfToken = vi.fn();
  api.clearCsrfToken = vi.fn();

  bootstrapPickup(document, api);
  await vi.waitFor(() => expect(api.get).toHaveBeenCalledWith("/api/pickup/check-auth"));

  expect(document.querySelector("#pickup-dashboard").hidden).toBe(true);
  expect(api.post).not.toHaveBeenCalled();
});


test("pickup login loads mail through account-id-free endpoints", async () => {
  const api = new EventTarget();
  api.get = vi.fn().mockResolvedValue({ authenticated: false });
  api.post = vi.fn()
    .mockResolvedValueOnce({
      authenticated: true,
      csrfToken: "csrf",
      email: "rented@example.test",
      expiresAt: new Date(Date.now() + 3600000).toISOString(),
      tokenValid: true,
    })
    .mockResolvedValueOnce([]);
  api.setCsrfToken = vi.fn();
  api.clearCsrfToken = vi.fn();
  api.abortAll = vi.fn();

  bootstrapPickup(document, api);
  document.querySelector("#pickup-code").value = "one-time-code";
  document.querySelector("#pickup-login-form").dispatchEvent(
    new Event("submit", { bubbles: true, cancelable: true }),
  );

  await vi.waitFor(() => expect(api.post).toHaveBeenCalledTimes(2));
  expect(api.post.mock.calls[0][0]).toBe("/api/pickup/login");
  expect(api.post.mock.calls[1]).toEqual([
    "/api/pickup/mail",
    { folder: "inbox", limit: 100 },
    { timeoutMs: 75000 },
  ]);
  expect(api.post.mock.calls.flat().join(" ")).not.toMatch(/accounts\/\d/);
  expect(document.querySelector("#pickup-email").textContent).toBe("rented@example.test");
});


test("authorization-needed pickup can reveal password without loading mail", async () => {
  const api = new EventTarget();
  api.get = vi.fn().mockResolvedValue({ authenticated: false });
  api.post = vi.fn()
    .mockResolvedValueOnce({
      authenticated: true,
      csrfToken: "csrf",
      email: "rented@example.test",
      expiresAt: new Date(Date.now() + 3600000).toISOString(),
      tokenValid: false,
    })
    .mockResolvedValueOnce({ password: "mailbox-password" });
  api.setCsrfToken = vi.fn();
  api.clearCsrfToken = vi.fn();
  api.abortAll = vi.fn();
  Object.assign(navigator, { clipboard: { writeText: vi.fn() } });

  bootstrapPickup(document, api);
  document.querySelector("#pickup-code").value = "one-time-code";
  document.querySelector("#pickup-login-form").dispatchEvent(
    new Event("submit", { bubbles: true, cancelable: true }),
  );
  await vi.waitFor(() => expect(document.querySelector("#pickup-token-status").textContent)
    .toBe("授权需处理"));

  expect(api.post).toHaveBeenCalledTimes(1);
  expect(document.querySelector("#pickup-refresh").disabled).toBe(true);
  document.querySelector("#pickup-reveal-password").click();
  await vi.waitFor(() => expect(api.post).toHaveBeenCalledWith(
    "/api/pickup/reveal-password", {},
  ));
  expect(document.querySelector("#pickup-password").textContent).toBe("mailbox-password");
});


test("revealed password clears after sixty seconds", async () => {
  vi.useFakeTimers();
  const api = new EventTarget();
  api.get = vi.fn().mockResolvedValue({ authenticated: true,
    csrfToken: "csrf", email: "rented@example.test",
    expiresAt: new Date(Date.now() + 3600000).toISOString(), tokenValid: false });
  api.post = vi.fn().mockResolvedValue({ password: "mailbox-password" });
  api.setCsrfToken = vi.fn();
  api.clearCsrfToken = vi.fn();
  api.abortAll = vi.fn();

  bootstrapPickup(document, api);
  await vi.advanceTimersByTimeAsync(0);
  document.querySelector("#pickup-reveal-password").click();
  await vi.advanceTimersByTimeAsync(0);
  expect(document.querySelector("#pickup-password").textContent).toBe("mailbox-password");

  await vi.advanceTimersByTimeAsync(60000);
  expect(document.querySelector("#pickup-password").textContent).toBe("••••••••");
  expect(document.querySelector("#pickup-copy-password").disabled).toBe(true);
  vi.useRealTimers();
});


test("pickup loads a message body only after selecting its summary", async () => {
  const api = new EventTarget();
  api.get = vi.fn().mockResolvedValue({ authenticated: false });
  api.post = vi.fn()
    .mockResolvedValueOnce({
      authenticated: true,
      csrfToken: "csrf",
      email: "rented@example.test",
      expiresAt: new Date(Date.now() + 3600000).toISOString(),
      tokenValid: true,
    })
    .mockResolvedValueOnce([{
      id: "message-1",
      subject: "Subject",
      sender: "sender@example.test",
      date: "2026-07-22T11:00:00Z",
      bodyPreview: "Preview",
    }])
    .mockResolvedValueOnce({
      id: "message-1",
      subject: "Subject",
      sender: "sender@example.test",
      date: "2026-07-22T11:00:00Z",
      bodyPreview: "Preview",
      html: "<b>Body</b>",
    });
  api.setCsrfToken = vi.fn();
  api.clearCsrfToken = vi.fn();
  api.abortAll = vi.fn();

  bootstrapPickup(document, api);
  document.querySelector("#pickup-code").value = "one-time-code";
  document.querySelector("#pickup-login-form").dispatchEvent(
    new Event("submit", { bubbles: true, cancelable: true }),
  );
  await vi.waitFor(() => expect(document.querySelector("[data-mail-index]")).not.toBeNull());
  document.querySelector("[data-mail-index]").click();

  await vi.waitFor(() => expect(api.post).toHaveBeenCalledTimes(3));
  expect(api.post.mock.calls[2]).toEqual([
    "/api/pickup/mail/message",
    { folder: "inbox", messageId: "message-1" },
    { timeoutMs: 75000 },
  ]);
});
