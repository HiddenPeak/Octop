import { parseKnowledgeCitations } from "./parseKnowledgeCitations";

describe("parseKnowledgeCitations", () => {
  it("returns plain text when no marker", () => {
    expect(parseKnowledgeCitations("hello")).toEqual({
      text: "hello",
      citations: [],
    });
  });

  it("extracts citations and strips the marker", () => {
    const raw =
      'passages\n\n<!--inhere-box-kb-citations:[{"kb_id":"kb1","kb_name":"Docs","doc_id":"d1","filename":"a.md"}]-->';
    expect(parseKnowledgeCitations(raw)).toEqual({
      text: "passages",
      citations: [
        {
          kbId: "kb1",
          kbName: "Docs",
          docId: "d1",
          filename: "a.md",
        },
      ],
    });
  });

  it("dedupes by doc id", () => {
    const raw =
      'x<!--inhere-box-kb-citations:[{"doc_id":"d1","filename":"a.md"},{"doc_id":"d1","filename":"a.md"}]-->';
    expect(parseKnowledgeCitations(raw).citations).toHaveLength(1);
  });

  it("parses optional path", () => {
    const raw =
      'x<!--inhere-box-kb-citations:[{"kb_id":"kb1","kb_name":"Docs","doc_id":"d1","filename":"a.md","path":"notes/a.md"}]-->';
    expect(parseKnowledgeCitations(raw).citations[0]).toMatchObject({
      path: "notes/a.md",
    });
  });

  it("omits path when absent", () => {
    const raw =
      'x<!--inhere-box-kb-citations:[{"doc_id":"d1","filename":"a.md"}]-->';
    expect(parseKnowledgeCitations(raw).citations[0].path).toBeUndefined();
  });

  it("reads the persisted legacy marker into the current citation shape", () => {
    const raw =
      'passages\n\n<!--octop-kb-citations:[{"kb_id":"kb1","kb_name":"Docs","doc_id":"d1","filename":"a.md","path":"notes/a.md"}]-->';
    expect(parseKnowledgeCitations(raw)).toEqual({
      text: "passages",
      citations: [
        {
          kbId: "kb1",
          kbName: "Docs",
          docId: "d1",
          filename: "a.md",
          path: "notes/a.md",
        },
      ],
    });
    expect(raw).toContain("<!--octop-kb-citations:");
  });

  it.each([false, true])(
    "prefers the current marker when both suffixes exist (legacy last: %s)",
    (legacyLast) => {
      const oldMarker = '<!--octop-kb-citations:[{"doc_id":"old"}]-->';
      const newMarker = '<!--inhere-box-kb-citations:[{"doc_id":"new"}]-->';
      const markers = legacyLast
        ? [newMarker, oldMarker]
        : [oldMarker, newMarker];
      const parsed = parseKnowledgeCitations(`passages\n${markers.join("\n")}`);
      expect(parsed.text).toBe("passages");
      expect(parsed.citations.map((item) => item.docId)).toEqual(["new"]);
    },
  );

  it.each([
    "octop and inhere-box are mentioned in ordinary prose.  ",
    '<!--octop-kb-citations:[{"doc_id":"d1"}]--> followed by prose',
    "```html\n<!--octop-kb-citations:[]-->\n```",
    '<!--unrelated-kb-citations:[{"doc_id":"d1"}]-->',
    "<!--octop-kb-citations:[invalid JSON]-->",
    '<!--inhere-box-kb-citations:{"doc_id":"d1"}-->',
    '<!--octop-kb-citations:[{"doc_id":"d1"}]-->\n<!--inhere-box-kb-citations:[invalid]-->',
  ])("preserves prose and invalid or nonterminal markers: %s", (raw) => {
    expect(parseKnowledgeCitations(raw)).toEqual({ text: raw, citations: [] });
  });

  it("does not mistake marker text inside a filename for another envelope", () => {
    const filename = "<!--octop-kb-citations:example.md";
    const raw = `passages\n<!--inhere-box-kb-citations:${JSON.stringify([
      { doc_id: "d1", filename },
    ])}-->`;
    expect(parseKnowledgeCitations(raw).citations[0].filename).toBe(filename);
  });
});
