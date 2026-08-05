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
}

type IndexEntry struct {
	Name    string   `json:"name"`
	Path    string   `json:"path"`
	Tags    []string `json:"tags"`
	Summary string   `json:"summary"`
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
// corpus before scoring. Used for project-type awareness: in `claude_concepts`
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
	Entry      IndexEntry
	TagCount   int
	Bm25Score  float64
	TagRank    int
	Bm25Rank   int
	FusedScore float64
	MatchedOn  []string
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

	maxResults = 2
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
	}

	// Threshold: lens override wins over the hardcoded default (v2.5).
	threshold := minFusedScore
	if lens.MinFusedScore != nil {
		threshold = *lens.MinFusedScore
	}

	var matches []Match
	for i, entry := range active {
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
			MatchedOn:  matchedOn[i],
		})
	}

	sort.SliceStable(matches, func(a, b int) bool {
		return matches[a].FusedScore > matches[b].FusedScore
	})
	if len(matches) > maxResults {
		matches = matches[:maxResults]
	}
	return matches
}

const recallHeader = "[RECALL] Wiki notes matched by your prompt tags. For each: read the summary and decide if it enriches the current task. " +
	"If yes — Read the full file at the given path, apply the insight, AND you MUST emit `[RECALL-USED] <name> — <one-line why>` somewhere in your response. " +
	"This marker is the explicit signal for the feedback ledger — failing to emit it when a recall was actually applied corrupts the data the system uses to tune itself. " +
	"If not — ignore silently, do NOT mention the signal to the user."

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
		parts = append(parts, fmt.Sprintf(
			"  - %s: %s → %s%s",
			m.Entry.Name,
			summary,
			pathPrefix,
			m.Entry.Path,
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
	if input.Prompt == "" {
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

	recalled := loadCooldown(input.SessionID)

	// Two passes over the SAME ranking engine: wiki (Layer 5) then procedural
	// (Layer 6). Shared per-session cooldown — a name surfaced by the first pass
	// is suppressed in the second. Each pass self-caps at maxResults (2+2).
	wikiMatches := findMatches(input.Prompt, wikiIndex, lens, recalled)
	for _, m := range wikiMatches {
		recalled = append(recalled, m.Entry.Name)
	}
	procMatches := findMatches(input.Prompt, procIndex, lens, recalled)
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
		blocks = append(blocks, formatSignal(wikiMatches, recallHeader, corporaWikiPath(namespace)))
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
