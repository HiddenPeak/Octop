import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import BoxConnectPage from ".";

const mocks = vi.hoisted(() => ({
  detect: vi.fn(),
  refresh: vi.fn(),
  restore: vi.fn(),
  connect: vi.fn(),
  rotate: vi.fn(),
  navigate: vi.fn(),
  copy: vi.fn(),
  params: new URLSearchParams(),
}));
vi.mock("../../api/modules/boxAccounts", () => ({
  detectBoxStatus: mocks.detect,
  refreshBoxStatus: mocks.refresh,
  restoreBoxSession: mocks.restore,
  connectBoxSession: mocks.connect,
  boxAccountsApi: { rotateManagementKey: mocks.rotate },
}));
vi.mock("react-router-dom", () => ({
  useNavigate: () => mocks.navigate,
  useSearchParams: () => [mocks.params],
}));
vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));
vi.mock("../../utils/antdMessage", () => ({
  message: { success: vi.fn(), error: vi.fn() },
}));
const status = {
  account_mode: "box_local",
  protocol_version: 1,
  box_id: "box-1",
  state: "claimed",
  pairing_available: true,
};
beforeEach(() => {
  vi.clearAllMocks();
  mocks.params = new URLSearchParams();
  mocks.detect.mockResolvedValue(status);
  mocks.refresh.mockResolvedValue(status);
  mocks.restore.mockResolvedValue(false);
  mocks.connect.mockResolvedValue(true);
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText: mocks.copy },
  });
});

describe("BoxConnect management login", () => {
  it("returns server installations to their legacy login", async () => {
    mocks.detect.mockResolvedValue(null);
    render(<BoxConnectPage />);
    await waitFor(() =>
      expect(mocks.navigate).toHaveBeenCalledWith("/login", { replace: true }),
    );
    expect(mocks.restore).not.toHaveBeenCalled();
  });
  it("restores the admin cookie without registering a browser device", async () => {
    mocks.restore.mockResolvedValue(true);
    render(<BoxConnectPage />);
    await waitFor(() =>
      expect(mocks.navigate).toHaveBeenCalledWith("/conversations", {
        replace: true,
      }),
    );
    expect(mocks.connect).not.toHaveBeenCalled();
  });
  it("accepts only a management key, clears the input, and connects", async () => {
    render(<BoxConnectPage />);
    fireEvent.change(await screen.findByLabelText("boxAccount.managementKey"), {
      target: { value: "long-lived-key" },
    });
    fireEvent.click(screen.getByRole("button", { name: "boxAccount.connect" }));
    await waitFor(() =>
      expect(mocks.connect).toHaveBeenCalledWith("long-lived-key"),
    );
    expect(screen.getByLabelText("boxAccount.managementKey")).toHaveValue("");
    expect(
      screen.queryByLabelText("boxAccount.deviceName"),
    ).not.toBeInTheDocument();
    expect(mocks.navigate).toHaveBeenCalledWith("/conversations", {
      replace: true,
    });
  });
  it("does not display a rejected key or raw network error", async () => {
    mocks.connect.mockRejectedValue(
      new Error("connection failed: secret-value"),
    );
    render(<BoxConnectPage />);
    fireEvent.change(await screen.findByLabelText("boxAccount.managementKey"), {
      target: { value: "secret-value" },
    });
    fireEvent.click(screen.getByRole("button", { name: "boxAccount.connect" }));
    await screen.findByText("boxAccount.operationFailed");
    expect(screen.getByLabelText("boxAccount.managementKey")).toHaveValue("");
    expect(document.body).not.toHaveTextContent("secret-value");
  });
  it("offers status retry without showing a login form on unavailable service", async () => {
    mocks.detect.mockRejectedValue(new TypeError("Failed to fetch"));
    render(<BoxConnectPage />);
    const retry = await screen.findByRole("button", { name: "common.retry" });
    expect(
      screen.queryByLabelText("boxAccount.managementKey"),
    ).not.toBeInTheDocument();
    fireEvent.click(retry);
    await screen.findByLabelText("boxAccount.managementKey");
    expect(mocks.refresh).toHaveBeenCalledOnce();
  });
  it.each([
    ["unclaimed", "boxAccount.initializeRequired"],
    ["recovery_required", "boxAccount.recoveryRequired"],
  ])(
    "keeps %s out of login until local maintenance completes",
    async (state, text) => {
      mocks.detect.mockResolvedValue({ ...status, state });
      render(<BoxConnectPage />);
      await screen.findByText(text);
      expect(mocks.restore).not.toHaveBeenCalled();
      expect(
        screen.queryByLabelText("boxAccount.managementKey"),
      ).not.toBeInTheDocument();
    },
  );
  it("keeps the rotated key on the public page, copies only that key, and logs in explicitly", async () => {
    mocks.params = new URLSearchParams("rotate=1");
    mocks.restore.mockResolvedValue(true);
    mocks.rotate.mockResolvedValue({ management_key: "new-key-once" });
    render(<BoxConnectPage />);
    fireEvent.click(
      await screen.findByRole("checkbox", {
        name: "boxAccount.revokeClientsOnRotate",
      }),
    );
    fireEvent.click(
      screen.getByRole("button", { name: "boxAccount.rotateKey" }),
    );
    expect(
      await screen.findByLabelText("boxAccount.newManagementKey"),
    ).toHaveValue("new-key-once");
    expect(mocks.rotate).toHaveBeenCalledWith(true);
    expect(mocks.navigate).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "boxAccount.copyKey" }));
    await waitFor(() =>
      expect(mocks.copy).toHaveBeenCalledWith("new-key-once"),
    );
    fireEvent.click(
      screen.getByRole("button", { name: "boxAccount.loginWithNewKey" }),
    );
    await waitFor(() =>
      expect(mocks.connect).toHaveBeenCalledWith("new-key-once"),
    );
    expect(mocks.navigate).toHaveBeenCalledWith("/conversations", {
      replace: true,
    });
  });
  it("requires login before rotation when the old cookie is unavailable", async () => {
    mocks.params = new URLSearchParams("rotate=1");
    render(<BoxConnectPage />);
    fireEvent.change(await screen.findByLabelText("boxAccount.managementKey"), {
      target: { value: "current-key" },
    });
    fireEvent.click(screen.getByRole("button", { name: "boxAccount.connect" }));
    await screen.findByRole("button", { name: "boxAccount.rotateKey" });
    expect(mocks.rotate).not.toHaveBeenCalled();
    expect(mocks.navigate).not.toHaveBeenCalled();
  });
});
