// @vitest-environment jsdom

import { expect, test } from "vitest";

import { createMailFrame } from "../../mail_manager/static/js/mail-frame.js";


test("mail HTML receives an opaque scriptless sandbox", () => {
  const frame = createMailFrame("<img src=x onerror=alert(1)><script>alert(2)</script>");

  expect(frame.getAttribute("sandbox")).toBe("");
  expect(frame.referrerPolicy).toBe("no-referrer");
  expect(frame.title).toBe("邮件正文");
  expect(frame.srcdoc).toContain("default-src 'none'");
  expect(frame.srcdoc).toContain("script-src 'none'");
  expect(frame.srcdoc).toContain("img-src data:");
  expect(frame.srcdoc).toContain("style-src 'unsafe-inline'");
  expect(frame.srcdoc).toContain("form-action 'none'");
  expect(frame.srcdoc).toContain("base-uri 'none'");
});


test("mail frame does not grant scripts or same-origin access", () => {
  const frame = createMailFrame("<p>Message</p>");
  const sandbox = frame.getAttribute("sandbox");

  expect(sandbox).toBe("");
  expect(sandbox).not.toContain("allow-scripts");
  expect(sandbox).not.toContain("allow-same-origin");
  expect(frame.getAttribute("src")).toBeNull();
});
