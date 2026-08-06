// Smoke test: speak MCP over stdio to the built server, exercise every tool, print what came back.
// Not a unit test - an end-to-end check that the protocol handshake works and each tool answers.
import { spawn } from "node:child_process";

const child = spawn("node", ["dist/index.js"], { env: process.env, stdio: ["pipe", "pipe", "pipe"] });
let buffer = "";
const pending = new Map();

child.stdout.on("data", (d) => {
  buffer += d.toString();
  let nl;
  while ((nl = buffer.indexOf("\n")) >= 0) {
    const line = buffer.slice(0, nl).trim();
    buffer = buffer.slice(nl + 1);
    if (!line) continue;
    let msg;
    try { msg = JSON.parse(line); } catch { continue; }
    if (msg.id !== undefined && pending.has(msg.id)) {
      pending.get(msg.id)(msg);
      pending.delete(msg.id);
    }
  }
});
child.stderr.on("data", (d) => process.stderr.write(`[server] ${d}`));

let nextId = 1;
function call(method, params) {
  const id = nextId++;
  return new Promise((resolve) => {
    pending.set(id, resolve);
    child.stdin.write(JSON.stringify({ jsonrpc: "2.0", id, method, params }) + "\n");
  });
}
function notify(method, params) {
  child.stdin.write(JSON.stringify({ jsonrpc: "2.0", method, params }) + "\n");
}

const text = (r) => (r.result?.content ?? []).map((c) => c.text).join("\n");

const init = await call("initialize", {
  protocolVersion: "2025-06-18",
  capabilities: {},
  clientInfo: { name: "smoke", version: "0" },
});
console.log("initialize ->", init.result?.serverInfo?.name, init.result?.serverInfo?.version);
notify("notifications/initialized", {});

const tools = await call("tools/list", {});
console.log("\ntools:");
for (const t of tools.result.tools) {
  console.log(`  ${t.name.padEnd(16)} ${t.description.slice(0, 84)}...`);
}

console.log("\n--- vault_search (rerank off, so this stays fast) ---");
const s = await call("tools/call", {
  name: "vault_search",
  arguments: { query: "how does a hook get the project directory", k: 3, rerank: false },
});
console.log(text(s).slice(0, 600));

console.log("\n--- vault_search on something the vault has nothing about ---");
const empty = await call("tools/call", {
  name: "vault_search",
  arguments: { query: "medieval falconry equipment prices", k: 3, rerank: false },
});
console.log(text(empty).slice(0, 400));

console.log("\n--- vault_neighbors ---");
const n = await call("tools/call", {
  name: "vault_neighbors",
  arguments: { note: "multi-machine-git-sync", depth: 2 },
});
console.log(text(n).slice(0, 600));

console.log("\n--- vault_read ---");
const r = await call("tools/call", { name: "vault_read", arguments: { note: "multi-machine-git-sync" } });
console.log(text(r).slice(0, 260), "...");

console.log("\n--- vault_read, path escape attempt (must be refused) ---");
const esc = await call("tools/call", {
  name: "vault_read",
  arguments: { note: "../../../../Windows/System32/drivers/etc/hosts" },
});
console.log("isError:", esc.result?.isError, "|", text(esc).slice(0, 180));

console.log("\n--- vault_link to a note that does not exist (must be refused) ---");
const bad = await call("tools/call", {
  name: "vault_link",
  arguments: { note: "multi-machine-git-sync", target: "no-such-note-anywhere" },
});
console.log("isError:", bad.result?.isError, "|", text(bad).slice(0, 180));

child.kill();
process.exit(0);
