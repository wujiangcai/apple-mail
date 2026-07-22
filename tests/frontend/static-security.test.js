import fs from "node:fs/promises";
import path from "node:path";

import { expect, test } from "vitest";


const STATIC_ROOT = path.resolve("mail_manager/static");


async function readJavaScript() {
  const directory = path.join(STATIC_ROOT, "js");
  const names = (await fs.readdir(directory)).filter((name) => name.endsWith(".js"));
  return (await Promise.all(names.map(async (name) => [
    name,
    await fs.readFile(path.join(directory, name), "utf8"),
  ])));
}


test("published HTML contains no inline executable content", async () => {
  const html = await fs.readFile(path.join(STATIC_ROOT, "index.html"), "utf8");

  expect(html).toMatch(/<form[^>]+id="login-form"[^>]+method="post"[^>]+action="\/api\/login"/i);
  expect(html).not.toMatch(/\son[a-z]+\s*=/i);
  expect(html).not.toMatch(/<script(?![^>]+\bsrc=)[^>]*>/i);
  expect(html).not.toMatch(/https?:\/\//i);
});


test("static modules contain no dangerous DOM or storage sinks", async () => {
  const files = await readJavaScript();

  for (const [name, source] of files) {
    expect(source, name).not.toMatch(/\.(?:innerHTML|outerHTML)\b/);
    expect(source, name).not.toMatch(/insertAdjacentHTML/);
    expect(source, name).not.toMatch(/\b(?:localStorage|sessionStorage)\b/);
    expect(source, name).not.toMatch(/document\.write/);
  }
});


test("credentials never appear in URL construction", async () => {
  const files = await readJavaScript();

  for (const [name, source] of files) {
    expect(source, name).not.toMatch(/[?&](?:refresh[_-]?token|password)=/i);
    expect(source, name).not.toMatch(/URLSearchParams\s*\([^)]*(?:token|password)/i);
  }
});
