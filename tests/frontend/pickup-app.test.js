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
        <button data-pickup-folder="inbox"></button>
        <button data-pickup-folder="junkemail"></button>
        <input id="pickup-keyword">
        <button id="pickup-verify"></button>
        <div id="pickup-mail-list"></div><div id="pickup-mail-detail"></div>
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
  ]);
  expect(api.post.mock.calls.flat().join(" ")).not.toMatch(/accounts\/\d/);
  expect(document.querySelector("#pickup-email").textContent).toBe("rented@example.test");
});
