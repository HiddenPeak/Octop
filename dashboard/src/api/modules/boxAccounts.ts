import { getApiUrl } from "../config";
import { randomUUID } from "../../utils/randomUUID";

export class BoxAccountError extends Error {
  readonly reason: string;
  readonly status: number;
  constructor(reason: string, status = 0) {
    super(reason);
    this.name = "BoxAccountError";
    this.reason = reason;
    this.status = status;
  }
}

export interface BoxStatus {
  account_mode: "box_local";
  protocol_version: number;
  auth_model_version: 2;
  multiple_owner_devices?: boolean;
  management_key_configured: boolean;
  primary_client_id: string | null;
  box_id: string;
  state: "unclaimed" | "claimed" | "recovery_required";
  pairing_available: boolean;
}
export interface BoxClient {
  client_id: string;
  name: string;
  platform: string;
  role: "primary" | "guest" | "legacy";
  state: "active" | "revoked";
  created_at: number;
  last_seen_at: number | null;
  revoked_at: number | null;
}
export interface BoxShareCode {
  secret: string;
  expires_at: number;
}
export interface BoxAccount {
  box_id: string;
  user: { id: number; username: string; display_name: string | null };
  owner: { id: number; username: string; display_name: string | null } | null;
  role: "admin" | "primary" | "guest";
  session_kind: "admin" | "client";
  primary_client_id: string | null;
  owner_client_ids?: string[];
  recovery_key_confirmed: boolean;
  instance_id: string;
  account_id: string;
  auth_revision: number;
}
interface BrowserSession {
  session_id: string;
  expires_in: number;
  csrf_token: string;
}

export const BOX_SESSION_EVENT = "inhere-box:box-session";
export const BOX_AUTH_REQUIRED_EVENT = "inhere-box:box-auth-required";
let knownStatus: BoxStatus | null | undefined;
let statusRequest: Promise<BoxStatus | null> | undefined;
let session: BrowserSession | undefined;
let expiresAt = 0;
let expiryTimer: ReturnType<typeof setTimeout> | undefined;
let sessionRequest: Promise<boolean> | undefined;
let sessionGeneration = 0;

function sameOriginUrl(path: string): string {
  const url = new URL(getApiUrl(path), location.origin);
  if (url.origin !== location.origin)
    throw new BoxAccountError("box_origin_mismatch");
  return url.href;
}
async function responseJson<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new BoxAccountError(
      body?.error?.details?.reason ?? body?.details?.reason ?? "request_failed",
      response.status,
    );
  }
  return response.status === 204
    ? (undefined as T)
    : (response.json() as Promise<T>);
}
async function raw<T>(
  path: string,
  body?: unknown,
  extraHeaders: Record<string, string> = {},
): Promise<T> {
  return responseJson<T>(
    await fetch(sameOriginUrl(path), {
      method: body === undefined ? "GET" : "POST",
      credentials: "same-origin",
      cache: "no-store",
      headers:
        body === undefined
          ? undefined
          : { "Content-Type": "application/json", ...extraHeaders },
      body: body === undefined ? undefined : JSON.stringify(body),
    }),
  );
}
export function cachedBoxIdentity(): string {
  return knownStatus?.box_id ?? "";
}
export function isBoxAccountMode(): boolean {
  return knownStatus?.account_mode === "box_local";
}
export function detectBoxStatus(): Promise<BoxStatus | null> {
  if (knownStatus !== undefined) return Promise.resolve(knownStatus);
  if (!statusRequest)
    statusRequest = (async () => {
      const response = await fetch(getApiUrl("/box/status"), {
        credentials: "same-origin",
        cache: "no-store",
      });
      if (response.status === 404) return (knownStatus = null);
      const value = await responseJson<BoxStatus>(response);
      if (value.account_mode !== "box_local" || value.auth_model_version !== 2)
        throw new BoxAccountError("protocol_mismatch");
      sameOriginUrl("/box/status");
      localStorage.removeItem("auth_token");
      sessionStorage.removeItem("auth_token");
      return (knownStatus = value);
    })().finally(() => {
      statusRequest = undefined;
    });
  return statusRequest;
}
export async function refreshBoxStatus(): Promise<BoxStatus | null> {
  knownStatus = undefined;
  return detectBoxStatus();
}
function pauseKey(): string {
  return `inhere-box:box:paused:${knownStatus?.box_id ?? ""}`;
}
export function isBoxSessionPaused(): boolean {
  return isBoxAccountMode() && localStorage.getItem(pauseKey()) === "1";
}
export function hasBoxSession(): boolean {
  return (
    isBoxAccountMode() &&
    !isBoxSessionPaused() &&
    !!session &&
    expiresAt > Date.now()
  );
}
export function boxSessionIdentity(): string {
  return hasBoxSession() ? session!.session_id : "";
}
function clearSession(): void {
  sessionGeneration += 1;
  session = undefined;
  expiresAt = 0;
  clearTimeout(expiryTimer);
  window.dispatchEvent(new Event(BOX_SESSION_EVENT));
}
function authRequired(): void {
  clearSession();
  window.dispatchEvent(new Event(BOX_AUTH_REQUIRED_EVENT));
}
function acceptSession(value: BrowserSession): void {
  const changed = session?.session_id !== value.session_id;
  session = value;
  expiresAt = Date.now() + value.expires_in * 1000;
  clearTimeout(expiryTimer);
  // The management key is never retained to renew a session. The fixed cookie
  // lifetime is enforced by the server; expiry requires another explicit login.
  expiryTimer = setTimeout(
    () => {
      // Another tab may have replaced the shared cookie. Re-read its metadata
      // before expiring this tab; this never renews a cookie or reuses a key.
      void ensureSession(true).catch(() => authRequired());
    },
    Math.max(0, value.expires_in * 1000),
  );
  if (changed) window.dispatchEvent(new Event(BOX_SESSION_EVENT));
}
async function locked<T>(work: () => Promise<T>): Promise<T> {
  return navigator.locks
    ? navigator.locks.request("inhere-box-box-browser-session", work)
    : work();
}
async function ensureSession(forceCheck = false): Promise<boolean> {
  if (!(await detectBoxStatus()) || isBoxSessionPaused()) return false;
  if (!forceCheck && hasBoxSession()) return true;
  if (!sessionRequest)
    sessionRequest = locked(async () => {
      if (isBoxSessionPaused()) return false;
      const generation = sessionGeneration;
      try {
        const current = await raw<BrowserSession>("/auth/browser/session");
        if (generation !== sessionGeneration || isBoxSessionPaused())
          return false;
        acceptSession(current);
        return true;
      } catch (error) {
        if (error instanceof BoxAccountError && error.status === 401) {
          authRequired();
          return false;
        }
        throw error;
      }
    }).finally(() => {
      sessionRequest = undefined;
    });
  return sessionRequest;
}
export function restoreBoxSession(): Promise<boolean> {
  return ensureSession();
}
export async function connectBoxSession(
  managementKey: string,
): Promise<boolean> {
  if (!(await detectBoxStatus()))
    throw new BoxAccountError("box_mode_required");
  if (!managementKey.trim())
    throw new BoxAccountError("management_key_required");
  return locked(async () => {
    let csrfToken: string | undefined;
    try {
      csrfToken = (await raw<BrowserSession>("/auth/browser/session"))
        .csrf_token;
    } catch (error) {
      if (!(error instanceof BoxAccountError && error.status === 401))
        throw error;
    }
    const created = await raw<BrowserSession>(
      "/auth/browser/sessions",
      { management_key: managementKey.trim() },
      csrfToken ? { "X-Inhere-Box-CSRF": csrfToken } : {},
    );
    sessionGeneration += 1;
    localStorage.removeItem(pauseKey());
    acceptSession(created);
    return true;
  });
}

/** No automatic replay: mutating requests may already have been accepted. */
export async function runBoxRequest<T>(
  method: string | undefined,
  work: (headers: Record<string, string>) => Promise<T>,
): Promise<T> {
  if (!(await detectBoxStatus())) return work({});
  if (!(await ensureSession())) {
    authRequired();
    throw new BoxAccountError("session_required", 401);
  }
  const safe = ["GET", "HEAD", "OPTIONS"].includes(
    (method ?? "GET").toUpperCase(),
  );
  const run = async () => {
    if (isBoxSessionPaused())
      throw new BoxAccountError("session_required", 401);
    if (safe) return work({});
    try {
      const current = await raw<BrowserSession>("/auth/browser/session");
      acceptSession(current);
      return await work({ "X-Inhere-Box-CSRF": current.csrf_token });
    } catch (error) {
      if (error instanceof BoxAccountError && error.status === 401)
        authRequired();
      throw error;
    }
  };
  return safe ? run() : locked(run);
}
export async function boxRequestHeaders(
  method = "GET",
): Promise<Record<string, string>> {
  return runBoxRequest(method, async (headers) => headers);
}
export async function boxAuthenticatedFetch(
  url: string,
  options: RequestInit = {},
): Promise<Response> {
  await detectBoxStatus();
  if (!isBoxAccountMode()) return fetch(url, options);
  if (new URL(url, location.origin).origin !== location.origin)
    throw new BoxAccountError("box_origin_mismatch");
  return runBoxRequest(options.method, async (authHeaders) => {
    const headers = new Headers(options.headers);
    headers.delete("Authorization");
    for (const [name, value] of Object.entries(authHeaders))
      headers.set(name, value);
    const response = await fetch(url, {
      ...options,
      headers,
      credentials: "same-origin",
    });
    if (response.status === 401) authRequired();
    return response;
  });
}
async function protectedCall<T>(
  path: string,
  body?: unknown,
  method = body === undefined ? "GET" : "POST",
): Promise<T> {
  return responseJson<T>(
    await boxAuthenticatedFetch(sameOriginUrl(path), {
      method,
      headers: { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    }),
  );
}
export async function logoutBoxSession(): Promise<void> {
  if (!(await detectBoxStatus())) return;
  await locked(async () => {
    try {
      const current = await raw<BrowserSession>("/auth/browser/session");
      await raw<void>(
        "/auth/logout",
        {},
        { "X-Inhere-Box-CSRF": current.csrf_token },
      );
    } catch (error) {
      if (!(error instanceof BoxAccountError && error.status === 401))
        throw error;
    }
    localStorage.setItem(pauseKey(), "1");
    clearSession();
  });
}
export const boxAccountsApi = {
  account: () => protectedCall<BoxAccount>("/box/account"),
  clients: () => protectedCall<BoxClient[]>("/box/clients"),
  createShare: () =>
    protectedCall<BoxShareCode>("/box/pairing-grants", {
      request_id: randomUUID(),
    }),
  setOwner: (clientId: string) =>
    protectedCall<void>(
      `/box/clients/${encodeURIComponent(clientId)}/owner`,
      {},
    ),
  revoke: (clientId: string) =>
    protectedCall<void>(
      `/box/clients/${encodeURIComponent(clientId)}/revoke`,
      {},
    ),
  deleteClient: (clientId: string) =>
    protectedCall<void>(
      `/box/clients/${encodeURIComponent(clientId)}`,
      undefined,
      "DELETE",
    ),
  rotateManagementKey: async (
    revokeClients: boolean,
  ): Promise<{ management_key: string }> => {
    const result = await protectedCall<{ management_key: string }>(
      "/box/management-key/rotate",
      { revoke_clients: revokeClients },
    );
    localStorage.setItem(pauseKey(), "1");
    clearSession();
    return result;
  },
};

window.addEventListener("storage", (event) => {
  if (isBoxAccountMode() && event.key === pauseKey() && isBoxSessionPaused())
    clearSession();
});

window.addEventListener("focus", () => {
  if (isBoxAccountMode() && session && !isBoxSessionPaused()) {
    // A temporary network error should not discard an otherwise valid cookie.
    void ensureSession(true).catch(() => {});
  }
});

/** Cookie-authenticated sockets close when the current web session changes. */
export function openAuthenticatedWebSocket(
  url: string,
  reconnect?: () => void,
): WebSocket {
  if (isBoxAccountMode() && !hasBoxSession())
    throw new BoxAccountError("session_required", 401);
  const socket = new WebSocket(url);
  if (isBoxAccountMode()) {
    const identity = boxSessionIdentity();
    let reconnectAfterClose = false;
    const changed = () => {
      if (boxSessionIdentity() !== identity) {
        reconnectAfterClose = hasBoxSession();
        socket.close(1000, "session_changed");
      }
    };
    window.addEventListener(BOX_SESSION_EVENT, changed);
    socket.addEventListener(
      "close",
      (event) => {
        window.removeEventListener(BOX_SESSION_EVENT, changed);
        if (event.code === 4401 && !isBoxSessionPaused()) {
          void ensureSession(true)
            .then((active) => {
              if (active) reconnect?.();
            })
            .catch(() => authRequired());
        } else if (reconnectAfterClose && hasBoxSession()) reconnect?.();
      },
      { once: true },
    );
  }
  return socket;
}
