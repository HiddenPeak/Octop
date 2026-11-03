import { afterEach, beforeEach, expect, it, vi } from "vitest";
vi.mock("../config", () => ({ getApiUrl: (path: string) => `/api${path}` }));
const status = {
  account_mode: "box_local",
  protocol_version: 1,
  auth_model_version: 2,
  management_key_configured: true,
  primary_client_id: null,
  box_id: "box-test",
  state: "claimed",
  pairing_available: true,
};
const json = (value: unknown, code = 200) =>
  new Response(JSON.stringify(value), {
    status: code,
    headers: { "Content-Type": "application/json" },
  });
const unauthorized = () =>
  json({ error: { details: { reason: "session_invalid" } } }, 401);
let cookie:
  | { session_id: string; expires_in: number; csrf_token: string }
  | undefined;
let fetchMock: ReturnType<typeof vi.fn>;
let logoutFails = false;
let serverMode = false;
let issue = 0;
beforeEach(() => {
  vi.resetModules();
  vi.useFakeTimers();
  localStorage.clear();
  sessionStorage.clear();
  cookie = undefined;
  logoutFails = false;
  serverMode = false;
  issue = 0;
  fetchMock = vi.fn(async (input: string | URL, init: RequestInit = {}) => {
    const path = new URL(String(input), location.origin).pathname;
    if (path === "/api/box/status")
      return serverMode ? json({}, 404) : json(status);
    if (path === "/api/auth/browser/session")
      return cookie ? json(cookie) : unauthorized();
    if (path === "/api/auth/browser/sessions") {
      const body = JSON.parse(String(init.body));
      if (
        body.management_key !== "admin-key" &&
        body.management_key !== "rotated-key"
      )
        return json(
          { error: { details: { reason: "management_key_invalid" } } },
          401,
        );
      cookie = {
        session_id: `web-${++issue}`,
        expires_in: 86400,
        csrf_token: `csrf-${issue}`,
      };
      return json(cookie);
    }
    if (path === "/api/auth/logout") {
      if (logoutFails) return json({}, 503);
      cookie = undefined;
      return new Response(null, { status: 204 });
    }
    if (path === "/api/box/management-key/rotate") {
      cookie = undefined;
      return json({ management_key: "rotated-key", auth_revision: 2 });
    }
    if (path === "/api/box/pairing-grants")
      return json({ secret: "short-code", expires_at: 9999999999 });
    if (path === "/api/private")
      return cookie ? json({ ok: true }) : unauthorized();
    return json({ ok: true });
  });
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => {
  vi.clearAllTimers();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});
const bodyOf = (path: string) =>
  JSON.parse(
    String(
      fetchMock.mock.calls.find(([url]) => String(url).endsWith(path))?.[1]
        ?.body,
    ),
  );
it("logs in with only a management key and never persists session secrets", async () => {
  const box = await import("./boxAccounts");
  expect(await box.connectBoxSession(" admin-key ")).toBe(true);
  expect(bodyOf("/auth/browser/sessions")).toEqual({
    management_key: "admin-key",
  });
  expect(box.boxSessionIdentity()).toBe("web-1");
  expect(localStorage.length).toBe(0);
  expect(sessionStorage.length).toBe(0);
  expect(
    fetchMock.mock.calls.some(([url]) =>
      /challenges|pairings|clients/.test(String(url)),
    ),
  ).toBe(false);
});
it("deletes a revoked registration with DELETE and current CSRF", async () => {
  const box = await import("./boxAccounts");
  await box.connectBoxSession("admin-key");
  fetchMock.mockImplementationOnce(async () => json(cookie));
  await box.boxAccountsApi.deleteClient("client/1");
  const call = fetchMock.mock.calls.find(([url]) =>
    String(url).endsWith("/box/clients/client%2F1"),
  )!;
  expect(call[1].method).toBe("DELETE");
  expect(new Headers(call[1].headers).get("X-Inhere-Box-CSRF")).toBe("csrf-1");
  expect(call[1].body).toBeUndefined();
});
it("restores an admin cookie without a key or paired client", async () => {
  cookie = {
    session_id: "existing-admin",
    expires_in: 1200,
    csrf_token: "existing-csrf",
  };
  const box = await import("./boxAccounts");
  expect(await box.restoreBoxSession()).toBe(true);
  expect(box.boxSessionIdentity()).toBe("existing-admin");
  expect(
    fetchMock.mock.calls.some(([url]) =>
      String(url).endsWith("/auth/browser/sessions"),
    ),
  ).toBe(false);
});
it("requires explicit login after logout and can use the same key again", async () => {
  const box = await import("./boxAccounts");
  await box.connectBoxSession("admin-key");
  await box.logoutBoxSession();
  expect(box.hasBoxSession()).toBe(false);
  expect(await box.restoreBoxSession()).toBe(false);
  expect(cookie).toBeUndefined();
  expect(localStorage.getItem("inhere-box:box:paused:box-test")).toBe("1");
  expect(await box.connectBoxSession("admin-key")).toBe(true);
  expect(box.boxSessionIdentity()).toBe("web-2");
});
it("retains the session when server logout fails", async () => {
  const box = await import("./boxAccounts");
  await box.connectBoxSession("admin-key");
  logoutFails = true;
  await expect(box.logoutBoxSession()).rejects.toMatchObject({ status: 503 });
  expect(box.hasBoxSession()).toBe(true);
  expect(box.isBoxSessionPaused()).toBe(false);
});
it("ends the 24-hour session without automatically logging in", async () => {
  const box = await import("./boxAccounts");
  await box.connectBoxSession("admin-key");
  const required = vi.fn();
  window.addEventListener(box.BOX_AUTH_REQUIRED_EVENT, required);
  cookie = undefined;
  await vi.advanceTimersByTimeAsync(86400 * 1000);
  expect(box.hasBoxSession()).toBe(false);
  expect(required).toHaveBeenCalledOnce();
  expect(
    fetchMock.mock.calls.filter(([url]) =>
      String(url).endsWith("/auth/browser/sessions"),
    ),
  ).toHaveLength(1);
  window.removeEventListener(box.BOX_AUTH_REQUIRED_EVENT, required);
});
it("reads current CSRF before mutation and removes bearer auth", async () => {
  const box = await import("./boxAccounts");
  await box.connectBoxSession("admin-key");
  cookie!.csrf_token = "fresh-csrf";
  await box.boxAuthenticatedFetch("/api/private", {
    method: "POST",
    headers: { Authorization: "Bearer old-jwt" },
    body: "{}",
  });
  const call = fetchMock.mock.calls.find(([url]) =>
    String(url).endsWith("/api/private"),
  )!;
  expect(new Headers(call[1].headers).get("X-Inhere-Box-CSRF")).toBe(
    "fresh-csrf",
  );
  expect(new Headers(call[1].headers).has("Authorization")).toBe(false);
  expect(call[1].credentials).toBe("same-origin");
});
it("does not replay a mutation when authorization is rejected", async () => {
  const box = await import("./boxAccounts");
  await box.connectBoxSession("admin-key");
  const required = vi.fn();
  window.addEventListener(box.BOX_AUTH_REQUIRED_EVENT, required);
  fetchMock
    .mockImplementationOnce(async () => json(cookie))
    .mockImplementationOnce(async () => unauthorized());
  const response = await box.boxAuthenticatedFetch("/api/private", {
    method: "POST",
    body: "{}",
  });
  expect(response.status).toBe(401);
  expect(box.hasBoxSession()).toBe(false);
  expect(required).toHaveBeenCalled();
  expect(
    fetchMock.mock.calls.filter(([url]) =>
      String(url).endsWith("/api/private"),
    ),
  ).toHaveLength(1);
  window.removeEventListener(box.BOX_AUTH_REQUIRED_EVENT, required);
});
it("issues each one-time share code with a unique request id", async () => {
  // Public HTTP does not expose crypto.randomUUID, but getRandomValues is available.
  vi.stubGlobal("crypto", {
    getRandomValues: crypto.getRandomValues.bind(crypto),
  });
  const box = await import("./boxAccounts");
  await box.connectBoxSession("admin-key");
  await box.boxAccountsApi.createShare();
  await box.boxAccountsApi.createShare();
  const bodies = fetchMock.mock.calls
    .filter(([url]) => String(url).endsWith("/box/pairing-grants"))
    .map(([, init]) => JSON.parse(String(init.body)));
  expect(Object.keys(bodies[0])).toEqual(["request_id"]);
  expect(bodies[0].request_id).not.toBe(bodies[1].request_id);
});
it("rotates the key without storing it and requires explicit login", async () => {
  const box = await import("./boxAccounts");
  await box.connectBoxSession("admin-key");
  expect(await box.boxAccountsApi.rotateManagementKey(false)).toMatchObject({
    management_key: "rotated-key",
  });
  expect(bodyOf("/box/management-key/rotate")).toEqual({
    revoke_clients: false,
  });
  expect(box.hasBoxSession()).toBe(false);
  expect(await box.restoreBoxSession()).toBe(false);
  expect(Object.values(localStorage).join()).not.toContain("rotated-key");
  expect(sessionStorage.length).toBe(0);
  expect(await box.connectBoxSession("rotated-key")).toBe(true);
});
it("manages clients using admin cookie endpoints without proofs", async () => {
  const box = await import("./boxAccounts");
  await box.connectBoxSession("admin-key");
  await box.boxAccountsApi.setOwner("client/1");
  await box.boxAccountsApi.revoke("client/1");
  expect(bodyOf("/box/clients/client%2F1/owner")).toEqual({});
  expect(bodyOf("/box/clients/client%2F1/revoke")).toEqual({});
});
it("preserves server-mode fetch options and bearer authentication", async () => {
  serverMode = true;
  const box = await import("./boxAccounts");
  const options = { headers: { Authorization: "Bearer legacy" } };
  await box.boxAuthenticatedFetch("/api/legacy", options);
  expect(fetchMock).toHaveBeenLastCalledWith("/api/legacy", options);
  expect(box.isBoxAccountMode()).toBe(false);
  expect(await box.restoreBoxSession()).toBe(false);
});
it("rejects authenticated requests to another origin", async () => {
  const box = await import("./boxAccounts");
  await box.connectBoxSession("admin-key");
  await expect(
    box.boxAuthenticatedFetch("https://other.example/api/private"),
  ).rejects.toMatchObject({ reason: "box_origin_mismatch" });
  expect(
    fetchMock.mock.calls.some(([url]) => String(url).includes("other.example")),
  ).toBe(false);
});
it("does not automatically reconnect revoked WebSocket sessions", async () => {
  class FakeSocket extends EventTarget {
    close = vi.fn();
  }
  vi.stubGlobal("WebSocket", FakeSocket);
  const box = await import("./boxAccounts");
  await box.connectBoxSession("admin-key");
  const reconnect = vi.fn();
  const socket = box.openAuthenticatedWebSocket(
    "ws://localhost/api/ws",
    reconnect,
  );
  cookie = undefined;
  socket.dispatchEvent(Object.assign(new Event("close"), { code: 4401 }));
  await vi.advanceTimersByTimeAsync(0);
  expect(box.hasBoxSession()).toBe(false);
  expect(reconnect).not.toHaveBeenCalled();
});

it("does not submit a management key to an older box authentication model", async () => {
  const box = await import("./boxAccounts");
  fetchMock.mockImplementationOnce(async () =>
    json({ ...status, auth_model_version: 1 }),
  );
  await expect(box.connectBoxSession("admin-key")).rejects.toMatchObject({
    reason: "protocol_mismatch",
  });
  expect(fetchMock).toHaveBeenCalledTimes(1);
});

it("refreshes stale tab metadata when another tab replaced the shared cookie", async () => {
  const box = await import("./boxAccounts");
  await box.connectBoxSession("admin-key");
  cookie = {
    session_id: "other-tab-session",
    expires_in: 3600,
    csrf_token: "other-tab-csrf",
  };
  await vi.advanceTimersByTimeAsync(86400 * 1000);
  expect(box.boxSessionIdentity()).toBe("other-tab-session");
  expect(box.hasBoxSession()).toBe(true);
  expect(
    fetchMock.mock.calls.filter(([url]) =>
      String(url).endsWith("/auth/browser/sessions"),
    ),
  ).toHaveLength(1);
});
it("discards in-memory authorization when another tab signs out", async () => {
  const box = await import("./boxAccounts");
  await box.connectBoxSession("admin-key");
  localStorage.setItem("inhere-box:box:paused:box-test", "1");
  window.dispatchEvent(
    new StorageEvent("storage", {
      key: "inhere-box:box:paused:box-test",
      newValue: "1",
    }),
  );
  expect(box.boxSessionIdentity()).toBe("");
  expect(await box.restoreBoxSession()).toBe(false);
});
