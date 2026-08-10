/* Mechanical text <-> DOM Range mapping. This is NOT the anchoring
 * algorithm (that is the vendored match-quote/approx-string-match): it is
 * only the bookkeeping that (a) extracts the page text the daemon stores as
 * the Document artifact, and (b) converts character offsets in that exact
 * text to/from DOM Ranges. Both directions MUST walk text nodes identically
 * or every stored offset is garbage.
 */
"use strict";

(() => {
  const anchoring = (globalThis.inspegAnchoring = globalThis.inspegAnchoring || {});

  const SKIP_TAGS = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE"]);

  function textWalker(root) {
    return document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode(node) {
        const parent = node.parentElement;
        if (parent && SKIP_TAGS.has(parent.tagName)) {
          return NodeFilter.FILTER_REJECT;
        }
        return NodeFilter.FILTER_ACCEPT;
      },
    });
  }

  /** The page text as stored in the Document artifact: concatenated data of
   * every visible-ish text node, no separators added or removed. */
  function pageText(root = document.body) {
    let text = "";
    const walker = textWalker(root);
    while (walker.nextNode()) {
      text += walker.currentNode.data;
    }
    return text;
  }

  /** Char offsets of a DOM Range's boundaries within pageText(root). */
  function rangeToTextPosition(range, root = document.body) {
    let acc = 0;
    let start = null;
    let end = null;
    const walker = textWalker(root);
    while (walker.nextNode()) {
      const node = walker.currentNode;
      if (node === range.startContainer) {
        start = acc + range.startOffset;
      }
      if (node === range.endContainer) {
        end = acc + range.endOffset;
        break;
      }
      acc += node.data.length;
    }
    // Element-node boundaries (triple-click selections): fall back to
    // comparing positions instead of identity.
    if (start === null || end === null) {
      acc = 0;
      start = null;
      end = null;
      const walker2 = textWalker(root);
      while (walker2.nextNode()) {
        const node = walker2.currentNode;
        const nodeRange = document.createRange();
        nodeRange.selectNodeContents(node);
        if (start === null && range.compareBoundaryPoints(Range.START_TO_START, nodeRange) <= 0) {
          start = acc;
        }
        if (range.compareBoundaryPoints(Range.END_TO_END, nodeRange) >= 0) {
          end = acc + node.data.length;
        }
        acc += node.data.length;
      }
    }
    if (start === null || end === null || end < start) {
      return null;
    }
    return { start, end };
  }

  /** DOM Range for [start, end) char offsets within pageText(root). */
  function textPositionToRange(start, end, root = document.body) {
    const range = document.createRange();
    let acc = 0;
    let startSet = false;
    const walker = textWalker(root);
    while (walker.nextNode()) {
      const node = walker.currentNode;
      const next = acc + node.data.length;
      if (!startSet && start >= acc && start <= next) {
        range.setStart(node, start - acc);
        startSet = true;
      }
      if (startSet && end >= acc && end <= next) {
        range.setEnd(node, end - acc);
        return range;
      }
      acc = next;
    }
    return null;
  }

  anchoring.pageText = pageText;
  anchoring.rangeToTextPosition = rangeToTextPosition;
  anchoring.textPositionToRange = textPositionToRange;
})();
