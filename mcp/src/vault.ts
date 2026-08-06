// @module: vault
// @role: every way the MCP server touches the vault, in one place - path resolution and its safety
//        boundary, note reading, the compiled index, the link graph, and the shell-out to semantic.py.
// @deps: node stdlib only. Nothing here re-implements what inkling already does in Python or Go.
// @used-by: index.ts (the tool surface)
//
// The rule this file exists to hold: THE SERVER IS A FACE, NOT A SECOND IMPLEMENTATION. Search is
// semantic.py invoked as a process; membership and the link graph are read from index_compiled.json,
// which compile_wiki_index.py builds; the note format is whatever VAULT_CONTRACT.md says. Re-deriving
// any of the three in TypeScript would create a second answer to a question that already has one,
// and the two would drift apart silently - which is exactly the failure mode this project keeps
// finding in its own history.

import { spawn } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import path from "node:path";

export class VaultError extends Error {}

// ---------------------------------------------------------------------------
//  Locations - from the environment, never from a literal
// ---------------------------------------------------------------------------

export function vaultRoot(): string {
  const raw = (process.env.CLAUDE_WIKI_PATH ?? "").trim();
  if (!raw) {
    throw new VaultError(
      "CLAUDE_WIKI_PATH is not set, so there is no vault to read. Point it at your Obsidian vault root."
    );
  }
  const root = path.resolve(raw);
  if (!existsSync(root)) {
    throw new VaultError(`CLAUDE_WIKI_PATH points at ${root}, which does not exist.`);
  }
  return root;
}

export function configDir(): string {
  const raw = (process.env.CLAUDE_CONFIG_DIR ?? "").trim();
  return raw ? path.resolve(raw) : path.join(homedir(), ".claude");
}

function recallDir(): string {
  return path.join(configDir(), "hooks", "recall");
}

/**
 * THE SECURITY BOUNDARY. Everything a client names is untrusted: the tools are reachable by any MCP
 * client, and `read` plus `write` over a caller-supplied path is how a note-taking server turns into
 * an arbitrary-file-read. A relative path is resolved against the vault and must still be inside it
 * afterwards - which is the only check that survives `..`, an absolute path, and a mixed separator.
 */
export function resolveInVault(relative: string): string {
  const root = vaultRoot();
  const target = path.resolve(root, relative.replace(/\\/g, "/"));
  const inside = target === root || target.startsWith(root + path.sep);
  if (!inside) {
    throw new VaultError(
      `"${relative}" resolves outside the vault. Paths are relative to the vault root and may not escape it.`
    );
  }
  return target;
}

// ---------------------------------------------------------------------------
//  The compiled index - membership, summaries and the link graph
// ---------------------------------------------------------------------------

export interface Entry {
  name: string;
  path: string;
  tags?: string[];
  summary?: string;
  related?: string[];
}

export function loadCompiled(corpus: string): Entry[] {
  const file =
    corpus === "" || corpus === "code" || corpus === "default"
      ? "index_compiled.json"
      : `index_compiled_${corpus}.json`;
  const full = path.join(recallDir(), file);
  if (!existsSync(full)) {
    throw new VaultError(
      `no compiled index at ${full} - run compile_wiki_index.py (SessionStart does it for you).`
    );
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(readFileSync(full, "utf-8"));
  } catch (err) {
    throw new VaultError(`the compiled index at ${full} is not readable JSON: ${String(err)}`);
  }
  if (!Array.isArray(parsed)) {
    throw new VaultError(`the compiled index at ${full} is not a list - rebuild it.`);
  }
  return parsed as Entry[];
}

/** A note is addressable by its bare name or by its vault-relative path; both are keys. */
function indexByKey(entries: Entry[]): Map<string, Entry> {
  const byKey = new Map<string, Entry>();
  for (const entry of entries) {
    if (entry.path) byKey.set(entry.path, entry);
    if (entry.name) byKey.set(entry.name, entry);
  }
  return byKey;
}

export interface Neighbor {
  name: string;
  path: string;
  summary: string;
  hop: number;
  via: string;
}

/**
 * Walk the note's own `related` edges outward.
 *
 * The hot-path hook walks exactly ONE edge from ONE match, deliberately, because a hop from a weak
 * match multiplies noise on every prompt whether anyone wanted it or not. That timidity is correct
 * for a push rail and wrong here: this is pulled on purpose, by a caller who already decided the
 * starting note is the right one, so depth is theirs to choose. Same graph, different licence.
 */
export function neighbors(start: string, depth: number): { origin: Entry; found: Neighbor[] } {
  const entries = loadCompiled("code");
  const byKey = indexByKey(entries);
  const origin = byKey.get(start) ?? byKey.get(start.replace(/\.md$/, ""));
  if (!origin) {
    throw new VaultError(
      `no note named "${start}" in the compiled index. Note names are filename stems (patterns/x.md is "x").`
    );
  }
  const seen = new Set<string>([origin.path, origin.name]);
  const found: Neighbor[] = [];
  let frontier: Entry[] = [origin];
  for (let hop = 1; hop <= Math.max(1, Math.min(depth, 3)); hop++) {
    const next: Entry[] = [];
    for (const node of frontier) {
      for (const raw of node.related ?? []) {
        // `[[wiki-link]]` is one of the three shapes the contract accepts; the compiler stores what
        // the author typed, so the shapes have to be understood here rather than assumed away.
        const ref = raw.trim().replace(/^\[\[|\]\]$/g, "");
        if (!ref || seen.has(ref)) continue;
        const hit = byKey.get(ref) ?? byKey.get(ref.replace(/\.md$/, ""));
        if (!hit) continue; // a link may point at a note that was deleted - the author's problem
        if (seen.has(hit.path) || seen.has(hit.name)) continue;
        seen.add(hit.path);
        seen.add(hit.name);
        found.push({
          name: hit.name,
          path: hit.path,
          summary: hit.summary ?? "",
          hop,
          via: node.name,
        });
        next.push(hit);
      }
    }
    if (next.length === 0) break;
    frontier = next;
  }
  return { origin, found };
}

// ---------------------------------------------------------------------------
//  Reading
// ---------------------------------------------------------------------------

export async function readNote(relative: string): Promise<{ path: string; text: string }> {
  const withExt = relative.endsWith(".md") ? relative : `${relative}.md`;
  let full = resolveInVault(withExt);
  if (!existsSync(full)) {
    // A caller usually has a NAME, because that is what search and the recall signal hand back.
    // Resolving it through the index costs one file read and removes a whole class of "not found"
    // that is really "you gave me the name and I wanted the path".
    const hit = indexByKey(loadCompiled("code")).get(relative.replace(/\.md$/, ""));
    if (!hit) {
      throw new VaultError(`no note at "${relative}", and no note by that name in the index.`);
    }
    full = resolveInVault(hit.path);
  }
  return { path: path.relative(vaultRoot(), full).replace(/\\/g, "/"), text: await readFile(full, "utf-8") };
}

// ---------------------------------------------------------------------------
//  Writing
// ---------------------------------------------------------------------------

const RAW_DIR = "raw";

/**
 * Material lands in `raw/`, never in the corpus.
 *
 * That is not caution, it is VAULT_CONTRACT.md section 8: "material enters as raw and leaves as a
 * note; nothing is authored straight into the corpus by a machine without passing that step." `raw/`
 * is excluded from retrieval by design, so anything dropped here is invisible until /compile turns
 * it into a note - which is where a human still sees it. A server that wrote straight into the
 * corpus would break the one rule the whole product rests on, and it would break it silently.
 */
export async function captureRaw(slug: string, content: string): Promise<string> {
  const safe = slug
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9Ѐ-ӿ._-]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 60);
  if (!safe) throw new VaultError("the slug is empty after cleaning - give it a name with letters in it.");
  const relative = `${RAW_DIR}/${safe}.md`;
  const full = resolveInVault(relative);
  await mkdir(path.dirname(full), { recursive: true });
  if (existsSync(full)) {
    // Appending beats overwriting: raw/ is a staging heap, and losing somebody's earlier capture to
    // a name collision is not recoverable from here.
    const previous = await readFile(full, "utf-8");
    await writeFile(full, `${previous.trimEnd()}\n\n---\n\n${content.trim()}\n`, "utf-8");
    return relative;
  }
  await writeFile(full, `${content.trim()}\n`, "utf-8");
  return relative;
}

/**
 * Add ONE `related` edge to an existing note.
 *
 * This is the one write that touches the corpus, and it is deliberate: a link is not content. It
 * asserts that two notes travel together, and the assertion is checkable - the target must exist in
 * the index, which is more than a human pasting a line ever verifies. `semantic.py suggest`
 * proposes and writes nothing on purpose; this is its committing half, kept separate so that
 * proposing stays free of consequence.
 */
export async function addRelated(noteRef: string, target: string): Promise<string> {
  const entries = loadCompiled("code");
  const byKey = indexByKey(entries);
  const note = byKey.get(noteRef) ?? byKey.get(noteRef.replace(/\.md$/, ""));
  if (!note) throw new VaultError(`no note named "${noteRef}" in the index.`);
  const targetEntry = byKey.get(target) ?? byKey.get(target.replace(/\.md$/, ""));
  if (!targetEntry) {
    throw new VaultError(
      `"${target}" is not a note in the index, so linking to it would create a dead edge.`
    );
  }
  if (note.path === targetEntry.path) throw new VaultError("a note cannot link to itself.");

  const full = resolveInVault(note.path);
  const text = await readFile(full, "utf-8");
  const fm = /^---\r?\n([\s\S]*?)\r?\n---/.exec(text);
  if (!fm) {
    throw new VaultError(
      `${note.path} has no frontmatter, so it is not in the corpus and a link there would do nothing.`
    );
  }
  // `fm[1]` is guaranteed by the capture group, but the compiler is right to insist: an index into a
  // match array is optional, and a silent `undefined` here would rewrite the file with the string
  // "undefined" as its frontmatter. Defaulting is cheaper than proving it cannot happen.
  const block = fm[1] ?? "";
  const link = targetEntry.name;
  if (new RegExp(`(^|[\\s\\[,])${escapeRegExp(link)}([\\s\\],]|$)`, "m").test(block)) {
    return `${note.path} already links to ${link}`;
  }

  let updated: string;
  const inline = /^related:[ \t]*\[([^\]]*)\][ \t]*$/m;
  const listHead = /^related:[ \t]*$/m;
  if (inline.test(block)) {
    updated = block.replace(inline, (_m: string, items: string) => {
      const body = items.trim();
      return `related: [${body ? `${body}, ${link}` : link}]`;
    });
  } else if (listHead.test(block)) {
    // Append after the LAST existing item so the list keeps its order and its indentation.
    const lines = block.split(/\r?\n/);
    const head = lines.findIndex((l) => listHead.test(l));
    let last = head;
    while (last + 1 < lines.length && /^[ \t]*-[ \t]*\S/.test(lines[last + 1] ?? "")) last++;
    const indent = /^([ \t]*)-/.exec(lines[head + 1] ?? "  - x")?.[1] ?? "  ";
    lines.splice(last + 1, 0, `${indent}- ${link}`);
    updated = lines.join("\n");
  } else {
    updated = `${block.trimEnd()}\nrelated:\n  - ${link}`;
  }
  await writeFile(full, text.replace(fm[0], `---\n${updated}\n---`), "utf-8");
  return `${note.path} now links to ${link}`;
}

function escapeRegExp(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

// ---------------------------------------------------------------------------
//  semantic.py - the search half, invoked, never reimplemented
// ---------------------------------------------------------------------------

export interface SearchHit {
  score: number;
  judge?: number;
  path: string;
  name: string;
  summary: string;
}

export interface SearchResult {
  query: string;
  corpus: string;
  model: string;
  judge: string | null;
  /** hits | rejected | no-index | unavailable - four different facts, never flattened to "empty". */
  verdict: string;
  note: string | null;
  results: SearchHit[];
}

/**
 * The interpreter is probed, not assumed. inkling's own install notes say `python3` on PATH, "on
 * Windows without it, replace python3 with py" - so both are tried, and INKLING_PYTHON overrides
 * when a machine keeps its interpreter somewhere else entirely.
 */
function pythonCandidates(): string[] {
  const explicit = (process.env.INKLING_PYTHON ?? "").trim();
  return explicit ? [explicit] : ["python3", "py", "python"];
}

export async function semanticSearch(
  query: string,
  corpus: string,
  k: number,
  rerank: boolean
): Promise<SearchResult> {
  const script = path.join(recallDir(), "semantic.py");
  if (!existsSync(script)) {
    throw new VaultError(`semantic.py is not at ${script} - is CLAUDE_CONFIG_DIR pointing at the install?`);
  }
  const args = [script, "search", query, "-k", String(k), "--corpus", corpus, "--json"];
  if (rerank) args.push("--rerank");

  let lastFailure = "";
  for (const bin of pythonCandidates()) {
    const out = await run(bin, args);
    if (out === null) {
      lastFailure = `${bin} is not on PATH`;
      continue;
    }
    if (out.code !== 0) {
      lastFailure = `${bin} exited ${out.code}: ${out.stderr.trim().slice(0, 300)}`;
      continue;
    }
    try {
      return JSON.parse(out.stdout.trim()) as SearchResult;
    } catch {
      lastFailure = `${bin} returned output that is not JSON: ${out.stdout.trim().slice(0, 200)}`;
    }
  }
  throw new VaultError(`could not run semantic.py (${lastFailure}). Set INKLING_PYTHON to your interpreter.`);
}

function run(
  bin: string,
  args: string[]
): Promise<{ code: number; stdout: string; stderr: string } | null> {
  return new Promise((resolve) => {
    const child = spawn(bin, args, { env: process.env });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (d) => (stdout += d.toString()));
    child.stderr.on("data", (d) => (stderr += d.toString()));
    child.on("error", () => resolve(null)); // interpreter not found - try the next candidate
    child.on("close", (code) => resolve({ code: code ?? 1, stdout, stderr }));
  });
}
