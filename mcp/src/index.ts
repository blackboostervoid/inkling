#!/usr/bin/env node
// @module: index
// @role: the MCP face of inkling - five tools over the vault, and nothing else. Every tool is a thin
//        call into vault.ts; no retrieval, no parsing and no note format is decided here.
// @deps: @modelcontextprotocol/sdk, zod, ./vault
// @used-by: any MCP client (Claude Code, Claude Desktop, anything speaking the protocol)
//
// WHY THIS EXISTS, and it is not "so other clients can reach the vault".
//
// inkling already has a push rail: the recall hook fires on every prompt, cheap and automatic,
// and it is the reason the vault gets read at all. But cheap is a constraint, so the deep half -
// meaning-based search with a judge that can say "nothing here answers you" - had to live off the
// hot path, as a command somebody has to REMEMBER to type. Measured in a sibling project on
// 2026-08-06: a capability agents call ran 70 times in a week, and one mandated in prose ran 0 times
// out of 28 opportunities. Prose is not a channel.
//
// A tool carries its own description in the protocol. It is in front of the model every turn without
// anyone remembering anything. So the MCP is not a second door for external clients - it is the
// delivery mechanism that makes inkling's expensive half reachable at all. The push rail stays cheap
// and automatic; this is where depth lives.

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";

import {
  addRelated,
  captureRaw,
  neighbors,
  readNote,
  semanticSearch,
  VaultError,
  vaultRoot,
} from "./vault.js";

const server = new McpServer({ name: "inkling", version: "0.1.0" });

/** Every tool answers in text, and a failure is an ANSWER, not a crash: an MCP client shows the
 *  model whatever comes back, so a stated reason is worth more than a stack trace it cannot read. */
function ok(text: string) {
  return { content: [{ type: "text" as const, text }] };
}
function failed(err: unknown) {
  const message = err instanceof VaultError ? err.message : String(err);
  return { content: [{ type: "text" as const, text: `inkling: ${message}` }], isError: true };
}

// ---------------------------------------------------------------------------

server.registerTool(
  "vault_search",
  {
    title: "Search the vault by meaning",
    description:
      "Search the user's Obsidian vault by MEANING, not by keyword - it finds the note about " +
      "circuit breakers when the question says 'retry logic'. Use it whenever the answer might " +
      "already have been written down: a past decision, a gotcha hit before, an API's real " +
      "behaviour, why an approach was rejected. Cheap to try and worth trying before answering " +
      "from memory. With rerank on (the default) a local model reads the shortlist and may answer " +
      "that the vault holds NOTHING on the subject - that verdict is a real result, not a failure, " +
      "and it means you can stop looking here. " +
      "This searches the note BODIES at section granularity, which the automatic [RECALL] block " +
      "does not - that one only ever sees a note's title, tags and summary. So a note whose " +
      "summary is about another subject entirely, but which happens to answer this question in " +
      "one of its sections, is reachable ONLY here. Results name the sections that matched.",
    inputSchema: {
      query: z.string().describe("What you want to know, phrased as a question or a topic."),
      k: z.number().int().min(1).max(20).default(10).describe("How many notes to return."),
      rerank: z
        .boolean()
        .default(true)
        .describe("Let a local judge drop irrelevant hits and answer 'nothing here'. Slower, sharper."),
      corpus: z.string().default("code").describe("Which vault namespace to search."),
    },
  },
  async ({ query, k, rerank, corpus }) => {
    try {
      const found = await semanticSearch(query, corpus, k, rerank);
      // The four verdicts are four different facts and the caller has to be able to act on each.
      // Collapsing them into "no results" is how "the embedder is not running" gets reported to a
      // user as "your vault has nothing on this".
      if (found.verdict === "rejected") {
        return ok(`Nothing in the vault answers "${query}". ${found.note ?? ""}`.trim());
      }
      if (found.verdict === "unavailable" || found.verdict === "no-index") {
        return ok(`Search could not run: ${found.note ?? found.verdict}`);
      }
      // A corpus bound to another project. Deliberate scoping, not an outage and not an empty
      // vault - saying so plainly is what stops the caller from retrying or from reporting
      // "there is nothing on this" about a vault it was simply not allowed to open.
      if (found.verdict === "out-of-scope") {
        return ok(`Not searched: ${found.note ?? "this corpus is scoped to another project."}`);
      }
      if (found.results.length === 0) return ok(`No notes matched "${query}".`);
      const lines = found.results.map((r) => {
        const judged = r.judge !== undefined ? ` judge ${r.judge.toFixed(0)}/10` : "";
        // Naming the sections is what makes a whole-note result usable. The match happened at
        // section granularity; handing back only the note would leave the caller to re-find the
        // paragraph that pulled it in, and handing back the bare paragraph would strip the
        // context that keeps it honest. So: whole note, with a pointer to where to look.
        const where =
          r.matched && r.matched.length > 0
            ? `\n  matched in: ${r.matched.slice(0, 3).join(" · ")}`
            : "";
        return `- ${r.name} (${r.path})  score ${r.score.toFixed(3)}${judged}\n  ${r.summary}${where}`;
      });
      const engine = found.judge ? `${found.model} + ${found.judge}` : found.model;
      // The judge is optional and OFF unless CLAUDE_RERANK_MODEL names a model. Asking for it and
      // silently not getting it is the worst of the three states: this tool's description promises
      // that a "nothing here" verdict is possible, and without a judge it never is. Cosine always
      // ranks something first, so these hits are similarity, not relevance - and an unmade
      // judgement must never be handed over looking like a made one.
      const unjudged =
        rerank && found.judge === null
          ? `\n\n[relevance NOT checked - ${found.note ?? "no judge answered"}, so these are raw ` +
            "similarity matches and the search cannot tell you the vault has nothing on this.]"
          : "";
      const swapped =
        found.judge && found.judge_asked && found.judge !== found.judge_asked
          ? `\n\n[judged by ${found.judge}, not ${found.judge_asked} - ${found.note ?? "reason not given"}]`
          : "";
      // The pull rail's usage marker. The push rail has had one since it was built, which is the
      // only reason anything is known about which notes earn their place; this door has been
      // uncounted, so a note that works here has looked dead in the ledger. Named SMRITI against
      // the push rail's RECALL after the sruti/smrti pair - what arrives unbidden versus what is
      // fetched by an act of will, which is exactly the difference between the two doors.
      //
      // The placeholder below is written with angle brackets on purpose: the ledger credits only
      // a marker naming a note that actually exists, so this instruction can never credit itself.
      const marker =
        "\n\nIf one of these notes changes what you do, emit `[SMRITI-USED] <note-name> - " +
        "<one-line why>` in your reply. That marker is the only record this door leaves; without " +
        "it a note that earned its place here is indistinguishable from one nobody ever opened.";
      return ok(
        `${found.results.length} note(s) for "${query}"  [${engine}]\n\n${lines.join("\n")}${unjudged}${swapped}${marker}`
      );
    } catch (err) {
      return failed(err);
    }
  }
);

server.registerTool(
  "vault_read",
  {
    title: "Read a vault note",
    description:
      "Read one note from the vault in full. Search and the recall signal both hand back a summary " +
      "and a path; this is how you get the rest - the evidence, the numbers, the reasoning. Takes " +
      "either the note's bare name or its vault-relative path.",
    inputSchema: {
      note: z.string().describe("Note name (filename stem) or vault-relative path."),
    },
  },
  async ({ note }) => {
    try {
      const found = await readNote(note);
      return ok(`# ${found.path}\n\n${found.text}`);
    } catch (err) {
      return failed(err);
    }
  }
);

server.registerTool(
  "vault_neighbors",
  {
    title: "Follow a note's links",
    description:
      "List the notes a given note links to, and optionally their links in turn. The links were " +
      "written by hand as an assertion that two notes belong together, so this reaches material " +
      "that shares no words with your question and that search therefore cannot find. Use it after " +
      "a hit lands to see what the author thought travelled with it.",
    inputSchema: {
      note: z.string().describe("The note to start from - bare name or vault-relative path."),
      depth: z.number().int().min(1).max(3).default(1).describe("How many link hops to walk."),
    },
  },
  async ({ note, depth }) => {
    try {
      const { origin, found } = neighbors(note, depth);
      if (found.length === 0) {
        return ok(`${origin.name} links to nothing (no \`related\` entries that resolve to a live note).`);
      }
      const lines = found.map(
        (n) => `- [hop ${n.hop}, via ${n.via}] ${n.name} (${n.path})\n  ${n.summary}`
      );
      return ok(`${found.length} note(s) linked from ${origin.name}:\n\n${lines.join("\n")}`);
    } catch (err) {
      return failed(err);
    }
  }
);

server.registerTool(
  "vault_capture",
  {
    title: "Stage material into the vault",
    description:
      "Drop material into the vault's `raw/` staging area for later compiling into a note - a " +
      "finding worth keeping, a decision and its reason, a gotcha that cost time. It does NOT " +
      "create a searchable note: `raw/` is outside retrieval by design, and the /compile step is " +
      "where a human turns staged material into a note. Use it when something durable would " +
      "otherwise be lost at the end of the session.",
    inputSchema: {
      slug: z.string().describe("Short kebab-case name for the file, e.g. 'ipv6-loopback-tax'."),
      content: z.string().describe("The material itself, in markdown."),
    },
  },
  async ({ slug, content }) => {
    try {
      const written = await captureRaw(slug, content);
      return ok(
        `Staged to ${written}. It is NOT searchable yet - run /compile to turn it into a note.`
      );
    } catch (err) {
      return failed(err);
    }
  }
);

server.registerTool(
  "vault_link",
  {
    title: "Link two notes",
    description:
      "Record that two existing notes belong together, by adding one `related` entry to the first. " +
      "A link is what lets a note be reached from a question that shares no words with it. Both " +
      "notes must already exist - a link to a missing note is refused rather than written. Use it " +
      "when you notice two notes answering each other and neither one says so.",
    inputSchema: {
      note: z.string().describe("The note that gains the link."),
      target: z.string().describe("The note it should point at."),
    },
  },
  async ({ note, target }) => {
    try {
      return ok(await addRelated(note, target));
    } catch (err) {
      return failed(err);
    }
  }
);

// ---------------------------------------------------------------------------

async function main(): Promise<void> {
  // Fail here, loudly, rather than on the first tool call: a server that starts happily with no
  // vault configured looks healthy in the client's tool list and then refuses every single call.
  try {
    vaultRoot();
  } catch (err) {
    process.stderr.write(`inkling MCP cannot start: ${err instanceof Error ? err.message : String(err)}\n`);
    process.exit(1);
  }
  await server.connect(new StdioServerTransport());
}

main().catch((err) => {
  process.stderr.write(`inkling MCP crashed: ${String(err)}\n`);
  process.exit(1);
});
