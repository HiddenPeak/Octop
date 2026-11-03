import { act, renderHook } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { useProjectList } from "./useProjectLists";
import { residentStorageKey } from "../Resident/useResidentConversation";

const state = vi.hoisted(() => ({ boxId: "", sync: vi.fn() }));
vi.mock("../../api/modules/boxAccounts", () => ({
  cachedBoxIdentity: () => state.boxId,
}));
vi.mock("../../api/config", () => ({
  getApiUrl: (path: string) => `/api${path}`,
}));
vi.mock("../../i18n", () => ({ default: { language: "zh" } }));
vi.mock("../../hooks/useCurrentUser", () => ({
  useCurrentUser: () => ({ id: 1, username: "owner" }),
}));
vi.mock("../../context/TaskHostContext", () => ({
  useTaskHost: () => ({
    hostID: null,
    identity: "local-identity",
    projectsApi: { sync: state.sync },
  }),
}));

beforeEach(() => {
  sessionStorage.clear();
  state.boxId = "";
  state.sync.mockReset();
});
it("isolates persisted project lists when the same origin and owner belong to a new box", async () => {
  state.boxId = "box-a";
  const projectA = {
    project_id: "project-a",
    revision: 1,
    updated_at: "2026-10-30T00:00:00Z",
  };
  state.sync.mockResolvedValue({
    mode: "snapshot",
    changes: [{ operation: "upsert", data: projectA }],
    next_sync_cursor: "cursor-a",
    next_page_cursor: null,
  });
  const first = renderHook(() => useProjectList());
  await act(() => first.result.current.refresh());
  expect(first.result.current.items).toEqual([projectA]);
  first.unmount();

  state.boxId = "box-b";
  const second = renderHook(() => useProjectList());
  expect(second.result.current.items).toEqual([]);
  second.unmount();

  state.boxId = "box-a";
  const original = renderHook(() => useProjectList());
  expect(original.result.current.items).toEqual([projectA]);
});
it("preserves the existing server-mode cache key", () => {
  const project = { project_id: "legacy-project" };
  sessionStorage.setItem(
    "inhere-box:project-lists:v3:/api/:1:owner:local:local-identity:projects",
    JSON.stringify({ items: [project], cursor: "legacy-cursor", page: null }),
  );
  const hook = renderHook(() => useProjectList());
  expect(hook.result.current.items).toEqual([project]);
});
it("isolates resident drafts and pending requests without changing server keys", () => {
  const scope = { agent_id: "resident-agent", thread_id: "resident-thread" };
  const legacy = residentStorageKey(1, scope);
  expect(legacy).toBe(
    'inhere_box.resident.v1:["1","resident-agent","resident-thread"]',
  );
  state.boxId = "box-a";
  const first = residentStorageKey(1, scope);
  state.boxId = "box-b";
  expect(residentStorageKey(1, scope)).not.toBe(first);
  expect(residentStorageKey(1, scope)).not.toBe(legacy);
});
