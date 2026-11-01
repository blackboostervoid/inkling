package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"math"
	"os"
	"path"
	"path/filepath"
	"sort"
	"strings"
	"time"
	"unicode"

	"gopkg.in/yaml.v3"
)

// ========================================================================================
// MODELS
// ========================================================================================

type HookInput struct {
	Prompt         string `json:"prompt"`
	SessionID      string `json:"session_id"`
	TranscriptPath string `json:"transcript_path"`
	Cwd            string `json:"cwd"`
	Source         string `json:"source"`
}

var machinePromptMarkers = []string{
	"[SYSTEM NOTIFICATION - NOT USER INPUT]", "<task-notification>", "<ci-monitor-event>",
	"<cross-session-message", "[Cross-session delivery notice]", "[Cross-session idle notice]",
}

// A service notice is not the user's message: measured, 222 of 552 judged passes went on these.
func isMachinePrompt(input HookInput) bool {
	if input.Source != "" && input.Source != "user" {
		return true
	}
	head := strings.TrimSpace(input.Prompt)
	for _, marker := range machinePromptMarkers {
		if strings.HasPrefix(head, marker) {
			return true
		}
	}
	return false
}

type IndexEntry struct {
	Name    string   `json:"name"`
	Path    string   `json:"path"`
	Tags    []string `json:"tags"`
	Summary string   `json:"summary"`
	// Related carries the note's own outgoing links, straight from its frontmatter.
	// It is NOT ranked against - the retrieval document stays name+tags+summary - and
	// exists only for the single hop in linkedMatches.
	Related []string `json:"related,omitempty"`
}

// Lens is loaded from lens.yaml files. Two locations, merged project-over-user:
//  1. $CLAUDE_CONFIG_DIR/lens.yaml    — user-level defaults
//  2. <cwd>/.claude/lens.yaml          — per-project override
//
// MinFusedScore (v2.5) is an optional override of the hardcoded threshold.
// Pointer-typed so the absence of the field is distinguishable from a
// deliberate zero (which would disable the gate entirely).
//
// ExcludeTags / ExcludePaths (v2.6) hard-filter entries OUT of the rankable
// corpus before scoring. Used for project-type awareness: in a notes project
// Python-pipeline tags are noise; in `a speech pipeline` they are signal. Filters union
// across user and project levels (both apply, no override).
//
// WikiIndex (v2.7) selects WHICH compiled corpus the scanner matches against.
// Empty -> the default index_compiled.json (code wiki). A namespace like
// "personal" -> index_compiled_personal.json, letting a workspace recall an entirely
// separate vault. Project value overrides user. The namespace -> vault-path
// mapping lives in corpora.json (single source of truth, also read by
// compile_wiki_index.py to know which corpora to build).
//
// Stem (v2.8) turns on light suffix stemming + stopword drop for the active corpus.
// Off by default (the default corpus stays byte-identical); set true for a corpus in an
// inflected language
// (an opted-in namespace) via corpora.json so inflected prompts ("про пайку") match base-form tags.
type Lens struct {
	Bridge        map[string][]string `yaml:"bridge"`
	Suppress      []string            `yaml:"suppress"`
	Boost         []string            `yaml:"boost"`
	MinFusedScore *float64            `yaml:"min_fused_score,omitempty"`
	ExcludeTags   []string            `yaml:"exclude_tags,omitempty"`
	ExcludePaths  []string            `yaml:"exclude_paths,omitempty"`
	WikiIndex     string              `yaml:"wiki_index,omitempty"`
	Stem          bool                `yaml:"stem,omitempty"`
}

type Cooldown struct {
	SessionID       string   `json:"session_id"`
	RecalledEntries []string `json:"recalled_entries"`
}

type HookOutput struct {
	HookSpecificOutput *HookSpecific `json:"hookSpecificOutput,omitempty"`
}

type HookSpecific struct {
	HookEventName     string `json:"hookEventName"`
	AdditionalContext string `json:"additionalContext,omitempty"`
}

// Match — v2 carries dual-ranker provenance for debugging and future ranking
// adjustments. FusedScore is the final sort key after RRF + lens multipliers.
// TagRank / Bm25Rank are exposed for the v2.5 per-fire score log; rank=N+1
// (sentinel for "unranked", see rankByDesc*) is preserved verbatim so the
// log can distinguish single-rail from dual-rail hits downstream.
type Match struct {
	Entry     IndexEntry
	TagCount  int
	Bm25Score float64
	TagRank   int
	Bm25Rank  int
	// FusedScore is TOPICAL fit and is what the floor tests. RankScore is that same score times
	// the note's track record and is what the ordering uses. They are separate fields because
	// they answer separate questions, and collapsing them let a note's history veto its
	// relevance — see the comment where `ranked` is built.
	FusedScore float64
	RankScore  float64
	MatchedOn  []string
	// LinkedFrom names the match that pulled this one in, empty for a direct match.
	// The signal shows it, because a linked note was NOT what the prompt matched and
	// the reader has to be able to tell the difference.
	LinkedFrom string
}

// ========================================================================================
// CONFIG
// ========================================================================================

const (
	// BM25 tunables — Lucene-style defaults.
	bm25K1 = 1.5
	bm25B  = 0.75

	// RRF fusion constant. k=60 from AgentMemory / LongMemEval-S precedent;
	// softens single-ranker dominance and keeps single-rail wins competitive.
	rrfK = 60.0

	// Lens multipliers applied post-fusion. Replace v1's additive threshold
	// adjustments (+3 / -1) with multiplicative score adjustments on the
	// fused score — preserves semantics ("strong mute" vs "moderate lift").
	suppressMultiplier = 0.2
	boostMultiplier    = 1.5

	// Final gate on the fused score. Calibrated so single-rail rank=1 passes
	// (RRF(1) ≈ 0.0164) but trivial mid-rank single-rail noise does not.
	// This is the v2 calibration knob — adjust after observing live recall.
	minFusedScore = 0.015

	// Drop tokens shorter than this. Keeps short-but-meaningful tags (go, js, ai).
	minTokenLen = 2

	// Three numbers where there used to be one, because the cap now does a different job on
	// each side of the refusal stage (judge.go).
	//
	// maxCandidates is the pool the judge is allowed to consider. It is wide on purpose: the
	// fused score separates applied from ignored firings at AUC 0.548, so a narrow pool is a
	// coin flip deciding what the only stage that CAN assess relevance ever sees. Precision is
	// the judge's job; this number's job is recall. It costs almost nothing — judgeMatches
	// sends one request with the whole listing, so ten candidates and two cost the same call.
	maxCandidates = 10

	// What reaches the prompt once a judgement was actually made.
	maxJudgedResults = 5

	// The cap when NO judgement was made — judge off, ollama down, timeout, unparseable reply.
	maxUnjudgedResults = 4

	// No default vault path: CLAUDE_WIKI_PATH is the only source. Unset means the
	// signal prints note paths relative to the vault root, which still reads fine.
	defaultWikiPath    = ""
	userLensFile       = "lens.yaml"         // lives at $CLAUDE_CONFIG_DIR/lens.yaml
	projectLensRelPath = ".claude/lens.yaml" // lives at <cwd>/.claude/lens.yaml
)

var wikiBasePath = func() string {
	if p := os.Getenv("CLAUDE_WIKI_PATH"); p != "" {
		if !strings.HasSuffix(p, "/") && !strings.HasSuffix(p, "\\") {
			p += "/"
		}
		return p
	}
	return defaultWikiPath
}()

var configDir = func() string {
	if d := os.Getenv("CLAUDE_CONFIG_DIR"); d != "" {
		return d
	}
	home, _ := os.UserHomeDir()
	return filepath.Join(home, ".claude")
}()

var baseDir = filepath.Join(configDir, "hooks", "recall")

// ========================================================================================
// TOKENIZER (v2)
// ========================================================================================

// tokenize splits on non-letter / non-digit boundaries with unicode awareness,
// lowercases, and drops sub-minTokenLen noise. Closes v1's substring-match gap
// where `go` matched inside `together`.
func tokenize(s string) []string {
	var tokens []string
	var current []rune
	flush := func() {
		if len(current) >= minTokenLen {
			tokens = append(tokens, string(current))
		}
		current = current[:0]
	}
	for _, r := range s {
		if unicode.IsLetter(r) || unicode.IsDigit(r) {
			current = append(current, unicode.ToLower(r))
		} else {
			flush()
		}
	}
	flush()
	return tokens
}

// ========================================================================================
// STEMMING FOR INFLECTED LANGUAGES — per-corpus, opt-in via corpora.json "stem": true
// ========================================================================================
//
// English needs none of this and the default corpus does not use it. A language that
// declines its nouns does: a tag in the dictionary form misses the same word in another
// case, on both the substring rail and BM25, and the miss is silent. A light suffix stemmer
// folds inflected forms to a shared stem so BM25 matches across cases, and a stopword drop
// removes the connective words that otherwise cause spurious single-rail hits.
//
// Russian ships as the worked example, because it is heavily inflected and therefore the
// hard case. Supporting another language means adding its suffix list and stopwords beside
// these; nothing else in the ranker changes. Applied ONLY when the active corpus opts in,
// so the default stays byte-identical and its tests are unaffected. Stdlib-only, no
// dependency, which keeps the per-machine build light.

var ruStopwords = map[string]bool{
	"не": true, "на": true, "по": true, "из": true, "от": true, "до": true, "за": true,
	"то": true, "же": true, "бы": true, "ли": true, "ну": true, "об": true, "во": true,
	"со": true, "ко": true, "про": true, "как": true, "что": true, "это": true, "так": true,
	"или": true, "для": true, "нет": true, "вот": true, "его": true, "ее": true, "их": true,
	"мне": true, "при": true, "под": true, "над": true, "без": true, "там": true, "тут": true,
	"был": true, "уже": true, "если": true, "чтобы": true, "когда": true, "очень": true,
}

// ruSuffixes — inflectional endings of the shipped example language, stripped longest-first (sorted at
// init). stemRu removes the first suffix that leaves a stem of >= 3 runes.
var ruSuffixes = func() []string {
	s := []string{
		"иями", "ость", "ыми", "ими", "ями", "ами", "ого", "его", "ому", "ему",
		"ией", "иях", "иям", "ием", "ия", "ию", "ии", "ие", "ей", "ой", "ый", "ий",
		"ая", "яя", "ое", "ее", "ые", "ом", "ем", "ах", "ях", "ов", "ев", "ью", "ть", "ся",
		"а", "я", "о", "е", "у", "ю", "ы", "и", "й", "ь",
	}
	sort.SliceStable(s, func(i, j int) bool { return len([]rune(s[i])) > len([]rune(s[j])) })
	return s
}()

func hasCyrillic(s string) bool {
	for _, r := range s {
		if r >= 0x0400 && r <= 0x04FF {
			return true
		}
	}
	return false
}

// stemRu folds an inflected word to a stem by stripping the longest matching
// inflectional suffix while keeping the stem >= 3 runes. Non-Cyrillic tokens and short
// words (< 5 runes) pass through unchanged.
func stemRu(t string) string {
	if !hasCyrillic(t) {
		return t
	}
	runes := []rune(t)
	if len(runes) < 5 {
		return t
	}
	for _, suf := range ruSuffixes {
		n := len([]rune(suf))
		if len(runes)-n >= 3 && strings.HasSuffix(t, suf) {
			return string(runes[:len(runes)-n])
		}
	}
	return t
}

// analyzeTokens post-processes raw tokens for the active corpus. stem=false (default /
// code corpus) is a pass-through preserving baseline behavior exactly. stem=true (an opted-in namespace)
// drops the stopwords of the stemmed language and stems its remaining tokens.
func analyzeTokens(toks []string, stem bool) []string {
	if !stem {
		return toks
	}
	out := make([]string, 0, len(toks))
	for _, t := range toks {
		if ruStopwords[t] {
			continue
		}
		out = append(out, stemRu(t))
	}
	return out
}

// ========================================================================================
// BM25 (v2)
// ========================================================================================

type bm25Doc struct {
	TF     map[string]int
	DocLen int
}

type bm25Corpus struct {
	DocCount  int
	AvgDocLen float64
	DocFreq   map[string]int
	Docs      []bm25Doc
}

// buildBM25Corpus precomputes per-document term frequencies + corpus stats.
// Cost is O(total tokens), ~1ms for a 50-entry wiki on a modern CPU.
// Document = name + tags + summary (no field weighting; tags already get
// independent privilege via the tag-substring ranker).
func buildBM25Corpus(entries []IndexEntry, stem bool) bm25Corpus {
	corpus := bm25Corpus{
		DocCount: len(entries),
		DocFreq:  make(map[string]int),
		Docs:     make([]bm25Doc, len(entries)),
	}
	totalLen := 0
	for i, entry := range entries {
		text := entry.Name + " " + strings.Join(entry.Tags, " ") + " " + entry.Summary
		tokens := analyzeTokens(tokenize(text), stem)
		tf := make(map[string]int, len(tokens))
		for _, t := range tokens {
			tf[t]++
		}
		corpus.Docs[i] = bm25Doc{TF: tf, DocLen: len(tokens)}
		totalLen += len(tokens)
		for term := range tf {
			corpus.DocFreq[term]++
		}
	}
	if corpus.DocCount > 0 {
		corpus.AvgDocLen = float64(totalLen) / float64(corpus.DocCount)
	}
	return corpus
}

// bm25Score uses Lucene's non-negative IDF: ln(1 + (N - n + 0.5) / (n + 0.5)).
func bm25Score(query []string, doc bm25Doc, corpus bm25Corpus) float64 {
	if doc.DocLen == 0 || corpus.AvgDocLen == 0 {
		return 0
	}
	score := 0.0
	for _, qt := range query {
		n := corpus.DocFreq[qt]
		if n == 0 {
			continue
		}
		tf := float64(doc.TF[qt])
		if tf == 0 {
			continue
		}
		idf := math.Log(1 + (float64(corpus.DocCount-n)+0.5)/(float64(n)+0.5))
		numerator := tf * (bm25K1 + 1)
		denominator := tf + bm25K1*(1-bm25B+bm25B*float64(doc.DocLen)/corpus.AvgDocLen)
		score += idf * numerator / denominator
	}
	return score
}

// ========================================================================================
// RANKING & RRF FUSION (v2)
// ========================================================================================

// rankByDescInt returns strict ordinal 1-based ranks. Items with score=0 get
// rank=n+1 — a sentinel that signals "unranked"; the fusion step uses this to
// suppress that ranker's RRF contribution to zero (see findMatches).
// Ties resolved by sort.SliceStable, which preserves index order.
func rankByDescInt(scores []int) []int {
	n := len(scores)
	indices := make([]int, n)
	for i := range indices {
		indices[i] = i
	}
	sort.SliceStable(indices, func(a, b int) bool {
		return scores[indices[a]] > scores[indices[b]]
	})
	ranks := make([]int, n)
	for pos, idx := range indices {
		if scores[idx] == 0 {
			ranks[idx] = n + 1
		} else {
			ranks[idx] = pos + 1
		}
	}
	return ranks
}

func rankByDescFloat(scores []float64) []int {
	n := len(scores)
	indices := make([]int, n)
	for i := range indices {
		indices[i] = i
	}
	sort.SliceStable(indices, func(a, b int) bool {
		return scores[indices[a]] > scores[indices[b]]
	})
	ranks := make([]int, n)
	for pos, idx := range indices {
		if scores[idx] == 0 {
			ranks[idx] = n + 1
		} else {
			ranks[idx] = pos + 1
		}
	}
	return ranks
}

func rrf(rank int) float64 {
	return 1.0 / (rrfK + float64(rank))
}

// ========================================================================================
// MATCHER (v2 — RRF fusion of tag + BM25)
// ========================================================================================

func normalizeText(s string) string {
	return strings.ToLower(strings.TrimSpace(s))
}

func tagInList(tag string, list []string) bool {
	norm := normalizeText(tag)
	for _, t := range list {
		if normalizeText(t) == norm {
			return true
		}
	}
	return false
}

// applyExcludes drops entries whose tags or path hit the lens exclusion
// rules. Tag match is case-insensitive exact; path match is a forward-slash
// glob via `path.Match` (so `apis/*tts*.md` filters `apis/openai-tts.md`).
// Backslashes in stored paths are normalized to forward slashes so wiki
// indexes written on Windows match Unix-style globs identically.
//
// Both lists are unions across user + project lens — if either says exclude,
// the entry is out. This makes "global never-show" rules safe to stack with
// per-project additions without surprises.
func applyExcludes(entries []IndexEntry, excludeTags, excludePaths []string) []IndexEntry {
	if len(excludeTags) == 0 && len(excludePaths) == 0 {
		return entries
	}
	kept := make([]IndexEntry, 0, len(entries))
	for _, e := range entries {
		if matchAnyTag(e.Tags, excludeTags) {
			continue
		}
		if matchAnyPathGlob(e.Path, excludePaths) {
			continue
		}
		kept = append(kept, e)
	}
	return kept
}

func matchAnyTag(entryTags, excludeTags []string) bool {
	for _, t := range entryTags {
		if tagInList(t, excludeTags) {
			return true
		}
	}
	return false
}

func matchAnyPathGlob(entryPath string, globs []string) bool {
	if entryPath == "" || len(globs) == 0 {
		return false
	}
	norm := strings.ToLower(strings.ReplaceAll(entryPath, "\\", "/"))
	for _, g := range globs {
		pattern := strings.ToLower(strings.ReplaceAll(g, "\\", "/"))
		if ok, err := path.Match(pattern, norm); err == nil && ok {
			return true
		}
	}
	return false
}

// findMatches runs two independent rankers over the post-cooldown index,
// fuses them via Reciprocal Rank Fusion, applies lens multipliers, and
// returns top-K entries above minFusedScore.
//
// v2 changes from v1:
//   - Adds BM25 as a second rail. Closes the substring word-boundary gap
//     (`go` no longer matches inside `together`) and surfaces entries whose
//     summary content matches the prompt even when no tag does.
//   - Replaces v1's per-entry additive threshold with RRF + a single global
//     minFusedScore. Lens boost/suppress become score multipliers.
//   - Soft RRF: an entry unranked in a ranker (score=0) contributes 0 from
//     that rail — guards against accidental low-quality matches passing the
//     gate just by being non-zero everywhere.
//
// ============================================================
// EVIDENCE — the ledger finally reaches ranking
// ============================================================
//
// recall_feedback.py has recorded, for every note, how often it was shown and how often it was
// then marked used. Until now that number went to a human who was expected to hand-tune a lens
// from it, and the file's own docstring said so: "NEVER fed back into scanner ranking". After
// four months the global lens was still empty while the ledger held 1357 firings and 31 uses,
// so the loop was closed by nobody. This is that loop, closed in code.
//
// TWO DOORS, and a note alive through either is alive. The pushed rail ranks a note's label; the
// pulled MCP search reads its body. A note whose body answers questions while its summary does
// not would look dead on the first counter alone, and damping it is precisely the wrong move.
//
// NO THRESHOLD, and the base rate is why. Across the corpus a firing is applied about 2.3% of
// the time, so a note sitting at fifteen firings and zero uses is what an ordinary note looks
// like 70% of the time — "never used" is thin evidence at any single cutoff. A step function
// would dress that thin evidence as a verdict. Instead the damp is continuous and proportional:
// the more often a note has been shown without ever landing, the further it slides, and a note
// with three firings is left essentially alone.
type LedgerEntry struct {
	FiredSessions       []string `json:"fired_sessions"`
	UsedSessions        []string `json:"used_sessions"`
	Confidence          float64  `json:"confidence"`
	SmritiFiredSessions []string `json:"smriti_fired_sessions"`
	SmritiUsedSessions  []string `json:"smriti_used_sessions"`
	SmritiConfidence    float64  `json:"smriti_confidence"`
}

const (
	// Maximum lift for a note that has proved itself: confidence 1.0 -> 1.5x.
	evidenceLiftGain = 0.5
	// Damp half-life in firings: a never-used note at 30 firings scores half. 15 -> 0.67x,
	// 41 (the current loudest) -> 0.58x.
	evidenceDampScale = 30.0
)

var ledger map[string]LedgerEntry

// loadLedger reads recall_stats.json. A missing or unreadable ledger yields a nil map, and a nil
// map returns zero entries — which evidenceMultiplier turns into 1.0. Ranking without evidence is
// ranking as it was before this existed, which is the correct degradation: the feedback loop
// going quiet must never change what the rail believes about a note.
func loadLedger() map[string]LedgerEntry {
	data, err := os.ReadFile(filepath.Join(baseDir, "recall_stats.json"))
	if err != nil {
		return nil
	}
	var out map[string]LedgerEntry
	if json.Unmarshal(data, &out) != nil {
		return nil
	}
	return out
}

// evidenceMultiplier converts a note's history into a score multiplier.
//
//	used at least once -> 1.0 .. 1.5   (lift, by best confidence across the two doors)
//	never used         -> 1/(1+n/30)   (damp, continuous in firings)
//	never fired        -> 1.0          (unproven is NEUTRAL, never penalised)
//
// That last line is the one that keeps the system able to learn. A new note starts with no
// history; if absence of evidence were treated as evidence of absence it would rank below
// everything, never be shown, never acquire history, and stay there — proven notes would
// entrench and nothing new could ever displace them.
func evidenceMultiplier(e LedgerEntry) float64 {
	fires := len(e.FiredSessions) + len(e.SmritiFiredSessions)
	uses := len(e.UsedSessions) + len(e.SmritiUsedSessions)
	if uses > 0 {
		conf := e.Confidence
		if e.SmritiConfidence > conf {
			conf = e.SmritiConfidence
		}
		return 1.0 + evidenceLiftGain*conf
	}
	if fires == 0 {
		return 1.0
	}
	return 1.0 / (1.0 + float64(fires)/evidenceDampScale)
}

func findMatches(text string, index []IndexEntry, lens Lens, recalled []string) []Match {
	normalized := normalizeText(text)
	promptTokens := analyzeTokens(tokenize(normalized), lens.Stem)

	// Bridge expansion — semantic cluster semantics (v2.1 fix):
	// each bridge entry {key: [values...]} defines a semantic cluster
	// of size 1+len(values). If ANY member of a cluster appears in the
	// prompt, ALL members are added to the virtual tag pool. This is
	// what the original CONCEPT.md described; the v1 implementation
	// accidentally iterated only the values, producing a near-no-op.
	bridgeAdditions := []string{}
	if lens.Bridge != nil {
		for key, vals := range lens.Bridge {
			cluster := append([]string{key}, vals...)
			triggered := false
			for _, t := range cluster {
				if strings.Contains(normalized, normalizeText(t)) {
					triggered = true
					break
				}
			}
			if triggered {
				for _, t := range cluster {
					bridgeAdditions = appendUnique(bridgeAdditions, t)
				}
			}
		}
	}
	for _, bt := range bridgeAdditions {
		promptTokens = append(promptTokens, analyzeTokens(tokenize(bt), lens.Stem)...)
	}

	// Project-type exclusion (v2.6): drop entries that the lens flags as
	// always-noise for this project. Runs before cooldown + BM25 so corpus
	// stats reflect only the rankable population.
	rankable := applyExcludes(index, lens.ExcludeTags, lens.ExcludePaths)

	// Cooldown filter happens before scoring so the BM25 corpus statistics
	// reflect only the rankable population.
	active := make([]IndexEntry, 0, len(rankable))
	for _, e := range rankable {
		if !tagInList(e.Name, recalled) {
			active = append(active, e)
		}
	}
	if len(active) == 0 {
		return nil
	}

	// Ranker A: tag substring count (v1 logic, no threshold cut).
	tagCounts := make([]int, len(active))
	matchedOn := make([][]string, len(active))
	for i, entry := range active {
		for _, tag := range entry.Tags {
			if strings.Contains(normalized, normalizeText(tag)) {
				tagCounts[i]++
				matchedOn[i] = append(matchedOn[i], tag)
				continue
			}
			if tagInList(tag, bridgeAdditions) {
				tagCounts[i]++
				matchedOn[i] = append(matchedOn[i], tag+"*")
			}
		}
	}

	// Ranker B: BM25 over tokenized prompt vs entry document.
	corpus := buildBM25Corpus(active, lens.Stem)
	bm25Scores := make([]float64, len(active))
	for i, doc := range corpus.Docs {
		bm25Scores[i] = bm25Score(promptTokens, doc, corpus)
	}

	tagRanks := rankByDescInt(tagCounts)
	bm25Ranks := rankByDescFloat(bm25Scores)

	// RRF fusion. Unranked entries (tagCount=0 or bm25=0) contribute 0 from
	// that rail — this is what prevents zero-everywhere entries from passing
	// minFusedScore by accident on small corpora.
	fused := make([]float64, len(active))
	// The same notes scored twice: `fused` is topical fit and answers "does this belong in the
	// pool at all"; `ranked` folds in the note's track record and answers "who goes first".
	ranked := make([]float64, len(active))
	for i, entry := range active {
		score := 0.0
		if tagCounts[i] > 0 {
			score += rrf(tagRanks[i])
		}
		if bm25Scores[i] > 0 {
			score += rrf(bm25Ranks[i])
		}
		if tagInList(entry.Name, lens.Suppress) {
			score *= suppressMultiplier
		}
		for _, t := range entry.Tags {
			if tagInList(t, lens.Boost) {
				score *= boostMultiplier
				break
			}
		}
		fused[i] = score
		// Evidence ranks; it does not gate. Two different questions live here and mixing them
		// was a live false negative: `yaml-reserved-words` stopped answering "in yaml `off`
		// becomes false" — its own subject, and the vault's own canonical true positive —
		// because 41 fruitless firings damped it under the floor while two lifted notes about
		// nothing in particular took its place. A track record must never get a veto over
		// topical fit. So the FLOOR reads the topical score, and only the ORDER reads history.
		//
		// Lens suppress/boost stay above this line on purpose: those are a human saying a note
		// should or should not be in the rail at all, which IS a gate.
		ranked[i] = score * evidenceMultiplier(ledger[entry.Name])
	}

	// Threshold: lens override wins over the hardcoded default (v2.5).
	threshold := minFusedScore
	if lens.MinFusedScore != nil {
		threshold = *lens.MinFusedScore
	}

	var matches []Match
	for i, entry := range active {
		// Gated on TOPICAL fit only — see the note at `ranked`. A damped note that genuinely
		// matches still enters the pool and still gets judged; it just queues behind the notes
		// that have earned their place.
		if fused[i] < threshold {
			continue
		}
		matches = append(matches, Match{
			Entry:      entry,
			TagCount:   tagCounts[i],
			Bm25Score:  bm25Scores[i],
			TagRank:    tagRanks[i],
			Bm25Rank:   bm25Ranks[i],
			FusedScore: fused[i],
			RankScore:  ranked[i],
			MatchedOn:  matchedOn[i],
		})
	}

	sort.SliceStable(matches, func(a, b int) bool {
		return matches[a].RankScore > matches[b].RankScore
	})
	return capMatches(matches, maxCandidates)
}

// capMatches trims a ranked list to limit. Split out because the cap is now applied twice on
// the wiki path — once to bound what the judge considers, once to bound what the prompt sees —
// and the two limits are deliberately different numbers.
func capMatches(matches []Match, limit int) []Match {
	if len(matches) > limit {
		return matches[:limit]
	}
	return matches
}

// displayLimit is how many wiki notes may reach the prompt, and it is a function of whether a
// judgement was made rather than a constant. Unjudged output is raw similarity, so it stays at
// the width that was live before the refusal stage existed.
func displayLimit(judged bool) int {
	if judged {
		return maxJudgedResults
	}
	return maxUnjudgedResults
}

const recallHeader = "[RECALL] Wiki notes matched by your prompt tags. For each: read the summary and decide if it enriches the current task. " +
	"If yes — Read the full file at the given path, apply the insight, AND you MUST emit `[RECALL-USED] <name> — <one-line why>` somewhere in your response. " +
	"This marker is the explicit signal for the feedback ledger — failing to emit it when a recall was actually applied corrupts the data the system uses to tune itself. " +
	"If not — ignore silently, do NOT mention the signal to the user."

// Two headers, because the judge has two honest states and they must not read alike.
//
// judged: a local model was shown your message and these notes and kept only the ones it
// thinks bear on it. Say so, so the reader knows a second opinion already ran.
//
// unjudged: the judge could not be reached (switched off, ollama down, timeout, unparseable).
// These are RAW floor matches — the same output the rail produced before the refusal stage
// existed. It says so loudly, because a judgement that was never made must never arrive
// wearing the clothes of one that was.
const recallJudgedNote = " These were CHECKED for relevance: a local model read your message and " +
	"dropped the notes that do not bear on it, so what survived is a considered shortlist rather than " +
	"the closest few by keyword."

const recallUncheckedTail = ", so these are raw keyword matches, ranked by similarity to your " +
	"words and not by whether they answer you. Weigh them yourself."

// uncheckedNote names WHICH nothing the rail is reporting. One sentence used to cover seven
// different failures, so a deliberate opt-out and a model answering in prose read identically
// and neither could be acted on.
func uncheckedNote(outcome JudgeOutcome, budgetNote string) string {
	switch outcome {
	case JudgeOff:
		return " RELEVANCE NOT CHECKED — the judge is switched off (RECALL_JUDGE=0)" +
			recallUncheckedTail
	case JudgeDeadline:
		if budgetNote != "" {
			return " RELEVANCE NOT CHECKED — the judge ran out of time, and it was running on a " +
				"fallback deadline because " + budgetNote + recallUncheckedTail
		}
		return " RELEVANCE NOT CHECKED — the judge ran out of the time this hook is allowed" +
			recallUncheckedTail
	case JudgeUnreachable:
		return " RELEVANCE NOT CHECKED — the judge could not be reached at " + judgeURL +
			recallUncheckedTail
	case JudgeUnparseable:
		return " RELEVANCE NOT CHECKED — the judge answered in a shape this rail could not read" +
			recallUncheckedTail
	}
	return " RELEVANCE NOT CHECKED" + recallUncheckedTail
}

const proceduralHeader = "[PROCEDURAL] Action sequence(s) that worked in this project before. For each: read the one-line why and decide if it fits the current task. " +
	"If yes — open the file at the given path, follow the steps, AND you MUST emit `[PROCEDURAL-USED] <name> — <one-line why>` somewhere in your response. " +
	"This marker is the explicit signal the feedback ledger uses to tune which sequences are worth keeping. " +
	"If not — ignore silently, do NOT mention the signal to the user."

// railBrokenHeader is the one signal that is NOT background info. A corpus that fails
// to load makes the rail blind, and a blind rail is silent in exactly the same way a
// genuine no-match is — so it must announce itself or it will never be noticed.
const railBrokenHeader = "[RAIL-BROKEN] A recall corpus failed to load. This is a BUG, not an empty corpus — " +
	"the affected rail matched nothing because it could not read its index, not because nothing matched. " +
	"Recall is BLIND for that rail this session. Tell the user now, quoting the error; do not treat a missing " +
	"[RECALL]/[PROCEDURAL] block as evidence that the wiki or the recipe book has nothing on the topic."

// ========================================================================================
// LINK HOP — one step along the note's own `related` edges
// ========================================================================================
//
// Written into a note by hand or by /compile, the `related` list is an assertion that
// two notes travel together. This walks exactly one edge out of the STRONGEST match, so
// a note that is right but worded differently can still arrive.
//
// It is deliberately timid, because a hop from a weak match is a noise multiplier:
//   - only the top match hops, never the rest;
//   - at most one linked note is added;
//   - a note already in the results is never re-added;
//   - the hop does not chain, so a link two steps out cannot reach the prompt.
//
// The linked note is marked in the output rather than blended in, since it did not
// match anything the user typed.
func linkedMatches(matches []Match, index []IndexEntry) []Match {
	if len(matches) == 0 {
		return matches
	}
	source := matches[0]
	if len(source.Entry.Related) == 0 {
		return matches
	}

	present := make(map[string]bool, len(matches))
	for _, m := range matches {
		present[m.Entry.Path] = true
		present[m.Entry.Name] = true
	}

	byKey := make(map[string]IndexEntry, len(index)*2)
	for _, e := range index {
		byKey[e.Path] = e
		byKey[e.Name] = e
	}

	for _, ref := range source.Entry.Related {
		ref = strings.TrimSpace(ref)
		if ref == "" || present[ref] {
			continue
		}
		entry, ok := byKey[ref]
		if !ok {
			// A link can point at a note that was deleted or never existed. That is the
			// author's problem, not the scanner's: skip it silently and keep going.
			continue
		}
		if present[entry.Path] || present[entry.Name] {
			continue
		}
		return append(matches, Match{
			Entry:      entry,
			FusedScore: source.FusedScore,
			LinkedFrom: source.Entry.Name,
		})
	}
	return matches
}

// formatSignal renders one matched-corpus block: a header plus one line per match
// (name: summary -> pathPrefix+path). Shared by the wiki ([RECALL]) and procedural
// ([PROCEDURAL]) rails — they differ only in header and path prefix.
func formatSignal(matches []Match, header, pathPrefix string) string {
	parts := []string{header}
	for _, m := range matches {
		summary := strings.TrimSpace(m.Entry.Summary)
		if summary == "" {
			summary = "(no summary)"
		}
		linked := ""
		if m.LinkedFrom != "" {
			linked = fmt.Sprintf(" [linked from %s — it did not match your words, it is the note that one points to]", m.LinkedFrom)
		}
		parts = append(parts, fmt.Sprintf(
			"  - %s: %s → %s%s%s",
			m.Entry.Name,
			summary,
			pathPrefix,
			m.Entry.Path,
			linked,
		))
	}
	return strings.Join(parts, "\n")
}

// ========================================================================================
// FILE I/O
// ========================================================================================

// loadIndex reads the compiled corpus for the given namespace. Empty namespace
// loads the default corpus (index_compiled.json); namespace "personal" loads
// index_compiled_personal.json. This is the per-workspace corpus switch (v2.7)
// and the seam Layer 6 will reuse to load additional corpora.
//
// Returns (nil, nil) when the file is simply absent — a project with no corpus is
// a valid state, not a fault. Any OTHER failure (unreadable, malformed JSON) comes
// back as an error and MUST NOT be flattened into "no matches": a blind rail that
// looks like a quiet miss is how Layer 5/6 dies unnoticed for weeks.
func loadIndex(namespace string) ([]IndexEntry, error) {
	fname := "index_compiled.json"
	if namespace != "" {
		fname = "index_compiled_" + namespace + ".json"
	}
	return readCorpus(filepath.Join(baseDir, fname))
}

// loadProcedural reads the project-local compiled recipe corpus (Layer 6) from
// <cwd>/.claude/procedural_compiled.json. Unlike the wiki corpus (global, in
// baseDir), procedurals are per-project. A missing file is a valid state; a
// corrupt one is not — see readCorpus.
func loadProcedural(cwd string) ([]IndexEntry, error) {
	if cwd == "" {
		return nil, nil
	}
	return readCorpus(filepath.Join(cwd, ".claude", "procedural_compiled.json"))
}

// readCorpus loads one compiled corpus file, keeping "absent" and "broken" apart.
//
// Absent  -> (nil, nil): no corpus configured here. Silence is correct.
// Broken  -> (nil, err): unreadable, or valid-file-invalid-JSON. Silence is a LIE —
//
//	it is indistinguishable from an honest miss, and the caller surfaces it
//	instead of emitting nothing.
func readCorpus(fpath string) ([]IndexEntry, error) {
	data, err := os.ReadFile(fpath)
	if errors.Is(err, fs.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, fmt.Errorf("read %s: %w", filepath.Base(fpath), err)
	}
	var index []IndexEntry
	if err := json.Unmarshal(data, &index); err != nil {
		return nil, fmt.Errorf("parse %s: %w", filepath.Base(fpath), err)
	}
	return index, nil
}

// sanitizeNamespace strips the lens-supplied corpus namespace down to a safe
// filename fragment: lowercase letters, digits, dash, underscore. Anything
// else (path separators, dots, "..") is dropped, so a stray lens value can
// never escape baseDir when composing index_compiled_<ns>.json.
func sanitizeNamespace(ns string) string {
	var b strings.Builder
	for _, r := range ns {
		if unicode.IsLetter(r) || unicode.IsDigit(r) || r == '-' || r == '_' {
			b.WriteRune(unicode.ToLower(r))
		}
	}
	return b.String()
}

// corporaWikiPath resolves the display root for a namespace's notes, read from
// the corpora.json registry (namespace -> {wiki_path}). The [RECALL] signal
// prepends this to each entry's vault-relative path so the agent can open it.
// Empty namespace or any miss falls back to wikiBasePath — keeping existing
// (non-namespaced) workspaces byte-identical.
func corporaWikiPath(namespace string) string {
	if namespace == "" {
		return wikiBasePath
	}
	data, err := os.ReadFile(filepath.Join(baseDir, "corpora.json"))
	if err != nil {
		return wikiBasePath
	}
	var reg map[string]struct {
		WikiPath string `json:"wiki_path"`
	}
	if json.Unmarshal(data, &reg) != nil {
		return wikiBasePath
	}
	entry, ok := reg[namespace]
	if !ok || entry.WikiPath == "" {
		return wikiBasePath
	}
	p := entry.WikiPath
	if !strings.HasSuffix(p, "/") && !strings.HasSuffix(p, "\\") {
		p += "/"
	}
	return p
}

// corporaStem reports whether the namespace's corpus opts into stemming
// (corpora.json "stem": true). Empty namespace / miss -> false (default corpus).
func corporaStem(namespace string) bool {
	if namespace == "" {
		return false
	}
	data, err := os.ReadFile(filepath.Join(baseDir, "corpora.json"))
	if err != nil {
		return false
	}
	var reg map[string]struct {
		Stem bool `json:"stem"`
	}
	if json.Unmarshal(data, &reg) != nil {
		return false
	}
	return reg[namespace].Stem
}

func loadLensFile(path string) Lens {
	var lens Lens
	data, err := os.ReadFile(path)
	if err != nil {
		return lens
	}
	yaml.Unmarshal(data, &lens)
	return lens
}

func mergeLens(a, b Lens) Lens {
	merged := Lens{Bridge: make(map[string][]string)}
	for k, v := range a.Bridge {
		for _, tag := range v {
			merged.Bridge[k] = appendUnique(merged.Bridge[k], tag)
		}
	}
	for k, v := range b.Bridge {
		for _, tag := range v {
			merged.Bridge[k] = appendUnique(merged.Bridge[k], tag)
		}
	}
	merged.Suppress = mergeUnique(a.Suppress, b.Suppress)
	merged.Boost = mergeUnique(a.Boost, b.Boost)

	// MinFusedScore: project (b) override wins; otherwise user (a); otherwise nil.
	if b.MinFusedScore != nil {
		merged.MinFusedScore = b.MinFusedScore
	} else {
		merged.MinFusedScore = a.MinFusedScore
	}

	// Excludes union — global rules + project additions BOTH apply (v2.6).
	merged.ExcludeTags = mergeUnique(a.ExcludeTags, b.ExcludeTags)
	merged.ExcludePaths = mergeUnique(a.ExcludePaths, b.ExcludePaths)

	// WikiIndex (v2.7): project (b) override wins; else user (a); else "".
	if b.WikiIndex != "" {
		merged.WikiIndex = b.WikiIndex
	} else {
		merged.WikiIndex = a.WikiIndex
	}

	// Stem (v2.8): either level can turn it on.
	merged.Stem = a.Stem || b.Stem
	return merged
}

func appendUnique(slice []string, item string) []string {
	for _, s := range slice {
		if s == item {
			return slice
		}
	}
	return append(slice, item)
}

func mergeUnique(a, b []string) []string {
	result := append([]string{}, a...)
	for _, item := range b {
		result = appendUnique(result, item)
	}
	return result
}

func loadLens(cwd string) Lens {
	user := loadLensFile(filepath.Join(configDir, userLensFile))
	if cwd == "" {
		return user
	}
	project := loadLensFile(filepath.Join(cwd, projectLensRelPath))
	return mergeLens(user, project)
}

func cooldownPath(sessionID string) string {
	return filepath.Join(baseDir, "cooldown_"+sessionID+".json")
}

func loadCooldown(sessionID string) []string {
	data, err := os.ReadFile(cooldownPath(sessionID))
	if err != nil {
		return nil
	}
	var cd Cooldown
	if err := json.Unmarshal(data, &cd); err != nil {
		return nil
	}
	return cd.RecalledEntries
}

func saveCooldown(sessionID string, recalled []string) {
	cd := Cooldown{SessionID: sessionID, RecalledEntries: recalled}
	data, _ := json.Marshal(cd)
	os.WriteFile(cooldownPath(sessionID), data, 0644)
}

// fireLogPath returns the per-session sidecar path for fire records.
func fireLogPath(sessionID string) string {
	return filepath.Join(baseDir, "fires_"+sessionID+".jsonl")
}

// appendFireLog writes one JSONL line per match into the per-session sidecar.
// Consumed by recall_feedback.py at Stop time, where it is cross-referenced
// with [RECALL-USED] markers and Read tool_use events to fill in the `used`
// flag, then merged into the ledger's fire_log and deleted.
//
// Schema (per line):
//
//	{ "ts": "<iso8601 utc>", "name": "<entry>", "fused": <float>,
//	  "tag_rank": <int>, "bm25_rank": <int> }
//
// Errors are silent — sidecar logging must never affect the recall pipeline.
func appendFireLog(sessionID string, matches []Match) {
	if sessionID == "" || len(matches) == 0 {
		return
	}
	f, err := os.OpenFile(fireLogPath(sessionID), os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0644)
	if err != nil {
		return
	}
	defer f.Close()

	ts := time.Now().UTC().Format(time.RFC3339)
	for _, m := range matches {
		record := struct {
			Ts       string  `json:"ts"`
			Name     string  `json:"name"`
			Fused    float64 `json:"fused"`
			TagRank  int     `json:"tag_rank"`
			Bm25Rank int     `json:"bm25_rank"`
		}{ts, m.Entry.Name, m.FusedScore, m.TagRank, m.Bm25Rank}
		data, err := json.Marshal(record)
		if err != nil {
			continue
		}
		f.Write(data)
		f.WriteString("\n")
	}
}

// appendProceduralFires logs procedural matches to a PER-PROJECT sidecar
// (<cwd>/.claude/procedural_fires.jsonl). Recipes are per-project (unlike the global
// wiki corpus), so their feedback ledger lives with the project. Consumed at Stop by
// procedural_feedback.py, cross-referenced with [PROCEDURAL-USED] markers.
// Schema per line: { "ts": "<iso8601 utc>", "session": "<id>", "name": "<slug>" }.
// Silent on error — sidecar logging must never affect the recall pipeline.
func appendProceduralFires(cwd, sessionID string, matches []Match) {
	if cwd == "" || sessionID == "" || len(matches) == 0 {
		return
	}
	f, err := os.OpenFile(filepath.Join(cwd, ".claude", "procedural_fires.jsonl"),
		os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0644)
	if err != nil {
		return
	}
	defer f.Close()

	ts := time.Now().UTC().Format(time.RFC3339)
	for _, m := range matches {
		record := struct {
			Ts      string `json:"ts"`
			Session string `json:"session"`
			Name    string `json:"name"`
		}{ts, sessionID, m.Entry.Name}
		if data, err := json.Marshal(record); err == nil {
			f.Write(data)
			f.WriteString("\n")
		}
	}
}

// ========================================================================================
// MAIN
// ========================================================================================

func main() {
	inputBytes, err := io.ReadAll(os.Stdin)
	if err != nil {
		os.Exit(1)
	}
	var input HookInput
	if err := json.Unmarshal(inputBytes, &input); err != nil {
		os.Exit(1)
	}
	if input.Prompt == "" || isMachinePrompt(input) {
		fmt.Print("{}")
		os.Exit(0)
	}

	lens := loadLens(input.Cwd)
	namespace := sanitizeNamespace(lens.WikiIndex)
	lens.Stem = lens.Stem || corporaStem(namespace)

	// A load ERROR is not an empty corpus. Collect failures and report them below —
	// exiting quietly here would make a broken rail look exactly like a clean miss.
	var railErrors []string
	wikiIndex, wikiErr := loadIndex(namespace)
	if wikiErr != nil {
		railErrors = append(railErrors, "wiki (Layer 5): "+wikiErr.Error())
	}
	procIndex, procErr := loadProcedural(input.Cwd)
	if procErr != nil {
		railErrors = append(railErrors, "procedural (Layer 6): "+procErr.Error())
	}
	if len(wikiIndex) == 0 && len(procIndex) == 0 && len(railErrors) == 0 {
		fmt.Print("{}")
		os.Exit(0)
	}

	ledger = loadLedger()
	recalled := loadCooldown(input.SessionID)

	// Two passes over the SAME ranking engine: wiki (Layer 5) then procedural
	// (Layer 6). Shared per-session cooldown — a name surfaced by the first pass
	// is suppressed in the second. The two passes are no longer symmetric: the wiki
	// pass is judged, so it draws a wide pool and is trimmed afterwards; the
	// procedural pass has no judge and stays at the old tight cap.
	wikiCandidates := linkedMatches(findMatches(input.Prompt, wikiIndex, lens, recalled), wikiIndex)

	// The refusal stage (judge.go). The floor ranks by topical similarity, which the live
	// telemetry says is not the question — it separates applied from ignored firings at
	// AUC 0.548, and no floor in its whole range moves precision off the base rate. A
	// judgement can say "none of these"; a threshold never can.
	//
	// The cap moved to AFTER this call, and that ordering is the whole point. Before, the
	// coin-flip score picked two and the judge could only ever say no to those two — it had
	// no way to reach a note the ranking had already buried. Now it rules on the pool and
	// the trim happens on what survived.
	wikiMatches := wikiCandidates
	wikiJudged := false
	kept, outcome := judgeMatches(input.Prompt, wikiCandidates)
	if outcome == JudgeRuled {
		wikiMatches, wikiJudged = kept, true
	}
	// Read for the note, not for the call: judgeMatches already used the budget. This second
	// read costs one small file and buys the reader the reason a fallback deadline was in force.
	_, budgetNote := judgeBudget()
	appendJudgeLog(input.SessionID, wikiCandidates, wikiMatches, wikiJudged)

	// A wide pool is only safe because something ruled on it. With no judgement the rail
	// falls back to the pre-judge width, so a stopped ollama quietly narrows recall instead
	// of loudly flooding the prompt with ten unchecked notes.
	wikiMatches = capMatches(wikiMatches, displayLimit(wikiJudged))

	// Only SURFACED notes go on cooldown. A note the judge dropped was never shown, so there
	// is no repetition to suppress — and burying it for the rest of the session on the
	// strength of one prompt's verdict would cost it every later prompt where it does fit.
	for _, m := range wikiMatches {
		recalled = append(recalled, m.Entry.Name)
	}
	// Trimmed before the link expansion, which is where the cap sat for both rails before the
	// judge existed — this keeps the procedural signal byte-identical to its previous behaviour.
	procMatches := linkedMatches(
		capMatches(findMatches(input.Prompt, procIndex, lens, recalled), maxUnjudgedResults), procIndex)
	for _, m := range procMatches {
		recalled = append(recalled, m.Entry.Name)
	}

	if len(wikiMatches) == 0 && len(procMatches) == 0 && len(railErrors) == 0 {
		fmt.Print("{}")
		os.Exit(0)
	}

	if input.SessionID != "" {
		saveCooldown(input.SessionID, recalled)
		// Fire ledgers feed the feedback loops: wiki fires are global (scanner dir),
		// procedural fires are per-project (<cwd>/.claude/), consumed at Stop by the
		// recall feedback reader and procedural_feedback.py respectively.
		if len(wikiMatches) > 0 {
			appendFireLog(input.SessionID, wikiMatches)
		}
		if len(procMatches) > 0 {
			appendProceduralFires(input.Cwd, input.SessionID, procMatches)
		}
	}

	var blocks []string
	// Failures lead: a blind rail is the most important thing on the prompt, and it
	// must not sit below matches that a half-working scanner happened to find.
	if len(railErrors) > 0 {
		blocks = append(blocks, railBrokenHeader+"\n  - "+strings.Join(railErrors, "\n  - "))
	}
	if len(wikiMatches) > 0 {
		header := recallHeader + uncheckedNote(outcome, budgetNote)
		if wikiJudged {
			header = recallHeader + recallJudgedNote
		}
		blocks = append(blocks, formatSignal(wikiMatches, header, corporaWikiPath(namespace)))
	}
	if len(procMatches) > 0 {
		blocks = append(blocks, formatSignal(procMatches, proceduralHeader, ".claude/"))
	}

	output := HookOutput{
		HookSpecificOutput: &HookSpecific{
			HookEventName:     "UserPromptSubmit",
			AdditionalContext: strings.Join(blocks, "\n\n"),
		},
	}
	outBytes, _ := json.Marshal(output)
	fmt.Print(string(outBytes))
}
