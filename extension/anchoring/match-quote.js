/* Ported from Hypothesis client src/annotator/anchoring/match-quote.ts
 * (https://github.com/hypothesis/client, BSD-2-Clause) — types stripped,
 * wired to the vendored approx-string-match global. The scoring model
 * (quote/prefix/suffix/position weights) is kept verbatim; do not tune it
 * here — re-port from upstream instead. Never hand-write text anchoring.
 */
"use strict";

(() => {
  const anchoring = (globalThis.inspegAnchoring = globalThis.inspegAnchoring || {});
  const approxSearch = anchoring.approxSearch;

  function search(text, str, maxErrors) {
    // Fast path for exact matches; the library doesn't do this itself.
    let matchPos = 0;
    const exactMatches = [];
    while (matchPos !== -1) {
      matchPos = text.indexOf(str, matchPos);
      if (matchPos !== -1) {
        exactMatches.push({ start: matchPos, end: matchPos + str.length, errors: 0 });
        matchPos += 1;
      }
    }
    if (exactMatches.length > 0) {
      return exactMatches;
    }
    return approxSearch(text, str, maxErrors);
  }

  function textMatchScore(text, str) {
    if (str.length === 0 || text.length === 0) {
      return 0.0;
    }
    const matches = search(text, str, str.length);
    return 1 - matches[0].errors / str.length;
  }

  /**
   * Find the best approximate match for `quote` in `text`.
   * `context` = {prefix?, suffix?, hint?}; returns {start, end, score} | null.
   */
  function matchQuote(text, quote, context = {}) {
    if (quote.length === 0) {
      return null;
    }
    const maxErrors = Math.min(256, quote.length / 2);
    const matches = search(text, quote, maxErrors);
    if (matches.length === 0) {
      return null;
    }

    const scoreMatch = (match) => {
      const quoteWeight = 50;
      const prefixWeight = 20;
      const suffixWeight = 20;
      const posWeight = 2;

      const quoteScore = 1 - match.errors / quote.length;
      const prefixScore = context.prefix
        ? textMatchScore(
            text.slice(Math.max(0, match.start - context.prefix.length), match.start),
            context.prefix,
          )
        : 1.0;
      const suffixScore = context.suffix
        ? textMatchScore(text.slice(match.end, match.end + context.suffix.length), context.suffix)
        : 1.0;

      let posScore = 1.0;
      if (typeof context.hint === "number") {
        const offset = Math.abs(match.start - context.hint);
        posScore = 1.0 - offset / text.length;
      }

      const rawScore =
        quoteWeight * quoteScore +
        prefixWeight * prefixScore +
        suffixWeight * suffixScore +
        posWeight * posScore;
      const maxScore = quoteWeight + prefixWeight + suffixWeight + posWeight;
      return rawScore / maxScore;
    };

    const scoredMatches = matches.map((m) => ({
      start: m.start,
      end: m.end,
      score: scoreMatch(m),
    }));
    scoredMatches.sort((a, b) => b.score - a.score);
    return scoredMatches[0];
  }

  anchoring.matchQuote = matchQuote;
})();
