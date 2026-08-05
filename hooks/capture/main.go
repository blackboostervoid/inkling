// @module: capture_scanner
// @role: UserPromptSubmit hook — a "should this be recorded?" rail, peer to recall_scanner but pointed at the prompt's SHAPE, not the corpus. Regex-free Unicode phrase match over the prompt; injects a [CAPTURE] hint suggesting where a durable note belongs. NEVER writes a note.
// @deps: (stdlib only — zero third-party; built per-machine by build_hooks.sh)
// @used-by: UserPromptSubmit hook in settings.json
// @doc: harvested from obsidian-mind classify-message; the capture twin of recall (retrieval). Signals live in capture_signals.json (runtime-editable, no recompile).
//
// Design stance (mirrors recall/procedural):
//   - Hint-only. It suggests capturing; it never auto-writes. A false positive costs one ignored line.
//   - Once-per-session-per-category dedup (cooldown file) kills nag fatigue — the failure mode that
//     makes an always-on suggestion get tuned out.
//   - Isolated binary. A capture bug can never break the recall retrieval hot path (separate process).
//   - Best-effort + always exit 0. A hook must never block prompt submission.
package main

import (
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"
)

// ========================================================================================
// MODELS
// ========================================================================================

type HookInput struct {
	SessionID string `json:"session_id"`
	Prompt    string `json:"prompt"`
	Cwd       string `json:"cwd"`
}

type HookOutput struct {
	HookSpecificOutput *HookSpecific `json:"hookSpecificOutput,omitempty"`
}

type HookSpecific struct {
	HookEventName     string `json:"hookEventName"`
	AdditionalContext string `json:"additionalContext,omitempty"`
}

type Category struct {
	ID       string   `json:"id"`
	Hint     string   `json:"hint"`
	Patterns []string `json:"patterns"`
}

type SignalConfig struct {
	Categories []Category `json:"categories"`
}

const (
	cooldownRetentionDays = 7.0
	captureHeader         = "[CAPTURE] The user's message looks capture-worthy. These are SUGGESTIONS — " +
		"act ONLY if the content is genuinely durable, and never auto-write a note without the user. " +
		"If a suggestion does not fit, ignore it silently (do not mention it):"
)

// ========================================================================================
// MATCHING — Unicode word-boundary phrase match (Go RE2 has no lookbehind; \b is ASCII-only,
// so a hand-rolled boundary check is what makes Cyrillic phrases fire correctly).
// ========================================================================================

// containsSignal reports whether phrase occurs in text flanked by non-letters on both sides.
// text and phrase are both already lowercased by the caller.
func containsSignal(text, phrase string) bool {
	if phrase == "" {
		return false
	}
	from := 0
	for from <= len(text)-len(phrase) {
		i := strings.Index(text[from:], phrase)
		if i < 0 {
			return false
		}
		start := from + i
		end := start + len(phrase)
		if boundaryOK(text, start, end) {
			return true
		}
		from = start + 1
	}
	return false
}

// boundaryOK is true when the rune immediately before start and immediately after end are not
// letters (or are string edges) — the Unicode equivalent of obsidian-mind's (?<![a-zA-Z]) guards,
// generalized so it works for Russian, not just Latin.
func boundaryOK(text string, start, end int) bool {
	if start > 0 {
		r, _ := utf8.DecodeLastRuneInString(text[:start])
		if unicode.IsLetter(r) {
			return false
		}
	}
	if end < len(text) {
		r, _ := utf8.DecodeRuneInString(text[end:])
		if unicode.IsLetter(r) {
			return false
		}
	}
	return true
}

// ========================================================================================
// SELF-REFERENCE FILTER — a marker word NAMED is not a marker word USED
// ========================================================================================
//
// Talking about this mechanism used to fire it: a signals table listing `PATTERN: "always",
// "every time"` matches on `always` exactly as a real rule would, because nothing here reads
// meaning. So instead of judging intent, drop the line SHAPES that only ever occur in a spec —
// fenced code, table rows, headings, blockquotes, `CATEGORY:` labels, trigger-phrase lists.
// The python twin (capture_inbox.py) applies the same four shapes plus a minimum length; length
// is deliberately omitted here, because this hook classifies a line rather than staging it as a
// quote, and a short real signal ("dead end") must still fire.

var (
	fenceRe      = regexp.MustCompile("^\\s*(?:```|~~~)")
	specLabelRe  = regexp.MustCompile(`(^|[\s*|(])(DECISION|GOTCHA|PATTERN|REJECTED|CAPTURE|SIGNAL)S?\s*[:|]`)
	quotedItemRe = regexp.MustCompile(`"[^"]{2,40}"`)
)

func isSpecNoise(line string) bool {
	trimmed := strings.TrimSpace(line)
	if strings.HasPrefix(trimmed, "|") || strings.HasPrefix(trimmed, "#") || strings.HasPrefix(trimmed, ">") {
		return true
	}
	if strings.Count(line, "|") >= 2 {
		return true
	}
	if specLabelRe.MatchString(line) {
		return true
	}
	return len(quotedItemRe.FindAllString(line, -1)) >= 3
}

// proseText keeps only the lines where the user is speaking, dropping quoted or tabular material.
func proseText(prompt string) string {
	var kept []string
	inFence := false
	for _, line := range strings.Split(prompt, "\n") {
		if fenceRe.MatchString(line) {
			inFence = !inFence
			continue
		}
		if inFence || isSpecNoise(line) {
			continue
		}
		kept = append(kept, line)
	}
	return strings.Join(kept, "\n")
}

// classify returns the ids of every category whose any phrase fires in the prompt's prose.
func classify(prompt string, cfg SignalConfig) []string {
	lowered := strings.ToLower(proseText(prompt))
	var fired []string
	for _, cat := range cfg.Categories {
		for _, p := range cat.Patterns {
			if containsSignal(lowered, strings.ToLower(p)) {
				fired = append(fired, cat.ID)
				break
			}
		}
	}
	return fired
}

// ========================================================================================
// CONFIG + COOLDOWN
// ========================================================================================

func baseDir() string {
	if exe, err := os.Executable(); err == nil {
		return filepath.Dir(exe)
	}
	if cfg := os.Getenv("CLAUDE_CONFIG_DIR"); cfg != "" {
		return filepath.Join(cfg, "hooks", "capture")
	}
	return "."
}

func loadConfig(dir string) (SignalConfig, error) {
	var cfg SignalConfig
	data, err := os.ReadFile(filepath.Join(dir, "capture_signals.json"))
	if err != nil {
		return cfg, err
	}
	err = json.Unmarshal(data, &cfg)
	return cfg, err
}

func cooldownPath(dir, sessionID string) string {
	return filepath.Join(dir, "capture_cd_"+sessionID+".json")
}

// loadFired returns the set of category ids already emitted this session.
func loadFired(path string) map[string]bool {
	fired := map[string]bool{}
	data, err := os.ReadFile(path)
	if err != nil {
		return fired
	}
	var ids []string
	if json.Unmarshal(data, &ids) == nil {
		for _, id := range ids {
			fired[id] = true
		}
	}
	return fired
}

func saveFired(path string, fired map[string]bool) {
	ids := make([]string, 0, len(fired))
	for id := range fired {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	if data, err := json.Marshal(ids); err == nil {
		os.WriteFile(path, data, 0644)
	}
}

// vacuumOldCooldowns deletes capture_cd_*.json files older than the retention window (bounds
// growth). Best-effort; the current session's file is kept regardless of age.
func vacuumOldCooldowns(dir, currentPath string) {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return
	}
	now := time.Now()
	for _, e := range entries {
		name := e.Name()
		if !strings.HasPrefix(name, "capture_cd_") || !strings.HasSuffix(name, ".json") {
			continue
		}
		full := filepath.Join(dir, name)
		if full == currentPath {
			continue
		}
		info, err := e.Info()
		if err != nil {
			continue
		}
		if now.Sub(info.ModTime()).Hours()/24.0 > cooldownRetentionDays {
			os.Remove(full)
		}
	}
}

// ========================================================================================
// MAIN
// ========================================================================================

func emit(ctx string) {
	out := HookOutput{HookSpecificOutput: &HookSpecific{HookEventName: "UserPromptSubmit", AdditionalContext: ctx}}
	b, _ := json.Marshal(out)
	fmt.Print(string(b))
}

func noop() { fmt.Print("{}") }

func main() {
	inputBytes, _ := io.ReadAll(os.Stdin)
	var input HookInput
	if json.Unmarshal(inputBytes, &input) != nil || input.SessionID == "" || strings.TrimSpace(input.Prompt) == "" {
		noop()
		return
	}

	dir := baseDir()
	cfg, err := loadConfig(dir)
	if err != nil || len(cfg.Categories) == 0 {
		noop()
		return
	}

	fired := classify(input.Prompt, cfg)
	if len(fired) == 0 {
		noop()
		return
	}

	// Dedup once-per-category-per-session.
	cdPath := cooldownPath(dir, input.SessionID)
	already := loadFired(cdPath)
	var fresh []string
	for _, id := range fired {
		if !already[id] {
			fresh = append(fresh, id)
			already[id] = true
		}
	}
	if len(fresh) == 0 {
		noop()
		return
	}
	saveFired(cdPath, already)
	vacuumOldCooldowns(dir, cdPath)

	// Build hint block in config order (stable, not match order).
	hintByID := map[string]string{}
	for _, c := range cfg.Categories {
		hintByID[c.ID] = c.Hint
	}
	freshSet := map[string]bool{}
	for _, id := range fresh {
		freshSet[id] = true
	}
	var lines []string
	for _, c := range cfg.Categories {
		if freshSet[c.ID] {
			lines = append(lines, "- "+c.Hint)
		}
	}
	emit(captureHeader + "\n" + strings.Join(lines, "\n"))
}
