package main

import (
	"bufio"
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

// ========================================================================================
// tokenize
// ========================================================================================

func TestTokenize_BasicSplit(t *testing.T) {
	got := tokenize("hello world")
	want := []string{"hello", "world"}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("got %v, want %v", got, want)
	}
}

func TestTokenize_Lowercase(t *testing.T) {
	got := tokenize("HELLO World")
	want := []string{"hello", "world"}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("got %v, want %v", got, want)
	}
}

func TestTokenize_Russian(t *testing.T) {
	got := tokenize("привет Мир")
	want := []string{"привет", "мир"}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("got %v, want %v", got, want)
	}
}

func TestTokenize_MixedRussianEnglish(t *testing.T) {
	// "и" is below minTokenLen=2, must drop.
	got := tokenize("debug агента и orchestrator")
	want := []string{"debug", "агента", "orchestrator"}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("got %v, want %v", got, want)
	}
}

func TestTokenize_WordBoundaryFixesV1SubstringGap(t *testing.T) {
	// v1's substring tag-match would surface a tag `go` from inside `together`.
	// v2's tokenizer must produce `together` and `go` as distinct tokens —
	// no fragmenting, no leakage.
	got := tokenize("together they go")
	want := []string{"together", "they", "go"}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("got %v, want %v", got, want)
	}
}

func TestTokenize_DropsSubMinLenTokens(t *testing.T) {
	// "a" is 1 char → dropped. Everything else 2+ chars kept.
	got := tokenize("a is to go in js")
	want := []string{"is", "to", "go", "in", "js"}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("got %v, want %v", got, want)
	}
}

func TestTokenize_Punctuation(t *testing.T) {
	// Apostrophe inside "it's" splits to "it" + "s" — "s" drops as sub-min.
	got := tokenize("hello, world! it's done.")
	want := []string{"hello", "world", "it", "done"}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("got %v, want %v", got, want)
	}
}

func TestTokenize_Empty(t *testing.T) {
	got := tokenize("")
	if len(got) != 0 {
		t.Errorf("expected empty, got %v", got)
	}
}

func TestTokenize_OnlyPunctuation(t *testing.T) {
	got := tokenize("!@#$%^&*()")
	if len(got) != 0 {
		t.Errorf("expected empty, got %v", got)
	}
}

// ========================================================================================
// BM25
// ========================================================================================

func TestBM25_RareTermBeatsCommonTerm(t *testing.T) {
	// IDF weighting: a term that appears in 1/5 docs should outscore one in 4/5.
	entries := []IndexEntry{
		{Name: "a", Summary: "rare common common"},
		{Name: "b", Summary: "frequent common common"},
		{Name: "c", Summary: "frequent common common"},
		{Name: "d", Summary: "frequent common common"},
		{Name: "e", Summary: "frequent common common"},
	}
	corpus := buildBM25Corpus(entries, false)
	scoreRare := bm25Score([]string{"rare"}, corpus.Docs[0], corpus)
	scoreFrequent := bm25Score([]string{"frequent"}, corpus.Docs[1], corpus)
	if scoreRare <= scoreFrequent {
		t.Errorf("rare term (%f) must score higher than frequent (%f)", scoreRare, scoreFrequent)
	}
}

func TestBM25_TermFrequencyMatters(t *testing.T) {
	// Same term, different TF — higher TF should score higher (with saturation).
	entries := []IndexEntry{
		{Name: "a", Summary: "alpha beta gamma"},
		{Name: "b", Summary: "alpha alpha alpha beta"},
	}
	corpus := buildBM25Corpus(entries, false)
	low := bm25Score([]string{"alpha"}, corpus.Docs[0], corpus)
	high := bm25Score([]string{"alpha"}, corpus.Docs[1], corpus)
	if high <= low {
		t.Errorf("doc with alpha x3 (%f) should outscore doc with alpha x1 (%f)", high, low)
	}
}

func TestBM25_AbsentTermZero(t *testing.T) {
	entries := []IndexEntry{
		{Name: "a", Summary: "alpha beta"},
	}
	corpus := buildBM25Corpus(entries, false)
	score := bm25Score([]string{"missing"}, corpus.Docs[0], corpus)
	if score != 0 {
		t.Errorf("absent term must score 0, got %f", score)
	}
}

func TestBM25_EmptyCorpus(t *testing.T) {
	corpus := buildBM25Corpus(nil, false)
	if corpus.DocCount != 0 || corpus.AvgDocLen != 0 {
		t.Errorf("empty corpus should yield zero stats, got %+v", corpus)
	}
}

// ========================================================================================
// ranking
// ========================================================================================

func TestRankByDescInt_Basic(t *testing.T) {
	// scores = [3,1,2,0,5] → sorted desc indices = [4,0,2,1,3]
	// idx=4 → rank 1; idx=0 → rank 2; idx=2 → rank 3; idx=1 → rank 4; idx=3 (zero) → rank n+1=6
	scores := []int{3, 1, 2, 0, 5}
	want := []int{2, 4, 3, 6, 1}
	got := rankByDescInt(scores)
	if !reflect.DeepEqual(got, want) {
		t.Errorf("got %v, want %v", got, want)
	}
}

func TestRankByDescInt_ZerosPushedToSentinel(t *testing.T) {
	scores := []int{0, 0, 5, 0}
	want := []int{5, 5, 1, 5}
	got := rankByDescInt(scores)
	if !reflect.DeepEqual(got, want) {
		t.Errorf("got %v, want %v", got, want)
	}
}

func TestRankByDescFloat_Basic(t *testing.T) {
	scores := []float64{0.3, 0.1, 0.5, 0.0}
	want := []int{2, 3, 1, 5}
	got := rankByDescFloat(scores)
	if !reflect.DeepEqual(got, want) {
		t.Errorf("got %v, want %v", got, want)
	}
}

// ========================================================================================
// findMatches — integration
// ========================================================================================

func makeWiki() []IndexEntry {
	return []IndexEntry{
		{
			Name:    "prompt-engineering",
			Path:    "patterns/prompt-engineering.md",
			Tags:    []string{"few-shot", "промпт", "5c-framework", "decision-matrix"},
			Summary: "Compilation of 2025-2026 techniques: 5C framework, few-shot 3-5 examples, decision matrix, self-verification.",
		},
		{
			Name:    "orchestrator-pattern",
			Path:    "patterns/orchestrator-pattern.md",
			Tags:    []string{"orchestrator", "architecture", "main", "suborc"},
			Summary: "Main orchestrator routes between sub-orchestrators; modules never talk cross-branch.",
		},
		{
			Name:    "silent-failure-patterns",
			Path:    "patterns/silent-failure-patterns.md",
			Tags:    []string{"debugging", "errors", "silent", "swallowed-exceptions"},
			Summary: "How silent error handling hides bugs: swallowed exceptions, missing context, dangerous fallbacks.",
		},
		{
			Name:    "voyage-code-3",
			Path:    "apis/voyage-code-3.md",
			Tags:    []string{"voyage", "embeddings", "code-3", "semantic-search"},
			Summary: "Voyage Code-3 1024-dim embedding API specifically tuned for code retrieval.",
		},
		{
			Name:    "claude-prompt-caching",
			Path:    "apis/claude-prompt-caching.md",
			Tags:    []string{"claude", "cache", "ttl", "ephemeral"},
			Summary: "Anthropic prompt caching with 5-minute TTL; cache_control on system or messages blocks.",
		},
	}
}

func TestFindMatches_TagAndBm25BothWin(t *testing.T) {
	// Prompt hits orchestrator-pattern on both rails — should be #1.
	matches := findMatches("orchestrator architecture suborc", makeWiki(), Lens{}, nil)
	if len(matches) == 0 {
		t.Fatalf("expected at least one match")
	}
	if matches[0].Entry.Name != "orchestrator-pattern" {
		t.Errorf("expected orchestrator-pattern as #1, got %s (score %f)",
			matches[0].Entry.Name, matches[0].FusedScore)
	}
}

func TestFindMatches_Bm25SingleRailWin_TheV2Improvement(t *testing.T) {
	// Prompt content overlaps with silent-failure-patterns summary text
	// ("swallowed exceptions hides bugs") but NO tag is a substring of the
	// prompt — tags are debugging/errors/silent/swallowed-exceptions, none
	// appear verbatim in "swallowed exceptions hide bugs in production".
	// v1 would gate this out; v2 should surface via BM25.
	matches := findMatches("swallowed exceptions hide bugs in production",
		makeWiki(), Lens{}, nil)

	found := false
	for _, m := range matches {
		if m.Entry.Name == "silent-failure-patterns" {
			found = true
			if m.Bm25Score == 0 {
				t.Errorf("expected non-zero BM25 score, got 0")
			}
		}
	}
	if !found {
		names := []string{}
		for _, m := range matches {
			names = append(names, m.Entry.Name)
		}
		t.Errorf("v2 must surface silent-failure-patterns via BM25 single-rail; matches=%v", names)
	}
}

func TestFindMatches_CooldownSkip(t *testing.T) {
	cooldown := []string{"orchestrator-pattern"}
	matches := findMatches("orchestrator architecture suborc", makeWiki(), Lens{}, cooldown)
	for _, m := range matches {
		if m.Entry.Name == "orchestrator-pattern" {
			t.Errorf("cooldown entry must be skipped, but got it in results")
		}
	}
}

func TestFindMatches_SuppressMutes(t *testing.T) {
	prompt := "orchestrator architecture"
	unsuppressed := findMatches(prompt, makeWiki(), Lens{}, nil)
	suppressed := findMatches(prompt, makeWiki(),
		Lens{Suppress: []string{"orchestrator-pattern"}}, nil)

	if len(unsuppressed) == 0 || unsuppressed[0].Entry.Name != "orchestrator-pattern" {
		t.Fatalf("setup: expected orchestrator-pattern as #1 unsuppressed, got %v", unsuppressed)
	}

	for _, m := range suppressed {
		if m.Entry.Name == "orchestrator-pattern" {
			if m.FusedScore >= unsuppressed[0].FusedScore {
				t.Errorf("suppress must lower score; unsuppressed=%f suppressed=%f",
					unsuppressed[0].FusedScore, m.FusedScore)
			}
		}
	}
}

func TestFindMatches_BoostLifts(t *testing.T) {
	prompt := "помоги с debugging"
	unboosted := findMatches(prompt, makeWiki(), Lens{}, nil)
	boosted := findMatches(prompt, makeWiki(),
		Lens{Boost: []string{"debugging"}}, nil)

	unboostedScore := 0.0
	boostedScore := 0.0
	for _, m := range unboosted {
		if m.Entry.Name == "silent-failure-patterns" {
			unboostedScore = m.FusedScore
		}
	}
	for _, m := range boosted {
		if m.Entry.Name == "silent-failure-patterns" {
			boostedScore = m.FusedScore
		}
	}
	if unboostedScore == 0 {
		t.Fatalf("setup: expected silent-failure-patterns to surface unboosted")
	}
	if boostedScore <= unboostedScore {
		t.Errorf("boost must lift score; unboosted=%f boosted=%f",
			unboostedScore, boostedScore)
	}
}

func TestFindMatches_EmptyIndex(t *testing.T) {
	matches := findMatches("any prompt", nil, Lens{}, nil)
	if matches != nil {
		t.Errorf("empty index should return nil, got %v", matches)
	}
}

func TestFindMatches_AllInCooldown(t *testing.T) {
	cooldown := []string{
		"prompt-engineering", "orchestrator-pattern", "silent-failure-patterns",
		"voyage-code-3", "claude-prompt-caching",
	}
	matches := findMatches("orchestrator", makeWiki(), Lens{}, cooldown)
	if matches != nil {
		t.Errorf("all-cooldown should return nil, got %v", matches)
	}
}

func TestFindMatches_RespectsCandidateCap(t *testing.T) {
	// Broad prompt that touches every entry.
	bigPrompt := "orchestrator debugging voyage claude prompt few-shot framework silent swallowed"
	matches := findMatches(bigPrompt, makeWiki(), Lens{}, nil)
	if len(matches) > maxCandidates {
		t.Errorf("expected ≤%d candidates, got %d", maxCandidates, len(matches))
	}
}

// The pool the judge considers and the output the prompt receives are two different widths.
// Getting this backwards is the failure that matters: an unjudged rail handing over the wide
// pool would ship ten unchecked notes on every prompt the moment ollama stops.
func TestDisplayLimit_NarrowsWhenUnjudged(t *testing.T) {
	if got := displayLimit(true); got != maxJudgedResults {
		t.Errorf("judged: expected %d, got %d", maxJudgedResults, got)
	}
	if got := displayLimit(false); got != maxUnjudgedResults {
		t.Errorf("unjudged: expected %d, got %d", maxUnjudgedResults, got)
	}
	if maxUnjudgedResults >= maxJudgedResults {
		t.Errorf("unjudged width (%d) must stay below judged width (%d)",
			maxUnjudgedResults, maxJudgedResults)
	}
	if maxCandidates <= maxJudgedResults {
		t.Errorf("candidate pool (%d) must exceed what is displayed (%d), or the judge has "+
			"nothing to choose from", maxCandidates, maxJudgedResults)
	}
}

// The evidence multiplier decides which notes the ledger promotes and which it buries, so the
// property that matters most is the one about notes with NO history: unproven must be neutral.
// Penalising absence of evidence would freeze the corpus — a new note would never be shown,
// never acquire history, and never escape.
func TestEvidenceMultiplier(t *testing.T) {
	sessions := func(n int) []string {
		out := make([]string, n)
		for i := range out {
			out[i] = "s"
		}
		return out
	}

	if got := evidenceMultiplier(LedgerEntry{}); got != 1.0 {
		t.Errorf("a note with no history must be neutral, got %v", got)
	}
	if got := evidenceMultiplier(LedgerEntry{FiredSessions: sessions(3)}); got < 0.9 {
		t.Errorf("3 fruitless firings is thin evidence and must barely move, got %v", got)
	}

	loud := evidenceMultiplier(LedgerEntry{FiredSessions: sessions(41)})
	mid := evidenceMultiplier(LedgerEntry{FiredSessions: sessions(15)})
	if !(loud < mid && mid < 1.0) {
		t.Errorf("damp must be monotonic in fruitless firings: 41 -> %v, 15 -> %v", loud, mid)
	}

	// A single use flips a note from damped to lifted, however loud it was: the damp is a
	// statement about NEVER landing, not about a poor ratio.
	lifted := evidenceMultiplier(LedgerEntry{
		FiredSessions: sessions(41), UsedSessions: sessions(1), Confidence: 0.2})
	if lifted <= 1.0 {
		t.Errorf("a note that has landed must never be damped, got %v", lifted)
	}

	// A note dead on the push door but alive through the MCP body search must NOT be damped —
	// this is the whole reason the two doors are counted separately.
	pullOnly := evidenceMultiplier(LedgerEntry{
		FiredSessions:       sessions(20),
		SmritiFiredSessions: sessions(4),
		SmritiUsedSessions:  sessions(3),
		SmritiConfidence:    0.75,
	})
	if pullOnly <= 1.0 {
		t.Errorf("alive through the second door must lift, not damp, got %v", pullOnly)
	}

	if got := evidenceMultiplier(LedgerEntry{
		UsedSessions: sessions(1), Confidence: 1.0}); got != 1.0+evidenceLiftGain {
		t.Errorf("full confidence must reach the lift ceiling, got %v", got)
	}
}

// Regression, and the bug was live: evidence must ORDER the pool, never GATE it.
//
// When the multiplier was applied before the floor, `yaml-reserved-words` stopped answering the
// question "in yaml `off` turns into false" — its own subject — because 41 fruitless firings
// pushed its score under the threshold while two lifted notes about nothing in particular took
// its place. A note's history had acquired a veto over its relevance.
func TestEvidence_RanksButDoesNotGate(t *testing.T) {
	wiki := makeWiki()
	prompt := "orchestrator architecture suborc"

	ledger = nil
	baseline := findMatches(prompt, wiki, Lens{}, nil)
	if len(baseline) == 0 {
		t.Fatal("fixture produced no matches; the rest of this test cannot mean anything")
	}
	top := baseline[0].Entry.Name

	// Damp the best match as hard as the formula ever damps anything.
	fired := make([]string, 60)
	for i := range fired {
		fired[i] = "s"
	}
	ledger = map[string]LedgerEntry{top: {FiredSessions: fired}}
	defer func() { ledger = nil }()

	after := findMatches(prompt, wiki, Lens{}, nil)
	found := false
	for _, m := range after {
		if m.Entry.Name == top {
			found = true
			if m.FusedScore != baseline[0].FusedScore {
				t.Errorf("the gate must see the UNWEIGHTED score: %v vs %v",
					m.FusedScore, baseline[0].FusedScore)
			}
			if m.RankScore >= m.FusedScore {
				t.Errorf("a damped note must rank below its own topical score: rank %v, fused %v",
					m.RankScore, m.FusedScore)
			}
		}
	}
	if !found {
		t.Errorf("%q was damped out of the pool entirely — evidence gated instead of ranking", top)
	}
}

func TestCapMatches_TrimsAndPreservesOrder(t *testing.T) {
	in := []Match{{FusedScore: 3}, {FusedScore: 2}, {FusedScore: 1}}
	out := capMatches(in, 2)
	if len(out) != 2 || out[0].FusedScore != 3 || out[1].FusedScore != 2 {
		t.Errorf("expected the top two in order, got %+v", out)
	}
	if got := capMatches(in, 10); len(got) != 3 {
		t.Errorf("a limit above the length must not trim: got %d", len(got))
	}
}

// ========================================================================================
// Bridge — semantic cluster expansion (v2.1)
// ========================================================================================

// makeBridgeWiki — small corpus where:
//   - entry A has tag "silent-failure" — NOT a substring of "debugging"
//   - entry B has tag "log-tricks"     — NOT a substring of "debugging"
//
// Both are reachable only through the bridge cluster.
func makeBridgeWiki() []IndexEntry {
	return []IndexEntry{
		{Name: "A-silent", Path: "x/a.md", Tags: []string{"silent-failure"}, Summary: "x"},
		{Name: "B-log", Path: "x/b.md", Tags: []string{"log-tricks"}, Summary: "y"},
		{Name: "C-unrelated", Path: "x/c.md", Tags: []string{"voyage"}, Summary: "z"},
	}
}

func TestFindMatches_BridgeTriggeredByKey(t *testing.T) {
	// Prompt mentions the KEY ("debugging"). Without bridge, neither A nor
	// B would surface — their tags don't appear in the prompt and no BM25
	// overlap. With the cluster fix, A and B both become reachable through
	// the virtual pool.
	lens := Lens{
		Bridge: map[string][]string{
			"debugging": {"silent-failure", "log-tricks"},
		},
	}
	withoutBridge := findMatches("помоги с debugging", makeBridgeWiki(), Lens{}, nil)
	withBridge := findMatches("помоги с debugging", makeBridgeWiki(), lens, nil)

	for _, m := range withoutBridge {
		if m.Entry.Name == "A-silent" || m.Entry.Name == "B-log" {
			t.Fatalf("setup broken: %s should NOT surface without bridge", m.Entry.Name)
		}
	}

	gotA := false
	gotB := false
	for _, m := range withBridge {
		if m.Entry.Name == "A-silent" {
			gotA = true
		}
		if m.Entry.Name == "B-log" {
			gotB = true
		}
	}
	if !gotA || !gotB {
		names := []string{}
		for _, m := range withBridge {
			names = append(names, m.Entry.Name)
		}
		t.Errorf("bridge-by-key must surface both A-silent and B-log; got %v", names)
	}
}

func TestFindMatches_BridgeTriggeredByValue(t *testing.T) {
	// Symmetry check: prompt mentions a VALUE (`silent-failure`), so the
	// cluster fires from the value side. All other cluster members —
	// including the KEY (`debugging`) and the sibling value (`log-tricks`)
	// — should become virtually present.
	lens := Lens{
		Bridge: map[string][]string{
			"debugging": {"silent-failure", "log-tricks"},
		},
	}
	matches := findMatches("обсудим silent-failure handling", makeBridgeWiki(), lens, nil)

	// B-log has tag "log-tricks" which is now virtually present via cluster.
	gotB := false
	for _, m := range matches {
		if m.Entry.Name == "B-log" {
			gotB = true
		}
	}
	if !gotB {
		t.Errorf("bridge-by-value must propagate to sibling cluster members; B-log missing")
	}
}

func TestFindMatches_BridgeQuietWhenIrrelevant(t *testing.T) {
	// Prompt unrelated to the cluster — bridge must stay silent.
	lens := Lens{
		Bridge: map[string][]string{
			"debugging": {"silent-failure", "log-tricks"},
		},
	}
	matches := findMatches("воркфлоу batch processing", makeBridgeWiki(), lens, nil)
	for _, m := range matches {
		if m.Entry.Name == "A-silent" || m.Entry.Name == "B-log" {
			t.Errorf("bridge fired on irrelevant prompt; surfaced %s", m.Entry.Name)
		}
	}
}

// ========================================================================================
// v2.5 — lens.min_fused_score override + per-fire sidecar log
// ========================================================================================

func TestFindMatches_LensMinFusedScoreOverrideRaisesGate(t *testing.T) {
	// Prompt that normally produces a passing match (dual-rail rank-1 ≈ 0.0328
	// from earlier calibration). Raising the threshold above that should
	// strangle the match entirely.
	high := 0.5
	lens := Lens{MinFusedScore: &high}

	baseline := findMatches("orchestrator architecture", makeWiki(), Lens{}, nil)
	gated := findMatches("orchestrator architecture", makeWiki(), lens, nil)

	if len(baseline) == 0 {
		t.Fatalf("setup: baseline should have matches with default threshold")
	}
	if len(gated) != 0 {
		t.Errorf("threshold 0.5 must strangle all matches; got %d", len(gated))
	}
}

func TestFindMatches_LensMinFusedScoreOverrideLowersGate(t *testing.T) {
	// Set a very loose threshold (0.001). Some entries that were rejected by
	// default 0.015 should now pass.
	loose := 0.001
	lensLoose := Lens{MinFusedScore: &loose}

	baseline := findMatches("voyage", makeWiki(), Lens{}, nil)
	relaxed := findMatches("voyage", makeWiki(), lensLoose, nil)

	if len(relaxed) < len(baseline) {
		t.Errorf("loose threshold must not produce fewer matches; baseline=%d relaxed=%d",
			len(baseline), len(relaxed))
	}
}

func TestMergeLens_MinFusedScoreProjectOverrides(t *testing.T) {
	userVal := 0.010
	projectVal := 0.025
	user := Lens{MinFusedScore: &userVal}
	project := Lens{MinFusedScore: &projectVal}
	merged := mergeLens(user, project)
	if merged.MinFusedScore == nil || *merged.MinFusedScore != projectVal {
		t.Errorf("project override should win; got %v", merged.MinFusedScore)
	}
}

func TestMergeLens_MinFusedScoreUserSurvivesNilProject(t *testing.T) {
	userVal := 0.010
	user := Lens{MinFusedScore: &userVal}
	merged := mergeLens(user, Lens{})
	if merged.MinFusedScore == nil || *merged.MinFusedScore != userVal {
		t.Errorf("user value should survive when project is nil; got %v", merged.MinFusedScore)
	}
}

func TestMergeLens_MinFusedScoreBothNilStaysNil(t *testing.T) {
	merged := mergeLens(Lens{}, Lens{})
	if merged.MinFusedScore != nil {
		t.Errorf("both nil should produce nil; got %v", merged.MinFusedScore)
	}
}

func TestAppendFireLog_WritesExpectedSchema(t *testing.T) {
	// Redirect baseDir to a temp location and verify the sidecar shape.
	tmpDir := t.TempDir()
	originalBaseDir := baseDir
	baseDir = tmpDir
	t.Cleanup(func() { baseDir = originalBaseDir })

	matches := []Match{
		{
			Entry:      IndexEntry{Name: "alpha", Path: "p/a.md"},
			FusedScore: 0.0254,
			TagRank:    1,
			Bm25Rank:   3,
		},
		{
			Entry:      IndexEntry{Name: "beta", Path: "p/b.md"},
			FusedScore: 0.0164,
			TagRank:    5,
			Bm25Rank:   6,
		},
	}
	appendFireLog("sid-x", matches)

	data, err := os.ReadFile(filepath.Join(tmpDir, "fires_sid-x.jsonl"))
	if err != nil {
		t.Fatalf("sidecar not written: %v", err)
	}

	var records []map[string]interface{}
	scanner := bufio.NewScanner(strings.NewReader(string(data)))
	for scanner.Scan() {
		var rec map[string]interface{}
		if err := json.Unmarshal(scanner.Bytes(), &rec); err != nil {
			t.Fatalf("bad JSONL line: %v", err)
		}
		records = append(records, rec)
	}
	if len(records) != 2 {
		t.Fatalf("expected 2 records, got %d", len(records))
	}
	for _, r := range records {
		for _, key := range []string{"ts", "name", "fused", "tag_rank", "bm25_rank"} {
			if _, ok := r[key]; !ok {
				t.Errorf("record missing key %q: %v", key, r)
			}
		}
	}
	if records[0]["name"] != "alpha" || records[1]["name"] != "beta" {
		t.Errorf("name order wrong: %v / %v", records[0]["name"], records[1]["name"])
	}
}

func TestAppendFireLog_EmptySessionIDNoOp(t *testing.T) {
	// No write should happen on empty session_id — keeps the sidecar absent
	// for one-off / smoke-test runs where there's no session to attribute to.
	tmpDir := t.TempDir()
	originalBaseDir := baseDir
	baseDir = tmpDir
	t.Cleanup(func() { baseDir = originalBaseDir })

	appendFireLog("", []Match{{Entry: IndexEntry{Name: "x"}}})

	entries, _ := os.ReadDir(tmpDir)
	for _, e := range entries {
		if strings.HasPrefix(e.Name(), "fires_") {
			t.Errorf("sidecar should not exist on empty session_id, found %s", e.Name())
		}
	}
}

// ========================================================================================
// v2.6 — project-type awareness via exclude_tags + exclude_paths
// ========================================================================================

func makeExcludeWiki() []IndexEntry {
	return []IndexEntry{
		{Name: "tts-api", Path: "apis/openai-tts.md",
			Tags:    []string{"openai", "tts", "audio"},
			Summary: "TTS API reference."},
		{Name: "dubbing-pattern", Path: "patterns/dubbing-russian.md",
			Tags:    []string{"dubbing", "русский", "локализация"},
			Summary: "Russian dubbing patterns."},
		{Name: "orchestrator-pattern", Path: "patterns/orchestrator-pattern.md",
			Tags:    []string{"orchestrator", "architecture"},
			Summary: "Orchestrator routing rules."},
	}
}

func TestFindMatches_ExcludeTagDropsEntry(t *testing.T) {
	// Without exclude: tts entry would surface on prompt mentioning "tts".
	// With exclude_tags=[tts]: tts entry is dropped before scoring.
	baseline := findMatches("tts api openai", makeExcludeWiki(), Lens{}, nil)
	gotInBaseline := false
	for _, m := range baseline {
		if m.Entry.Name == "tts-api" {
			gotInBaseline = true
		}
	}
	if !gotInBaseline {
		t.Fatalf("setup: tts-api expected in baseline matches; got %v", baseline)
	}

	lens := Lens{ExcludeTags: []string{"tts"}}
	excluded := findMatches("tts api openai", makeExcludeWiki(), lens, nil)
	for _, m := range excluded {
		if m.Entry.Name == "tts-api" {
			t.Errorf("tts-api must be excluded when 'tts' is in ExcludeTags")
		}
	}
}

func TestFindMatches_ExcludeTagCaseInsensitive(t *testing.T) {
	// Russian tag with mixed case in exclude rule.
	lens := Lens{ExcludeTags: []string{"РУССКИЙ"}}
	matches := findMatches("дубляж русский локализация", makeExcludeWiki(), lens, nil)
	for _, m := range matches {
		if m.Entry.Name == "dubbing-pattern" {
			t.Errorf("dubbing-pattern must be excluded via case-insensitive Russian tag match")
		}
	}
}

func TestFindMatches_ExcludePathGlob(t *testing.T) {
	// Path glob: apis/* drops anything under apis/.
	lens := Lens{ExcludePaths: []string{"apis/*"}}
	matches := findMatches("tts api openai", makeExcludeWiki(), lens, nil)
	for _, m := range matches {
		if strings.HasPrefix(m.Entry.Path, "apis/") {
			t.Errorf("path-glob 'apis/*' must drop %s", m.Entry.Path)
		}
	}
}

func TestFindMatches_ExcludePathGlobSubstring(t *testing.T) {
	// More specific glob: apis/*tts*.md
	lens := Lens{ExcludePaths: []string{"apis/*tts*.md"}}
	matches := findMatches("openai api", makeExcludeWiki(), lens, nil)
	for _, m := range matches {
		if m.Entry.Name == "tts-api" {
			t.Errorf("apis/*tts*.md must drop tts-api")
		}
	}
}

func TestFindMatches_NoExcludeRulesPreservesBehavior(t *testing.T) {
	// Empty exclude lists must be a perfect no-op.
	baseline := findMatches("orchestrator architecture", makeWiki(), Lens{}, nil)
	withEmpty := findMatches("orchestrator architecture", makeWiki(),
		Lens{ExcludeTags: []string{}, ExcludePaths: []string{}}, nil)

	if len(baseline) != len(withEmpty) {
		t.Errorf("empty exclude lists must not change match count; baseline=%d empty=%d",
			len(baseline), len(withEmpty))
	}
}

func TestMergeLens_ExcludesUnion(t *testing.T) {
	user := Lens{
		ExcludeTags:  []string{"python", "pipeline"},
		ExcludePaths: []string{"apis/*"},
	}
	project := Lens{
		ExcludeTags:  []string{"dubbing"},
		ExcludePaths: []string{"discoveries/*"},
	}
	merged := mergeLens(user, project)

	expectTags := map[string]bool{"python": true, "pipeline": true, "dubbing": true}
	if len(merged.ExcludeTags) != 3 {
		t.Errorf("expected 3 tag exclusions, got %d (%v)", len(merged.ExcludeTags), merged.ExcludeTags)
	}
	for _, t2 := range merged.ExcludeTags {
		if !expectTags[t2] {
			t.Errorf("unexpected tag in merged ExcludeTags: %s", t2)
		}
	}
	expectPaths := map[string]bool{"apis/*": true, "discoveries/*": true}
	if len(merged.ExcludePaths) != 2 {
		t.Errorf("expected 2 path exclusions, got %d (%v)", len(merged.ExcludePaths), merged.ExcludePaths)
	}
	for _, p := range merged.ExcludePaths {
		if !expectPaths[p] {
			t.Errorf("unexpected path in merged ExcludePaths: %s", p)
		}
	}
}

func TestApplyExcludes_NilOrEmptyShortCircuits(t *testing.T) {
	entries := makeExcludeWiki()
	// nil lists — return original slice
	out := applyExcludes(entries, nil, nil)
	if len(out) != len(entries) {
		t.Errorf("nil exclusions must return all entries; got %d/%d", len(out), len(entries))
	}
	// empty slices — same
	out = applyExcludes(entries, []string{}, []string{})
	if len(out) != len(entries) {
		t.Errorf("empty exclusions must return all entries; got %d/%d", len(out), len(entries))
	}
}

func TestFindMatches_NoSignalNoMatch(t *testing.T) {
	// Prompt with no overlap whatsoever — must return empty.
	matches := findMatches("xyzzyx plugh frobnicate", makeWiki(), Lens{}, nil)
	if matches != nil {
		t.Errorf("no-signal prompt must return nil, got %v", matches)
	}
}

// ========================================================================================
// v2.7 — per-workspace corpus namespace (wiki_index + corpora.json)
// ========================================================================================

func writeJSON(t *testing.T, path string, v interface{}) {
	t.Helper()
	data, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, data, 0644); err != nil {
		t.Fatal(err)
	}
}

func TestSanitizeNamespace_StripsUnsafe(t *testing.T) {
	cases := map[string]string{
		"personal":  "personal",
		"PERSONAL":  "personal",
		"perso nal": "personal",
		"../evil":   "evil",
		"a/b\\c":    "abc",
		"life_2":    "life_2",
		"domain-x":  "domain-x",
		"..":        "",
		"":          "",
	}
	for in, want := range cases {
		if got := sanitizeNamespace(in); got != want {
			t.Errorf("sanitizeNamespace(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestLoadIndex_NamespaceSelectsCorpus(t *testing.T) {
	tmpDir := t.TempDir()
	originalBaseDir := baseDir
	baseDir = tmpDir
	t.Cleanup(func() { baseDir = originalBaseDir })

	def := []IndexEntry{{Name: "code-note", Path: "x/code.md", Tags: []string{"go"}, Summary: "default corpus"}}
	personal := []IndexEntry{{Name: "sleep-note", Path: "health/sleep.md", Tags: []string{"сон"}, Summary: "personal corpus"}}
	writeJSON(t, filepath.Join(tmpDir, "index_compiled.json"), def)
	writeJSON(t, filepath.Join(tmpDir, "index_compiled_personal.json"), personal)

	gotDefault, err := loadIndex("")
	if err != nil {
		t.Fatalf("default corpus must load cleanly; got err %v", err)
	}
	if len(gotDefault) != 1 || gotDefault[0].Name != "code-note" {
		t.Errorf("empty namespace must load default corpus; got %v", gotDefault)
	}
	gotPersonal, err := loadIndex("personal")
	if err != nil {
		t.Fatalf("personal corpus must load cleanly; got err %v", err)
	}
	if len(gotPersonal) != 1 || gotPersonal[0].Name != "sleep-note" {
		t.Errorf("namespace 'personal' must load personal corpus; got %v", gotPersonal)
	}
	gotMissing, err := loadIndex("missing")
	if gotMissing != nil || err != nil {
		t.Errorf("unknown namespace = no corpus configured, a valid state: want (nil, nil); got (%v, %v)", gotMissing, err)
	}
}

// A corpus that exists but cannot be parsed must NOT read as an empty corpus. This is
// the whole point of the split: silence from a broken rail is indistinguishable from
// silence from an honest miss, so "broken" has to travel as an error.
func TestLoadIndexBrokenCorpusIsAnError(t *testing.T) {
	tmpDir := t.TempDir()
	originalBaseDir := baseDir
	baseDir = tmpDir
	t.Cleanup(func() { baseDir = originalBaseDir })

	// Valid file, invalid JSON — the exact shape a half-written or hand-edited
	// compiled corpus takes.
	if err := os.WriteFile(filepath.Join(tmpDir, "index_compiled_broken.json"),
		[]byte(`[{"name": "truncated", `), 0o644); err != nil {
		t.Fatalf("setup: %v", err)
	}

	got, err := loadIndex("broken")
	if err == nil {
		t.Fatal("malformed corpus must return an error, not a silent empty index")
	}
	if got != nil {
		t.Errorf("malformed corpus must yield no entries; got %v", got)
	}
	if !strings.Contains(err.Error(), "index_compiled_broken.json") {
		t.Errorf("error must name the offending file so the user can fix it; got %q", err)
	}
}

func TestLoadProceduralMissingIsSilentBrokenIsNot(t *testing.T) {
	tmpDir := t.TempDir()

	// No .claude/ at all — a project without recipes is a valid state.
	got, err := loadProcedural(tmpDir)
	if got != nil || err != nil {
		t.Errorf("absent procedural corpus is valid: want (nil, nil); got (%v, %v)", got, err)
	}

	// Present but corrupt — must surface.
	claudeDir := filepath.Join(tmpDir, ".claude")
	if err := os.MkdirAll(claudeDir, 0o755); err != nil {
		t.Fatalf("setup: %v", err)
	}
	if err := os.WriteFile(filepath.Join(claudeDir, "procedural_compiled.json"),
		[]byte(`not json at all`), 0o644); err != nil {
		t.Fatalf("setup: %v", err)
	}
	if _, err := loadProcedural(tmpDir); err == nil {
		t.Error("corrupt procedural corpus must return an error, not a silent nil")
	}
}

func TestMergeLens_WikiIndexProjectOverrides(t *testing.T) {
	merged := mergeLens(Lens{WikiIndex: "code"}, Lens{WikiIndex: "personal"})
	if merged.WikiIndex != "personal" {
		t.Errorf("project WikiIndex should win; got %q", merged.WikiIndex)
	}
}

func TestMergeLens_WikiIndexUserSurvivesEmptyProject(t *testing.T) {
	merged := mergeLens(Lens{WikiIndex: "personal"}, Lens{})
	if merged.WikiIndex != "personal" {
		t.Errorf("user WikiIndex should survive empty project; got %q", merged.WikiIndex)
	}
}

func TestMergeLens_WikiIndexBothEmpty(t *testing.T) {
	merged := mergeLens(Lens{}, Lens{})
	if merged.WikiIndex != "" {
		t.Errorf("both empty must stay empty; got %q", merged.WikiIndex)
	}
}

func TestCorporaWikiPath_FallsBackWhenAbsent(t *testing.T) {
	tmpDir := t.TempDir()
	originalBaseDir := baseDir
	baseDir = tmpDir
	t.Cleanup(func() { baseDir = originalBaseDir })

	if got := corporaWikiPath(""); got != wikiBasePath {
		t.Errorf("empty namespace must return default wikiBasePath; got %q", got)
	}
	if got := corporaWikiPath("personal"); got != wikiBasePath {
		t.Errorf("missing corpora.json must fall back to wikiBasePath; got %q", got)
	}
}

func TestCorporaWikiPath_ReadsRegistry(t *testing.T) {
	tmpDir := t.TempDir()
	originalBaseDir := baseDir
	baseDir = tmpDir
	t.Cleanup(func() { baseDir = originalBaseDir })

	reg := `{"personal": {"wiki_path": "/absolute/path/to/another/vault/personal"}}`
	if err := os.WriteFile(filepath.Join(tmpDir, "corpora.json"), []byte(reg), 0644); err != nil {
		t.Fatal(err)
	}
	got := corporaWikiPath("personal")
	want := "/absolute/path/to/another/vault/personal/" // trailing slash appended
	if got != want {
		t.Errorf("corporaWikiPath(personal) = %q, want %q", got, want)
	}
}

// ========================================================================================
// v2.8 — Russian stemming (per-corpus, gated by lens.Stem)
// ========================================================================================

func TestStemRu_FoldsInflections(t *testing.T) {
	pairs := [][2]string{
		{"пайка", "пайку"}, {"питание", "питания"}, {"дисциплина", "дисциплины"},
		{"творчество", "творчестве"}, {"мотивация", "мотивацией"}, {"тренировка", "тренировки"},
	}
	for _, p := range pairs {
		if stemRu(p[0]) != stemRu(p[1]) {
			t.Errorf("stemRu(%q)=%q != stemRu(%q)=%q", p[0], stemRu(p[0]), p[1], stemRu(p[1]))
		}
	}
	if stemRu("orchestrator") != "orchestrator" {
		t.Errorf("non-Cyrillic token must be untouched")
	}
	if stemRu("сон") != "сон" {
		t.Errorf("short word must be untouched")
	}
}

func TestAnalyzeTokens_StemPathDropsStopwordsAndStems(t *testing.T) {
	in := []string{"нет", "дисциплины", "как", "питание"}
	out := analyzeTokens(in, true)
	for _, tok := range out {
		if tok == "нет" || tok == "как" {
			t.Errorf("stopword %q must be dropped", tok)
		}
	}
	raw := analyzeTokens(in, false)
	if !reflect.DeepEqual(raw, in) {
		t.Errorf("stem=false must be a pass-through; got %v", raw)
	}
}

func TestFindMatches_StemSurfacesObliqueForm(t *testing.T) {
	wiki := []IndexEntry{
		{Name: "nutrition", Path: "h/n.md", Tags: []string{"питание"}, Summary: "здоровое питание и сбалансированный рацион"},
		{Name: "filler", Path: "h/f.md", Tags: []string{"воздух"}, Summary: "совсем иная тема без пересечений"},
	}
	prompt := "нужны советы про питания" // oblique "питания" + stopword "про"
	baseline := findMatches(prompt, wiki, Lens{}, nil)
	stemmed := findMatches(prompt, wiki, Lens{Stem: true}, nil)

	for _, m := range baseline {
		if m.Entry.Name == "nutrition" {
			t.Fatalf("setup: baseline must MISS oblique 'питания' vs tag 'питание'")
		}
	}
	found := false
	for _, m := range stemmed {
		if m.Entry.Name == "nutrition" {
			found = true
		}
	}
	if !found {
		t.Errorf("stemming must surface nutrition on oblique 'питания'")
	}
}
