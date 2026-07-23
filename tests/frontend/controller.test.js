// @vitest-environment jsdom

import fs from "node:fs/promises";

import { beforeEach, expect, test, vi } from "vitest";

import { bootstrap } from "../../mail_manager/static/js/app.js";


class FakeApi extends EventTarget {
  constructor() {
    super();
    this.setCsrfToken = vi.fn();
    this.clearCsrfToken = vi.fn();
    this.abortAll = vi.fn();
    this.get = vi.fn(async (path) => {
      if (path === "/api/check-auth") {
        return { authenticated: true, csrfToken: "csrf-value" };
      }
      if (path === "/api/accounts") {
        return [{ id: 7, email: "user@example.com", bound: false, tokenValid: true }];
      }
      throw new Error(`unexpected GET ${path}`);
    });
    this.post = vi.fn(async (path) => {
      if (path === "/api/accounts/7/mail") {
        return [];
      }
      if (path === "/api/accounts/7/verify-code") {
        return { code: "482913" };
      }
      if (path === "/api/accounts/7/reveal") {
        return {
          email: "user@example.com",
          password: "mail-secret",
          clientId: "client-id",
          refreshToken: "refresh-secret",
        };
      }
      if (path === "/api/accounts/check-bound") {
        return [{ id: 7, bound: false }];
      }
      if (path === "/api/accounts/random-unbound") {
        return { id: 7, email: "user@example.com" };
      }
      throw new Error(`unexpected POST ${path}`);
    });
    this.patch = vi.fn();
    this.delete = vi.fn();
  }
}


async function mountPage(api = new FakeApi()) {
  const html = await fs.readFile("mail_manager/static/index.html", "utf8");
  const parsed = new DOMParser().parseFromString(html, "text/html");
  document.head.replaceChildren(...parsed.head.childNodes);
  document.body.replaceChildren(...parsed.body.childNodes);
  document.body.setAttribute("data-app-root", "");
  bootstrap(document, api);
  return api;
}


async function loadPage(api = new FakeApi()) {
  await mountPage(api);
  await vi.waitFor(() => {
    expect(document.querySelector(".account-email")?.textContent).toBe("user@example.com");
  });
  return api;
}


beforeEach(() => {
  vi.useRealTimers();
  document.head.replaceChildren();
  document.body.replaceChildren();
});


test("mailbox controls use the exact account-id API contracts", async () => {
  const api = await loadPage();

  document.querySelector('[data-action="mail"]').click();
  await vi.waitFor(() => {
    expect(api.post).toHaveBeenCalledWith("/api/accounts/7/mail", {
      folder: "inbox",
      limit: 100,
    }, { timeoutMs: 75000 });
  });

  document.querySelector('[data-folder="junkemail"]').click();
  await vi.waitFor(() => {
    expect(api.post).toHaveBeenCalledWith("/api/accounts/7/mail", {
      folder: "junkemail",
      limit: 100,
    }, { timeoutMs: 75000 });
  });

  document.querySelector("#mail-keyword").value = "OpenAI";
  document.querySelector("#verify-code").click();
  await vi.waitFor(() => {
    expect(api.post).toHaveBeenCalledWith("/api/accounts/7/verify-code", {
      keyword: "OpenAI",
      maxResults: 100,
    }, { timeoutMs: 75000 });
  });
});


test("bound check and random lookup submit only account metadata", async () => {
  const api = await loadPage();
  const checkbox = document.querySelector(".account-select");
  checkbox.checked = true;
  checkbox.dispatchEvent(new Event("change", { bubbles: true }));

  document.querySelector("#check-bound").click();
  document.querySelector("#random-unbound").click();

  await vi.waitFor(() => {
    expect(api.post).toHaveBeenCalledWith("/api/accounts/check-bound", { ids: [7] });
    expect(api.post).toHaveBeenCalledWith("/api/accounts/random-unbound", {});
  });
});


test("revealed credential text is erased after sixty seconds", async () => {
  const api = await loadPage();
  vi.useFakeTimers();

  document.querySelector('[data-action="reveal"]').click();
  await vi.advanceTimersByTimeAsync(0);
  expect(document.querySelector("#secret-password").textContent).toBe("mail-secret");

  await vi.advanceTimersByTimeAsync(60_000);
  expect(document.querySelector("#secret-password").textContent).toBe("");
  expect(document.querySelector("#secret-refreshToken").textContent).toBe("");
});


test("a stale mailbox response cannot replace the newly selected folder", async () => {
  const api = await loadPage();
  let resolveInbox;
  let resolveJunk;
  api.post.mockImplementation((_path, body) => new Promise((resolve) => {
    if (body.folder === "inbox") {
      resolveInbox = resolve;
    } else {
      resolveJunk = resolve;
    }
  }));

  document.querySelector('[data-action="mail"]').click();
  await vi.waitFor(() => expect(resolveInbox).toBeTypeOf("function"));
  document.querySelector('[data-folder="junkemail"]').click();
  await vi.waitFor(() => expect(resolveJunk).toBeTypeOf("function"));

  resolveJunk([{ id: "junk", subject: "Junk result", sender: "j@example.com", date: "now" }]);
  await vi.waitFor(() => expect(document.querySelector(".mail-subject")?.textContent).toBe("Junk result"));
  resolveInbox([{ id: "inbox", subject: "Stale inbox", sender: "i@example.com", date: "before" }]);
  await Promise.resolve();
  await Promise.resolve();

  expect(document.querySelector(".mail-subject")?.textContent).toBe("Junk result");
});


test("an oversized import file is rejected before file.text is called", async () => {
  const api = await loadPage();
  const readText = vi.fn();
  Object.defineProperty(document.querySelector("#import-file"), "files", {
    configurable: true,
    value: [{ size: (2 * 1024 * 1024) + 1, text: readText }],
  });

  document.querySelector("#import-form").dispatchEvent(new Event("submit", {
    bubbles: true,
    cancelable: true,
  }));
  await Promise.resolve();

  expect(readText).not.toHaveBeenCalled();
  expect(document.querySelector("#app-status").textContent).toMatch(/2 MiB/);
  expect(api.post).not.toHaveBeenCalledWith("/api/accounts/import", expect.anything());
});


test("logout prevents a deferred reveal from restoring secret DOM", async () => {
  const api = await loadPage();
  let resolveReveal;
  api.post.mockImplementation((path) => {
    if (path === "/api/accounts/7/reveal") {
      return new Promise((resolve) => { resolveReveal = resolve; });
    }
    if (path === "/api/logout") {
      return Promise.resolve({ authenticated: false });
    }
    throw new Error(`unexpected POST ${path}`);
  });

  document.querySelector('[data-action="reveal"]').click();
  await vi.waitFor(() => expect(resolveReveal).toBeTypeOf("function"));
  document.querySelector("#logout-button").click();
  resolveReveal({
    email: "user@example.com",
    password: "late-secret",
    clientId: "client-id",
    refreshToken: "late-refresh",
  });
  await Promise.resolve();
  await Promise.resolve();

  expect(api.abortAll).toHaveBeenCalled();
  expect(document.querySelector("#secret-password").textContent).toBe("");
  expect(document.querySelector("#dashboard").hidden).toBe(true);
});


test("logout prevents deferred mail from rendering after session cleanup", async () => {
  const api = await loadPage();
  let resolveMail;
  api.post.mockImplementation((path) => {
    if (path === "/api/accounts/7/mail") {
      return new Promise((resolve) => { resolveMail = resolve; });
    }
    if (path === "/api/logout") {
      return Promise.resolve({ authenticated: false });
    }
    throw new Error(`unexpected POST ${path}`);
  });

  document.querySelector('[data-action="mail"]').click();
  await vi.waitFor(() => expect(resolveMail).toBeTypeOf("function"));
  document.querySelector("#logout-button").click();
  resolveMail([{ id: "late", subject: "Late mail", sender: "x@example.com", date: "now" }]);
  await Promise.resolve();
  await Promise.resolve();

  expect(document.querySelector(".mail-subject")).toBeNull();
  expect(document.querySelector("#mail-dialog").hasAttribute("open")).toBe(false);
});


test("closing the mail dialog invalidates a deferred verification code", async () => {
  const api = await loadPage();
  document.querySelector('[data-action="mail"]').click();
  await vi.waitFor(() => {
    expect(api.post).toHaveBeenCalledWith("/api/accounts/7/mail", {
      folder: "inbox",
      limit: 100,
    }, { timeoutMs: 75000 });
  });
  let resolveCode;
  api.post.mockImplementation((path) => {
    if (path === "/api/accounts/7/verify-code") {
      return new Promise((resolve) => { resolveCode = resolve; });
    }
    throw new Error(`unexpected POST ${path}`);
  });

  document.querySelector("#verify-code").click();
  await vi.waitFor(() => expect(resolveCode).toBeTypeOf("function"));
  document.querySelector('[data-close-dialog="#mail-dialog"]').click();
  resolveCode({ code: "999999" });
  await Promise.resolve();
  await Promise.resolve();

  expect(document.querySelector("#app-status").textContent).not.toContain("999999");
});


test.each([
  { startupAuth: { authenticated: false }, label: "signed-out" },
  {
    startupAuth: { authenticated: true, csrfToken: "stale-startup-csrf" },
    label: "signed-in",
  },
])("a late $label startup check cannot override a successful login", async ({ startupAuth }) => {
  const api = new FakeApi();
  let resolveStartup;
  api.get.mockImplementation((path) => {
    if (path === "/api/check-auth") {
      return new Promise((resolve) => { resolveStartup = resolve; });
    }
    if (path === "/api/accounts") {
      return Promise.resolve([]);
    }
    throw new Error(`unexpected GET ${path}`);
  });
  api.post.mockImplementation((path) => {
    if (path === "/api/login") {
      return Promise.resolve({ authenticated: true, csrfToken: "fresh-login-csrf" });
    }
    throw new Error(`unexpected POST ${path}`);
  });
  await mountPage(api);
  document.querySelector("#login-password").value = "correct password";

  document.querySelector("#login-form").dispatchEvent(new Event("submit", {
    bubbles: true,
    cancelable: true,
  }));
  await vi.waitFor(() => {
    expect(api.setCsrfToken).toHaveBeenCalledWith("fresh-login-csrf");
    expect(document.querySelector("#dashboard").hidden).toBe(false);
  });

  resolveStartup(startupAuth);
  await Promise.resolve();
  await Promise.resolve();

  expect(document.querySelector("#dashboard").hidden).toBe(false);
  expect(api.setCsrfToken).not.toHaveBeenCalledWith("stale-startup-csrf");
  expect(api.setCsrfToken).toHaveBeenLastCalledWith("fresh-login-csrf");
});


test.each(["success", "failure"])(
  "login waits for a pending logout after logout %s",
  async (outcome) => {
    const api = await loadPage();
    let settleLogout;
    const logout = new Promise((resolve, reject) => {
      settleLogout = outcome === "success" ? resolve : reject;
    });
    api.post.mockImplementation((path) => {
      if (path === "/api/logout") {
        return logout;
      }
      if (path === "/api/login") {
        return Promise.resolve({ authenticated: true, csrfToken: "new-session-csrf" });
      }
      throw new Error(`unexpected POST ${path}`);
    });

    document.querySelector("#logout-button").click();
    expect(document.querySelector("#dashboard").hidden).toBe(true);
    document.querySelector("#login-password").value = "correct password";
    document.querySelector("#login-form").dispatchEvent(new Event("submit", {
      bubbles: true,
      cancelable: true,
    }));
    await Promise.resolve();

    expect(api.post.mock.calls.map(([path]) => path)).toEqual(["/api/logout"]);

    settleLogout(outcome === "success" ? { authenticated: false } : new Error("logout failed"));
    await vi.waitFor(() => {
      expect(api.post).toHaveBeenCalledWith(
        "/api/login",
        { password: "correct password" },
        { notifyUnauthorized: false },
      );
      expect(api.setCsrfToken).toHaveBeenCalledWith("new-session-csrf");
    });
  },
);


test("a late startup check rejection cannot hide a successful login", async () => {
  const api = new FakeApi();
  let rejectStartup;
  api.get.mockImplementation((path) => {
    if (path === "/api/check-auth") {
      return new Promise((_resolve, reject) => { rejectStartup = reject; });
    }
    if (path === "/api/accounts") {
      return Promise.resolve([]);
    }
    throw new Error(`unexpected GET ${path}`);
  });
  api.post.mockImplementation((path) => {
    if (path === "/api/login") {
      return Promise.resolve({ authenticated: true, csrfToken: "fresh-login-csrf" });
    }
    throw new Error(`unexpected POST ${path}`);
  });
  await mountPage(api);
  document.querySelector("#login-password").value = "correct password";
  document.querySelector("#login-form").dispatchEvent(new Event("submit", {
    bubbles: true,
    cancelable: true,
  }));
  await vi.waitFor(() => {
    expect(document.querySelector("#dashboard").hidden).toBe(false);
  });

  rejectStartup(new Error("startup timeout"));
  await Promise.resolve();
  await Promise.resolve();

  expect(document.querySelector("#dashboard").hidden).toBe(false);
  expect(document.querySelector("#app-status").textContent).not.toContain("startup timeout");
  expect(api.setCsrfToken).toHaveBeenLastCalledWith("fresh-login-csrf");
});


test("a second login submit cannot start while the first login is pending", async () => {
  const api = new FakeApi();
  let resolveLogin;
  api.get.mockImplementation((path) => {
    if (path === "/api/check-auth") {
      return Promise.resolve({ authenticated: false });
    }
    if (path === "/api/accounts") {
      return Promise.resolve([]);
    }
    throw new Error(`unexpected GET ${path}`);
  });
  api.post.mockImplementation((path) => {
    if (path === "/api/login") {
      return new Promise((resolve) => { resolveLogin = resolve; });
    }
    throw new Error(`unexpected POST ${path}`);
  });
  await mountPage(api);
  const form = document.querySelector("#login-form");
  document.querySelector("#login-password").value = "correct password";

  form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
  await vi.waitFor(() => expect(resolveLogin).toBeTypeOf("function"));
  form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
  await Promise.resolve();

  expect(api.post.mock.calls.filter(([path]) => path === "/api/login")).toHaveLength(1);
  resolveLogin({ authenticated: true, csrfToken: "single-login-csrf" });
  await vi.waitFor(() => {
    expect(api.setCsrfToken).toHaveBeenCalledWith("single-login-csrf");
  });
  expect(api.post.mock.calls.filter(([path]) => path === "/api/login")).toHaveLength(1);
  expect(api.setCsrfToken).toHaveBeenLastCalledWith("single-login-csrf");
});


test("invalid login credentials remain a local form error", async () => {
  const api = new FakeApi();
  api.get.mockImplementation((path) => {
    if (path === "/api/check-auth") {
      return Promise.resolve({ authenticated: false });
    }
    throw new Error(`unexpected GET ${path}`);
  });
  api.post.mockRejectedValue(new Error("invalid credentials"));
  await mountPage(api);
  document.querySelector("#login-password").value = "wrong password";

  document.querySelector("#login-form").dispatchEvent(new Event("submit", {
    bubbles: true,
    cancelable: true,
  }));
  await vi.waitFor(() => {
    expect(document.querySelector("#app-status").textContent).toBe("invalid credentials");
  });

  expect(api.post).toHaveBeenCalledWith(
    "/api/login",
    { password: "wrong password" },
    { notifyUnauthorized: false },
  );
  expect(api.abortAll).not.toHaveBeenCalled();
  expect(document.querySelector("#login-view").hidden).toBe(false);
});
