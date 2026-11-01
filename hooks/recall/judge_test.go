package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func mkMatch(name, summary string, fused float64, tags ...string) Match {
	return Match{
		Entry:      IndexEntry{Name: name, Summary: summary, Tags: tags},
		FusedScore: fused,
	}
}

// withJudgeServer points judgeURL at a stub for the duration of the test.
func withJudgeServer(t *testing.T, handler http.HandlerFunc) {
	t.Helper()
	server := httptest.NewServer(handler)
	previous := judgeURL
	judgeURL = server.URL
	t.Cleanup(func() {
		judgeURL = previous
		server.Close()
	})
}

// respondUseful is an ollama stub returning the given 1-based indices as the verdict.
func respondUseful(indices ...int) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		body, _ := json.Marshal(map[string]any{"useful": indices})
		json.NewEncoder(w).Encode(map[string]string{"response": string(body)})
	}
}

func TestJudgeMatches_KeepsOnlyTheChosen(t *testing.T) {
	withJudgeServer(t, respondUseful(2))
	matches := []Match{mkMatch("a", "sa", 0.02), mkMatch("b", "sb", 0.02), mkMatch("c", "sc", 0.02)}

	kept, outcome := judgeMatches("a real question about the wiki rail", matches)

	if outcome != JudgeRuled {
		t.Fatal("outcome != JudgeRuled, but the stub answered")
	}
	if len(kept) != 1 || kept[0].Entry.Name != "b" {
		t.Fatalf("expected only b, got %v", kept)
	}
}

// The whole point of the stage: an empty verdict is a REAL answer meaning "none of these",
// and it must be distinguishable from "no judgement could be made".
func TestJudgeMatches_EmptyVerdictIsJudged(t *testing.T) {
	withJudgeServer(t, respondUseful())

	kept, outcome := judgeMatches("best borscht recipe", []Match{mkMatch("x", "sx", 0.02)})

	if outcome != JudgeRuled {
		t.Fatal("an empty keep must still count as JudgeRuled - otherwise silence is unreachable")
	}
	if len(kept) != 0 {
		t.Fatalf("expected zero kept, got %d", len(kept))
	}
}

// The failure that this whole three-state design exists to prevent: a judgement that could not
// be made must never arrive looking like a judgement that returned nothing.
func TestJudgeMatches_UnreachableIsUnjudgedNotEmpty(t *testing.T) {
	previous := judgeURL
	judgeURL = "http://127.0.0.1:1" // nothing listens here
	t.Cleanup(func() { judgeURL = previous })

	kept, outcome := judgeMatches("a question", []Match{mkMatch("x", "sx", 0.02)})

	if outcome == JudgeRuled {
		t.Fatal("an unreachable judge reported JudgeRuled - silence would be faked")
	}
	if kept != nil {
		t.Fatalf("expected nil keep on an unmade judgement, got %v", kept)
	}
}

func TestJudgeMatches_UnparseableReplyIsUnjudged(t *testing.T) {
	withJudgeServer(t, func(w http.ResponseWriter, r *http.Request) {
		json.NewEncoder(w).Encode(map[string]string{"response": "I think notes 1 and 2 look good!"})
	})

	_, outcome := judgeMatches("a question", []Match{mkMatch("x", "sx", 0.02)})

	if outcome == JudgeRuled {
		t.Fatal("prose instead of JSON must read as unjudged, not as a verdict")
	}
}

func TestJudgeMatches_HTTPErrorIsUnjudged(t *testing.T) {
	withJudgeServer(t, func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	})

	_, outcome := judgeMatches("a question", []Match{mkMatch("x", "sx", 0.02)})

	if outcome == JudgeRuled {
		t.Fatal("a 500 from ollama must read as unjudged")
	}
}

func TestJudgeMatches_KillSwitchSkipsTheCall(t *testing.T) {
	withJudgeServer(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatal("the judge was contacted despite RECALL_JUDGE=0")
	})
	t.Setenv(judgeEnvVar, "0")

	_, outcome := judgeMatches("a question", []Match{mkMatch("x", "sx", 0.02)})

	if outcome == JudgeRuled {
		t.Fatal("disabled judge reported a verdict")
	}
}

// No matches means the rail was going to be silent anyway, so the model is never contacted.
// This is the cost guard that replaced the prompt-length one.
func TestJudgeMatches_NoMatchesNeverContactsTheModel(t *testing.T) {
	withJudgeServer(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatal("the judge was contacted with zero candidates")
	})

	if _, outcome := judgeMatches("a question", nil); outcome == JudgeRuled {
		t.Fatal("expected unjudged on an empty candidate list")
	}
}

// The removed length guard, pinned as a regression: "лучший рецепт борща" is 19 runes and is
// the exact false positive the stage exists to kill. A short prompt must still be judged.
func TestJudgeMatches_ShortPromptIsStillJudged(t *testing.T) {
	withJudgeServer(t, respondUseful())

	_, outcome := judgeMatches("лучший рецепт борща", []Match{mkMatch("openpyxl", "xlsx recipe", 0.016)})

	if outcome != JudgeRuled {
		t.Fatal("a short prompt was skipped - that is the noisiest turn there is, not a cheap one")
	}
}

func TestJudgeGist_CarriesSummaryAndTags(t *testing.T) {
	gist := judgeGist(IndexEntry{Name: "n", Summary: "what the note holds", Tags: []string{"go", "hooks"}})

	if !strings.Contains(gist, "what the note holds") {
		t.Fatalf("summary missing from gist: %q", gist)
	}
	if !strings.Contains(gist, "go, hooks") {
		t.Fatalf("tags missing from gist: %q", gist)
	}
}

func TestJudgeGist_SaysSoWhenSummaryIsMissing(t *testing.T) {
	gist := judgeGist(IndexEntry{Name: "n"})

	if !strings.Contains(gist, "no summary") {
		t.Fatalf("a summary-less note must say so rather than present a bare slug: %q", gist)
	}
}

func TestJudgeGist_TruncatesRunesNotBytes(t *testing.T) {
	// A Cyrillic summary is 2 bytes per rune; slicing bytes would split one and corrupt it.
	gist := judgeGist(IndexEntry{Name: "n", Summary: strings.Repeat("я", 400)})

	if !strings.HasSuffix(gist, "…") {
		t.Fatalf("expected truncation marker, got %q", gist[len(gist)-8:])
	}
	if !strings.Contains(gist, strings.Repeat("я", 220)) {
		t.Fatal("truncation corrupted the multi-byte runes")
	}
}

func TestAppendJudgeLog_RecordsBothVerdicts(t *testing.T) {
	previous := baseDir
	baseDir = t.TempDir()
	t.Cleanup(func() { baseDir = previous })

	candidates := []Match{mkMatch("kept-one", "s", 0.049), mkMatch("dropped-one", "s", 0.049)}
	appendJudgeLog("sess", candidates, candidates[:1], true)

	lines := readJudgeLog(t, "sess")
	if len(lines) != 2 {
		t.Fatalf("expected 2 records, got %d", len(lines))
	}
	if lines["kept-one"] != "kept" || lines["dropped-one"] != "dropped" {
		t.Fatalf("wrong verdicts: %v", lines)
	}
}

func TestAppendJudgeLog_MarksUnjudgedRatherThanKept(t *testing.T) {
	previous := baseDir
	baseDir = t.TempDir()
	t.Cleanup(func() { baseDir = previous })

	candidates := []Match{mkMatch("a", "s", 0.02)}
	// Caller falls back to showing everything, but nothing was judged - the ledger must say so.
	appendJudgeLog("sess", candidates, candidates, false)

	if v := readJudgeLog(t, "sess")["a"]; v != "unjudged" {
		t.Fatalf("expected unjudged, got %q", v)
	}
}

func TestAppendJudgeLog_SilentWithoutSession(t *testing.T) {
	previous := baseDir
	baseDir = t.TempDir()
	t.Cleanup(func() { baseDir = previous })

	appendJudgeLog("", []Match{mkMatch("a", "s", 0.02)}, nil, true)

	if entries, _ := filepath.Glob(filepath.Join(baseDir, "judged_*.jsonl")); len(entries) != 0 {
		t.Fatalf("wrote a sidecar without a session id: %v", entries)
	}
}

func readJudgeLog(t *testing.T, session string) map[string]string {
	t.Helper()
	data, err := os.ReadFile(judgeLogPath(session))
	if err != nil {
		t.Fatalf("reading judge log: %v", err)
	}
	verdicts := map[string]string{}
	for _, line := range strings.Split(strings.TrimSpace(string(data)), "\n") {
		var record struct {
			Name    string `json:"name"`
			Verdict string `json:"verdict"`
		}
		if err := json.Unmarshal([]byte(line), &record); err != nil {
			t.Fatalf("bad JSONL line %q: %v", line, err)
		}
		verdicts[record.Name] = record.Verdict
	}
	return verdicts
}

func TestJudgeMatches_AsksForARealSchema(t *testing.T) {
	var sent map[string]any
	withJudgeServer(t, func(w http.ResponseWriter, r *http.Request) {
		json.NewDecoder(r.Body).Decode(&sent)
		respondUseful()(w, r)
	})

	judgeMatches("a real question about the wiki rail", []Match{mkMatch("x", "sx", 0.02)})

	schema, ok := sent["format"].(map[string]any)
	if !ok || schema["type"] != "object" {
		t.Fatalf("format must be a JSON Schema so a bad key cannot be generated, got %v", sent["format"])
	}
}

func TestJudgeMatches_SendsOnlyTheHeadOfALongPrompt(t *testing.T) {
	var sent string
	withJudgeServer(t, func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			Prompt string `json:"prompt"`
		}
		json.NewDecoder(r.Body).Decode(&body)
		sent = body.Prompt
		respondUseful()(w, r)
	})
	head := strings.Repeat("ж", judgeQueryRunes)

	_, outcome := judgeMatches(head+"TAIL", []Match{mkMatch("x", "sx", 0.02)})

	if outcome != JudgeRuled || !strings.Contains(sent, head) || strings.Contains(sent, "TAIL") {
		t.Fatalf("the judge must read the first %d runes and nothing past them", judgeQueryRunes)
	}
}

func TestJudgeModelFromEnv_DefaultUnlessNamed(t *testing.T) {
	t.Setenv(judgeModelEnvVar, "")
	if got := judgeModelFromEnv(); got != judgeModelDefault {
		t.Errorf("unset: got %q, want the default %q", got, judgeModelDefault)
	}
	t.Setenv(judgeModelEnvVar, " my-model ")
	if got := judgeModelFromEnv(); got != "my-model" {
		t.Errorf("named: got %q", got)
	}
}

func TestIsMachinePrompt_ABackgroundTaskNoticeIsNotAMessage(t *testing.T) {
	prompts := map[string]bool{
		"<task-notification>\n<task-id>b1</task-id>":      true,
		"  [SYSTEM NOTIFICATION - NOT USER INPUT]\nevent": true,
		"<ci-monitor-event>checks failed":                 true,
		"why does the judge hold the video memory":        false,
		"the log had a line <task-notification>, look":    false,
	}
	for prompt, want := range prompts {
		if got := isMachinePrompt(HookInput{Prompt: prompt}); got != want {
			t.Errorf("isMachinePrompt(%q) = %v, want %v", prompt, got, want)
		}
	}
	if !isMachinePrompt(HookInput{Prompt: "go on", Source: "system"}) {
		t.Error("a prompt the client marks source=system is not the user's message")
	}
	if isMachinePrompt(HookInput{Prompt: "go on", Source: "user"}) {
		t.Error("a prompt the user typed must reach the rail")
	}
}
