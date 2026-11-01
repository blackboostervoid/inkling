package main

// The refusal stage.
//
// The fused score ranks notes by TOPICAL similarity, and the live telemetry says that is not
// the same question as "would this note change the answer". Measured 2026-08-06 on 457 real
// firings across 27 sessions: 11 were ever marked [RECALL-USED], and the score separates those
// 11 from the other 446 with AUC 0.548 — a coin flip. Sweeping the floor across its whole range
// never moves precision off the 2.4% base rate; above 0.0325 it reaches 0%, meaning the
// HIGHEST-scoring firings were applied least often of all. So there is no floor to find. A
// threshold can only ever answer "is this about the same subject", and 446 times out of 457 the
// answer was yes and the note was still noise.
//
// The cure is the one the code-map rail already proved on its own corpus, where four separate
// gate candidates were tried and none separated the classes either: a second stage that can say
// NOTHING. A threshold cannot — it always ranks something first. A judgement can.
//
// Three outcomes, never two. The third exists because collapsing it is how "the local model is
// not running" reaches a reader as "the wiki holds nothing on this":
//
//	kept, judged=true   — the judge ruled; whatever it returned is what the agent sees.
//	  (an empty keep is a real verdict, and it means the rail stays silent)
//	nil,  judged=false  — the judgement could NOT be made (switched off, ollama down, timeout,
//	  unparseable reply). The caller must fall back to raw matches AND say so out loud.

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"
)

const (
	judgeModelDefault = "gemma-4-12b-it"
	judgeModelEnvVar  = "RECALL_JUDGE_MODEL"
	judgeQueryRunes   = 4000

	// The ONLY deadline written here, and it is the one used when the budget artifact cannot
	// be read at all. Everything else comes from cache/hook_budgets.json, because a deadline
	// written next to another number is a deadline nobody re-checks: this constant used to say
	// 25 s inside a hook Claude Code cancels at 5, and the comment beside it explained that the
	// 25 s existed so a cold model "must not hold the prompt hostage" — on exactly that turn
	// the parent killed the process and the reader saw nothing at all.
	judgeFallbackTimeout = 1000 * time.Millisecond

	// Set to "0" to fall back to floor-only recall (raw matches, loudly marked unchecked).
	judgeEnvVar = "RECALL_JUDGE"

	// LITERAL IPv4 address, never the name "localhost". ollama binds IPv4 only; on Windows
	// `localhost` resolves to ::1 first and the refused loopback connection costs 2.041s
	// before the fallback — measured, and it was the entire cost of this rail before the fix.
	// curl cannot show this because it races both address families (Happy Eyeballs).
	judgeDefaultURL = "http://127.0.0.1:11434"

	// The key this program looks itself up by in the budget artifact: the basename of the
	// binary settings.json actually runs.
	judgeBudgetKey = "recall_scanner.exe"
)

var judgeModel = judgeModelFromEnv()

var judgeSchema = map[string]any{
	"type": "object",
	"properties": map[string]any{
		"useful": map[string]any{"type": "array", "items": map[string]any{"type": "integer"}},
	},
	"required": []string{"useful"},
}

func judgeModelFromEnv() string {
	if v := strings.TrimSpace(os.Getenv(judgeModelEnvVar)); v != "" {
		return v
	}
	return judgeModelDefault
}

func judgeQuery(prompt string) string {
	runes := []rune(strings.TrimSpace(prompt))
	if len(runes) > judgeQueryRunes {
		runes = runes[:judgeQueryRunes]
	}
	return string(runes)
}

// judgeBudget returns how long the judge may take, and where that number came from.
//
// Measured 2026-08-24: one warm judge call costs 3130 ms and the rest of the rail costs 50 ms,
// against a declared hook budget of 5000 ms. So a warm judge fits with room, and a cold one
// now fails on ITS OWN clock inside the hook's lifetime — which is what makes the degradation
// at main.go:1219-1222 reachable for the first time.
//
// The second return value is not decoration. A fallback that does not announce itself is the
// blindness this whole change removes, so the caller folds it into the note the reader sees.
func judgeBudget() (time.Duration, string) {
	root := os.Getenv("CLAUDE_CONFIG_DIR")
	if root == "" {
		return judgeFallbackTimeout, "no CLAUDE_CONFIG_DIR"
	}
	raw, err := os.ReadFile(filepath.Join(root, "cache", "hook_budgets.json"))
	if err != nil {
		return judgeFallbackTimeout, "budget artifact unreadable"
	}
	var table struct {
		Hooks map[string]struct {
			ChildMS int `json:"child_ms"`
		} `json:"hooks"`
	}
	if err := json.Unmarshal(raw, &table); err != nil {
		return judgeFallbackTimeout, "budget artifact malformed"
	}
	row, ok := table.Hooks[judgeBudgetKey]
	if !ok || row.ChildMS <= 0 {
		return judgeFallbackTimeout, "no budget row for " + judgeBudgetKey
	}
	return time.Duration(row.ChildMS) * time.Millisecond, ""
}

// There is deliberately NO minimum-prompt-length guard here, and the first live probe is why.
// One was written, at 24 characters, to avoid paying the judge on "ok" / "go". It fired on
// "лучший рецепт борща" — 20 characters, the exact false positive this whole stage exists to
// kill — and handed back three raw notes stamped RELEVANCE NOT CHECKED. A short prompt that
// somehow produced matches is not a cheap turn to skip; it is the noisiest turn there is.
//
// The guard that actually costs nothing is already in place: the judge is only reached when
// the floor produced matches, so a turn the rail was going to be silent on never contacts
// the model at all.

const judgePrompt = `A user wrote the message below to a coding assistant. A keyword and BM25 search
returned the wiki notes listed after it. Similarity is NOT relevance: the search always returns its
closest few, even when the wiki holds nothing that bears on the message at all.

Decide which listed notes would GENUINELY change how the assistant answers THIS message — not which
share a subject with it. A note that is merely on a related topic is noise, and noise in the context
window reads to the assistant as work the system did for it.

Return an EMPTY list when the message is small talk, a general programming question, or about
anything the listed notes do not concretely help with. That is the expected answer far more often
than not.

USER MESSAGE:
%s

NOTES:
%s

Return ONLY compact JSON: {"useful":[numbers]}`

// judgeURL is the ollama endpoint, overridable for tests and for a machine that moved the port.
var judgeURL = func() string {
	if v := strings.TrimSpace(os.Getenv("OLLAMA_URL")); v != "" {
		return strings.TrimRight(v, "/")
	}
	return judgeDefaultURL
}()

// judgeEnabled reports whether the refusal stage should run at all.
func judgeEnabled() bool {
	return strings.TrimSpace(os.Getenv(judgeEnvVar)) != "0"
}

// JudgeOutcome is why the rail is about to say what it says. It replaced a bool, and the
// reason is worth keeping: seven different failures used to arrive as `false` — the switch
// being off, a marshalling bug, a dead server, a non-200, an unreadable envelope, an
// unparseable verdict, and a deadline. The note shown to the reader said "the local judge was
// unavailable" for all seven, so nobody could tell a deliberate opt-out from a broken model,
// and the one that was actually happening could not be found from outside.
type JudgeOutcome int

const (
	// A real verdict. An EMPTY kept slice is still this: it means "none of these".
	JudgeRuled JudgeOutcome = iota
	// Nothing to judge, or RECALL_JUDGE=0. Never an alarm — this is the documented way to
	// run the rail without a local model, and alarming on it would fire every prompt.
	JudgeOff
	// The call did not finish inside the budget this hook was given.
	JudgeDeadline
	// The server could not be reached at all, for a reason that was not the clock.
	JudgeUnreachable
	// The server answered, and the answer was not a verdict this rail can read.
	JudgeUnparseable
)

// isDeadline reports whether err is the clock running out rather than anything else.
//
// TWO call sites need this, and instrumenting only the obvious one misses half the aborts:
// http.Client.Timeout covers the WHOLE request lifecycle, so an expiry can surface either at
// the Post or later at the body decode.
func isDeadline(err error) bool {
	if errors.Is(err, context.DeadlineExceeded) || errors.Is(err, os.ErrDeadlineExceeded) {
		return true
	}
	var netErr net.Error
	return errors.As(err, &netErr) && netErr.Timeout()
}

// judgeMatches asks the local model which of these notes actually bear on the prompt.
//
// Returns (kept, JudgeRuled) when a judgement was made — an empty slice is a real verdict
// meaning "none of these", and the caller should stay silent. Every other outcome returns nil
// and names itself, so the caller can say WHICH nothing it is reporting.
//
// Costs nothing when the rail was not going to speak: the caller only reaches here with matches
// in hand, so a turn that found nothing never contacts the model.
func judgeMatches(prompt string, matches []Match) ([]Match, JudgeOutcome) {
	if len(matches) == 0 || !judgeEnabled() {
		return nil, JudgeOff
	}

	listing := make([]string, 0, len(matches))
	for i, m := range matches {
		listing = append(listing, fmt.Sprintf("%d. %s — %s", i+1, m.Entry.Name, judgeGist(m.Entry)))
	}

	payload := map[string]any{
		"model":  judgeModel,
		"stream": false,
		"format": judgeSchema,
		"think":  false,
		"prompt": fmt.Sprintf(judgePrompt, judgeQuery(prompt), strings.Join(listing, "\n")),
		// Temperature 0 + a fixed seed: a rail that fires on every prompt has to be
		// reproducible, or no measurement taken against it can be trusted later.
		"options": map[string]any{"num_predict": 200, "temperature": 0.0, "seed": 7},
	}
	body, err := json.Marshal(payload)
	if err != nil {
		return nil, JudgeUnparseable
	}

	budget, _ := judgeBudget()
	client := &http.Client{Timeout: budget}
	resp, err := client.Post(judgeURL+"/api/generate", "application/json", bytes.NewReader(body))
	if err != nil {
		if isDeadline(err) {
			return nil, JudgeDeadline
		}
		return nil, JudgeUnreachable
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, JudgeUnparseable
	}

	var envelope struct {
		Response string `json:"response"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&envelope); err != nil {
		if isDeadline(err) {
			return nil, JudgeDeadline
		}
		return nil, JudgeUnparseable
	}
	var verdict struct {
		Useful []int `json:"useful"`
	}
	if err := json.Unmarshal([]byte(envelope.Response), &verdict); err != nil {
		return nil, JudgeUnparseable
	}

	wanted := make(map[int]bool, len(verdict.Useful))
	for _, n := range verdict.Useful {
		wanted[n] = true
	}
	kept := make([]Match, 0, len(matches))
	for i, m := range matches {
		if wanted[i+1] {
			kept = append(kept, m)
		}
	}
	return kept, JudgeRuled
}

// judgeGist is what the judge reads to decide. The summary is the note's own one-line statement
// of what it holds; without it the model would be ruling on a slug, which is exactly the thin
// evidence that made the score guess in the first place.
func judgeGist(entry IndexEntry) string {
	summary := strings.TrimSpace(entry.Summary)
	if summary == "" {
		summary = "(no summary in frontmatter)"
	}
	if len([]rune(summary)) > 220 {
		summary = string([]rune(summary)[:220]) + "…"
	}
	if len(entry.Tags) > 0 {
		summary += "  [tags: " + strings.Join(entry.Tags, ", ") + "]"
	}
	return summary
}

// appendJudgeLog records what the judge did, in its OWN lane beside fires_<session>.jsonl.
//
// A separate sidecar rather than a field on the fire log, deliberately: fires_*.jsonl means
// "what the agent was shown", and that meaning is what recall_feedback.py credits a
// [RECALL-USED] marker against. Folding silenced notes into it would quietly redefine the
// ledger under its own reader. Here the dropped ones are recorded with their scores, so the
// next precision/recall curve can be built on the judge's decisions instead of on the floor's —
// which is the whole reason the first curve was possible at all.
//
// Schema per line:
//
//	{ "ts": ..., "name": ..., "fused": <float>, "verdict": "kept"|"dropped"|"unjudged" }
//
// Errors are silent: sidecar logging must never affect the recall pipeline.
func appendJudgeLog(sessionID string, candidates, kept []Match, judged bool) {
	if sessionID == "" || len(candidates) == 0 {
		return
	}
	f, err := os.OpenFile(judgeLogPath(sessionID), os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0644)
	if err != nil {
		return
	}
	defer f.Close()

	survived := make(map[string]bool, len(kept))
	for _, m := range kept {
		survived[m.Entry.Name] = true
	}
	ts := time.Now().UTC().Format(time.RFC3339)
	for _, m := range candidates {
		verdict := "unjudged"
		if judged {
			verdict = "dropped"
			if survived[m.Entry.Name] {
				verdict = "kept"
			}
		}
		record := struct {
			Ts      string  `json:"ts"`
			Name    string  `json:"name"`
			Fused   float64 `json:"fused"`
			Verdict string  `json:"verdict"`
		}{ts, m.Entry.Name, m.FusedScore, verdict}
		data, err := json.Marshal(record)
		if err != nil {
			continue
		}
		f.Write(data)
		f.WriteString("\n")
	}
}

func judgeLogPath(sessionID string) string {
	return filepath.Join(baseDir, "judged_"+sessionID+".jsonl")
}
