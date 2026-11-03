import { describe, expect, it, vi, beforeEach } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useNavigate } from "react-router-dom";
import { useEffect } from "react";
import AuthGuard from "./AuthGuard";
import AccountEntryGate from "./AccountEntryGate";

vi.mock("../api/modules/boxAccounts", () => ({
  BOX_AUTH_REQUIRED_EVENT: "inhere-box:box-auth-required",
  BOX_SESSION_EVENT: "inhere-box:box-session",
  isBoxSessionPaused: vi.fn(() => false),
  detectBoxStatus: vi.fn(),
  restoreBoxSession: vi.fn(),
}));

vi.mock("../api/modules/auth", () => ({
  authApi: {
    getAuthStatus: vi.fn(),
    me: vi.fn(),
  },
}));

vi.mock("../api/request", () => ({
  getAuthToken: vi.fn(() => null),
  clearAuthToken: vi.fn(),
}));

vi.mock("../utils/locale", () => ({
  applyUserLocale: vi.fn(() => Promise.resolve()),
}));

import { authApi } from "../api/modules/auth";
import { getAuthToken } from "../api/request";
import { detectBoxStatus, restoreBoxSession } from "../api/modules/boxAccounts";

describe("AuthGuard offline boot", () => {
  beforeEach(() => {
    vi.mocked(detectBoxStatus).mockReset().mockResolvedValue(null);
    vi.mocked(restoreBoxSession).mockReset().mockResolvedValue(false);
    vi.mocked(authApi.getAuthStatus).mockReset();
    vi.mocked(authApi.me).mockReset();
    vi.mocked(getAuthToken).mockReset();
    vi.mocked(getAuthToken).mockReturnValue(null);
  });

  it("restores an admin cookie with no primary device and skips legacy setup and bearer tokens", async () => {
    vi.mocked(detectBoxStatus).mockResolvedValue({
      state: "claimed",
      primary_client_id: null,
    } as never);
    vi.mocked(restoreBoxSession).mockResolvedValue(true);
    vi.mocked(authApi.me).mockResolvedValue({ id: 1, locale: "zh" } as never);
    render(
      <MemoryRouter>
        <AuthGuard>
          <div>box-shell</div>
        </AuthGuard>
      </MemoryRouter>,
    );
    expect(await screen.findByText("box-shell")).toBeInTheDocument();
    expect(authApi.getAuthStatus).not.toHaveBeenCalled();
    expect(getAuthToken).not.toHaveBeenCalled();
  });

  it.each(["unclaimed", "recovery_required", "claimed"])(
    "routes a %s box without a browser session to the connection page",
    async (state) => {
      vi.mocked(detectBoxStatus).mockResolvedValue({ state } as never);
      render(
        <MemoryRouter initialEntries={["/"]}>
          <Routes>
            <Route
              path="/"
              element={
                <AuthGuard>
                  <div>private-data</div>
                </AuthGuard>
              }
            />
            <Route path="/connect" element={<div>box-connect</div>} />
          </Routes>
        </MemoryRouter>,
      );
      expect(await screen.findByText("box-connect")).toBeInTheDocument();
      expect(screen.queryByText("private-data")).not.toBeInTheDocument();
      expect(authApi.getAuthStatus).not.toHaveBeenCalled();
      expect(authApi.me).not.toHaveBeenCalled();
    },
  );

  it("does not fall back to password login when detecting account mode fails", async () => {
    vi.mocked(detectBoxStatus).mockRejectedValue(
      new TypeError("Failed to fetch"),
    );
    render(
      <MemoryRouter>
        <AuthGuard>
          <div>private-data</div>
        </AuthGuard>
      </MemoryRouter>,
    );
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(authApi.getAuthStatus).not.toHaveBeenCalled();
    expect(screen.queryByText("private-data")).not.toBeInTheDocument();
  });

  it("unmounts private data when the browser session is revoked", async () => {
    vi.mocked(detectBoxStatus).mockResolvedValue({ state: "claimed" } as never);
    vi.mocked(restoreBoxSession).mockResolvedValue(true);
    vi.mocked(authApi.me).mockResolvedValue({ id: 1, locale: "zh" } as never);
    render(
      <MemoryRouter initialEntries={["/"]}>
        <Routes>
          <Route
            path="/"
            element={
              <AuthGuard>
                <div>private-data</div>
              </AuthGuard>
            }
          />
          <Route path="/connect" element={<div>box-connect</div>} />
        </Routes>
      </MemoryRouter>,
    );
    await screen.findByText("private-data");
    act(() => {
      window.dispatchEvent(new Event("inhere-box:box-auth-required"));
    });
    expect(await screen.findByText("box-connect")).toBeInTheDocument();
    expect(screen.queryByText("private-data")).not.toBeInTheDocument();
  });

  it("redirects old password/setup bookmarks before mounting their forms", async () => {
    vi.mocked(detectBoxStatus).mockResolvedValue({ state: "claimed" } as never);
    render(
      <MemoryRouter initialEntries={["/setup"]}>
        <Routes>
          <Route
            path="/setup"
            element={
              <AccountEntryGate>
                <div>old-password-form</div>
              </AccountEntryGate>
            }
          />
          <Route path="/connect" element={<div>box-connect</div>} />
        </Routes>
      </MemoryRouter>,
    );
    expect(await screen.findByText("box-connect")).toBeInTheDocument();
    expect(screen.queryByText("old-password-form")).not.toBeInTheDocument();
  });

  it("shows offline panel instead of the protected shell when setup/status fails", async () => {
    vi.mocked(authApi.getAuthStatus).mockRejectedValue(
      new TypeError("Failed to fetch"),
    );

    render(
      <MemoryRouter>
        <AuthGuard>
          <div>protected-shell</div>
        </AuthGuard>
      </MemoryRouter>,
    );

    const alert = await screen.findByRole("alert");
    expect(alert).toBeInTheDocument();
    expect(alert.textContent).toMatch(
      /errors\.offlineTitle|Cannot reach|无法连接/,
    );
    expect(
      screen.getByRole("button", { name: /errors\.retry|Retry|重试/ }),
    ).toBeInTheDocument();
    expect(screen.queryByText("protected-shell")).not.toBeInTheDocument();
  });

  it("keeps the authenticated shell mounted across in-app navigations", async () => {
    vi.mocked(getAuthToken).mockReturnValue("tok");
    vi.mocked(authApi.getAuthStatus).mockResolvedValue({
      setup_required: false,
      has_admin: true,
    } as never);
    vi.mocked(authApi.me).mockResolvedValue({
      user_id: 1,
      username: "admin",
      role: "admin",
      locale: "zh",
    } as never);

    function NavProbe() {
      const navigate = useNavigate();
      useEffect(() => {
        navigate("/b");
      }, [navigate]);
      return <div>protected-shell</div>;
    }

    render(
      <MemoryRouter initialEntries={["/a"]}>
        <AuthGuard>
          <Routes>
            <Route path="/a" element={<NavProbe />} />
            <Route path="/b" element={<div>protected-shell</div>} />
          </Routes>
        </AuthGuard>
      </MemoryRouter>,
    );

    expect(await screen.findByText("protected-shell")).toBeInTheDocument();
    await waitFor(() => {
      expect(authApi.getAuthStatus).toHaveBeenCalledTimes(1);
    });
    // Give route-driven navigate identity churn a tick; gate must not re-run.
    await waitFor(() => {
      expect(screen.getByText("protected-shell")).toBeInTheDocument();
    });
    expect(authApi.getAuthStatus).toHaveBeenCalledTimes(1);
    expect(authApi.me).toHaveBeenCalledTimes(1);
  });

  it("retries auth from the offline panel without leaving a blank shell", async () => {
    vi.mocked(authApi.getAuthStatus)
      .mockRejectedValueOnce(new TypeError("Failed to fetch"))
      .mockResolvedValue({
        setup_required: false,
        has_admin: true,
      } as never);
    vi.mocked(getAuthToken).mockReturnValue("tok");
    vi.mocked(authApi.me).mockResolvedValue({
      user_id: 1,
      username: "admin",
      role: "admin",
      locale: "zh",
    } as never);

    const user = userEvent.setup();
    render(
      <MemoryRouter>
        <AuthGuard>
          <div>protected-shell</div>
        </AuthGuard>
      </MemoryRouter>,
    );

    const retry = await screen.findByRole("button", {
      name: /errors\.retry|Retry|重试/,
    });
    await user.click(retry);
    expect(await screen.findByText("protected-shell")).toBeInTheDocument();
  });
});
