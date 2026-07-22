// @vitest-environment jsdom

import { beforeEach, expect, test } from "vitest";

import {
  renderAccountRow,
  renderMailDetail,
  renderMailList,
} from "../../mail_manager/static/js/render.js";


beforeEach(() => {
  document.body.replaceChildren();
});


test("account metadata is rendered as text", () => {
  const table = document.createElement("table");
  const rows = document.createElement("tbody");
  table.append(rows);
  document.body.append(table);

  renderAccountRow(rows, {
    id: 1,
    email: "<img src=x onerror=alert(1)>",
    bound: false,
    tokenValid: true,
  });

  expect(document.querySelector("img")).toBeNull();
  expect(document.body.textContent).toContain("<img src=x onerror=alert(1)>");
});


test("mail metadata never becomes executable markup", () => {
  const list = document.createElement("div");
  document.body.append(list);

  renderMailList(list, [{
    id: "message-1",
    subject: "<svg onload=alert(1)>",
    sender: "<img src=x onerror=alert(2)>",
    receivedAt: "2026-07-13T10:00:00Z",
  }]);

  expect(list.querySelector("svg")).toBeNull();
  expect(list.querySelector("img")).toBeNull();
  expect(list.textContent).toContain("<svg onload=alert(1)>");
  expect(list.textContent).toContain("<img src=x onerror=alert(2)>");
});


test("plain-text mail body is assigned through textContent", () => {
  const detail = document.createElement("section");
  document.body.append(detail);

  renderMailDetail(detail, {
    subject: "Plain message",
    sender: "sender@example.com",
    body: { contentType: "text", content: "<script>alert(1)</script>" },
  });

  expect(detail.querySelector("script")).toBeNull();
  expect(detail.querySelector("iframe")).toBeNull();
  expect(detail.textContent).toContain("<script>alert(1)</script>");
});