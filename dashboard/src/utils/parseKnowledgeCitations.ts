/**
 * Parse knowledge-citation markers embedded in search_knowledge tool output.
 */

export type KnowledgeCitation = {
  kbId: string;
  kbName: string;
  docId: string;
  filename: string;
  /** Workspace-relative path inside the KB when present (new markers). */
  path?: string;
};

const CITATIONS_PREFIX_RE = /<!--(?:inhere-box|octop)-kb-citations:/g;
const CITATIONS_MARKER_RE =
  /^<!--(inhere-box|octop)-kb-citations:(\[[\s\S]*\])-->\s*$/;

function trailingMarker(text: string): {
  start: number;
  current: boolean;
  payload: unknown[];
} | null {
  // Work from the last prefix, but allow marker-like text inside JSON strings.
  const prefixes = Array.from(text.matchAll(CITATIONS_PREFIX_RE));
  for (const prefix of prefixes.reverse()) {
    const start = prefix.index!;
    const match = text.slice(start).match(CITATIONS_MARKER_RE);
    if (!match) continue;
    try {
      const payload: unknown = JSON.parse(match[2]);
      if (Array.isArray(payload)) {
        return { start, current: match[1] === "inhere-box", payload };
      }
    } catch {
      // Invalid/embedded comments are ordinary text, not citation metadata.
    }
  }
  return null;
}

function asCitation(raw: unknown): KnowledgeCitation | null {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const row = raw as Record<string, unknown>;
  const docId = String(row.doc_id ?? row.docId ?? "").trim();
  if (!docId) return null;
  const path = String(row.path ?? "").trim();
  return {
    kbId: String(row.kb_id ?? row.kbId ?? "").trim(),
    kbName: String(row.kb_name ?? row.kbName ?? "").trim(),
    docId,
    filename: String(row.filename ?? "").trim() || docId,
    ...(path ? { path } : {}),
  };
}

/** Split tool output into display text + structured citations (if present). */
export function parseKnowledgeCitations(rawOutput: string | undefined): {
  text: string;
  citations: KnowledgeCitation[];
} {
  const raw = rawOutput ?? "";
  if (!raw) return { text: "", citations: [] };
  let text = raw;
  let parsed: unknown[] | undefined;
  let current = false;
  for (
    let marker = trailingMarker(text);
    marker;
    marker = trailingMarker(text)
  ) {
    if (parsed === undefined || (marker.current && !current)) {
      parsed = marker.payload;
      current = marker.current;
    }
    text = text.slice(0, marker.start).replace(/\s+$/, "");
  }
  if (parsed === undefined) return { text: raw, citations: [] };
  const citations: KnowledgeCitation[] = [];
  const seen = new Set<string>();
  for (const item of parsed) {
    const citation = asCitation(item);
    if (!citation || seen.has(citation.docId)) continue;
    seen.add(citation.docId);
    citations.push(citation);
  }
  return { text, citations };
}
