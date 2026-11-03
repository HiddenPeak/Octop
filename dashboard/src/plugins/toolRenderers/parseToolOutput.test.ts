import { describe, expect, it } from "vitest";
import {
  mergePatchedToolOutput,
  parseInhereBoxToolOutput,
  resolvePluginUiData,
} from "./parseToolOutput";
import {
  clearToolRenderers,
  getToolRendererVersion,
  registerToolRenderer,
  resolveToolRenderer,
} from "./registry";
import type { ToolRenderProps } from "./types";

function Dummy(_: ToolRenderProps) {
  return null;
}

describe("parseInhereBoxToolOutput", () => {
  it.each([false, true])(
    "reads historical UI envelopes (JSON string: %s)",
    (asString) => {
      const legacy = {
        octop_ui: { renderer: "existing-plugin" },
        data: { value: 42 },
      };
      const parsed = parseInhereBoxToolOutput(
        asString ? JSON.stringify(legacy) : legacy,
      );
      expect(parsed.inhereBoxUi?.renderer).toBe("existing-plugin");
      expect(parsed.data).toEqual({ value: 42 });
      expect(
        parseInhereBoxToolOutput({
          ...legacy,
          inhere_box_ui: { renderer: "current-plugin" },
        }).inhereBoxUi?.renderer,
      ).toBe("current-plugin");
    },
  );

  it("parses inhere_box_ui envelope", () => {
    const raw = JSON.stringify({
      inhere_box_ui: { renderer: "demo_card", version: 1 },
      data: { count: 2 },
      text: "hi",
    });
    const parsed = parseInhereBoxToolOutput(raw);
    expect(parsed.isJson).toBe(true);
    expect(parsed.inhereBoxUi).toEqual({ renderer: "demo_card", version: 1 });
    expect(parsed.data).toEqual({ count: 2 });
    expect(parsed.text).toBe("hi");
  });

  it("keeps plain text", () => {
    const parsed = parseInhereBoxToolOutput("hello");
    expect(parsed.isJson).toBe(false);
    expect(parsed.text).toBe("hello");
  });
});

describe("mergePatchedToolOutput", () => {
  it("merges data into existing envelope", () => {
    const prev = JSON.stringify({
      inhere_box_ui: { renderer: "demo_card" },
      data: { count: 1 },
      text: "x",
    });
    const next = mergePatchedToolOutput(prev, { count: 3 });
    expect(JSON.parse(next)).toEqual({
      inhere_box_ui: { renderer: "demo_card" },
      data: { count: 3 },
      text: "x",
    });
  });
});

describe("resolvePluginUiData", () => {
  const artifact = { results: [{ season_id: 1 }] };

  it("resolves data_ref envelopes from the artifact", () => {
    const parsed = parseInhereBoxToolOutput(
      JSON.stringify({
        inhere_box_ui: { renderer: "bilibili_player", version: 1 },
        data_ref: "artifact",
        text: "找到 1 部番剧。",
      }),
    );
    expect(parsed.data).toBeUndefined();
    expect(resolvePluginUiData(parsed, artifact, undefined)).toBe(artifact);
  });

  it("explicit data wins over data_ref/artifact (patched state)", () => {
    const patched = mergePatchedToolOutput(
      JSON.stringify({
        inhere_box_ui: { renderer: "bilibili_player" },
        data_ref: "artifact",
      }),
      { results: [], current_episode: 5 },
    );
    const parsed = parseInhereBoxToolOutput(patched);
    const data = resolvePluginUiData(parsed, artifact, undefined) as {
      current_episode: number;
    };
    expect(data.current_episode).toBe(5);
  });

  it("falls back to legacy shapes without data_ref", () => {
    const inline = parseInhereBoxToolOutput(
      JSON.stringify({
        inhere_box_ui: { renderer: "demo_card" },
        data: { count: 2 },
      }),
    );
    expect(resolvePluginUiData(inline, artifact, undefined)).toEqual({
      count: 2,
    });

    const plainJson = parseInhereBoxToolOutput(JSON.stringify({ foo: 1 }));
    expect(resolvePluginUiData(plainJson, artifact, undefined)).toEqual({
      foo: 1,
    });

    const text = parseInhereBoxToolOutput("hello");
    expect(resolvePluginUiData(text, artifact, "hello")).toBe("hello");
  });

  it("keeps the slim envelope when artifact is missing", () => {
    const parsed = parseInhereBoxToolOutput(
      JSON.stringify({
        inhere_box_ui: { renderer: "bilibili_player" },
        data_ref: "artifact",
      }),
    );
    const resolved = resolvePluginUiData(parsed, undefined, undefined) as {
      data_ref: string;
    };
    expect(resolved.data_ref).toBe("artifact");
  });
});

describe("resolveToolRenderer", () => {
  it("matches by inhere_box_ui.renderer then tool name", () => {
    clearToolRenderers();
    registerToolRenderer({
      id: "demo_card",
      pluginId: "demo-ui-card",
      tools: ["demo_ui_card"],
      component: Dummy,
    });
    const byHint = resolveToolRenderer({
      toolName: "other",
      pluginId: "demo-ui-card",
      parsed: parseInhereBoxToolOutput(
        JSON.stringify({ inhere_box_ui: { renderer: "demo_card" }, data: {} }),
      ),
    });
    expect(byHint?.id).toBe("demo_card");
    const byTool = resolveToolRenderer({
      toolName: "demo_ui_card",
      parsed: parseInhereBoxToolOutput("plain"),
    });
    expect(byTool?.id).toBe("demo_card");
    clearToolRenderers();
  });

  it("bumps version on register so subscribers can refresh", () => {
    clearToolRenderers();
    const before = getToolRendererVersion();
    registerToolRenderer({
      id: "x",
      pluginId: "p",
      component: Dummy,
    });
    expect(getToolRendererVersion()).toBeGreaterThan(before);
    clearToolRenderers();
  });
});
