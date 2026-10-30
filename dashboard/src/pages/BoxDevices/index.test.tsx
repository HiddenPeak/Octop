import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import BoxDevicesPage from ".";
const mocks = vi.hoisted(() => ({
  clients: vi.fn(),
  create: vi.fn(),
  primary: vi.fn(),
  revoke: vi.fn(),
  confirm: vi.fn(),
  navigate: vi.fn(),
  copy: vi.fn(),
}));
vi.mock("../../api/modules/boxAccounts", () => ({
  boxAccountsApi: {
    clients: mocks.clients,
    createShare: mocks.create,
    setPrimary: mocks.primary,
    revoke: mocks.revoke,
  },
}));
vi.mock("../../hooks/useServerTimezone", () => ({
  useServerTimezone: () => "Asia/Shanghai",
}));
vi.mock("react-router-dom", () => ({ useNavigate: () => mocks.navigate }));
vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));
vi.mock("../../utils/antdModal", () => ({ modal: { confirm: mocks.confirm } }));
vi.mock("../../utils/antdMessage", () => ({
  message: { success: vi.fn(), error: vi.fn() },
}));
const device = {
  client_id: "client-1",
  name: "Mac app",
  platform: "macOS",
  role: "primary",
  state: "active",
  created_at: 1,
  last_seen_at: 2,
  revoked_at: null,
};
const realGetComputedStyle = window.getComputedStyle;
beforeEach(() => {
  vi.clearAllMocks();
  vi.spyOn(window, "getComputedStyle").mockImplementation((element) =>
    realGetComputedStyle(element),
  );
  mocks.clients.mockResolvedValue([device]);
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText: mocks.copy },
  });
});
afterEach(() => vi.restoreAllMocks());
describe("BoxDevices native client management", () => {
  it("allows revoking the final device without ending web management", async () => {
    render(<BoxDevicesPage />);
    await screen.findByText("Mac app");
    expect(screen.getByText("boxAccount.primaryDevice")).toBeVisible();
    const revoke = screen.getByRole("button", { name: "boxAccount.revoke" });
    expect(revoke).toBeEnabled();
    fireEvent.click(revoke);
    expect(mocks.revoke).not.toHaveBeenCalled();
    await act(async () => mocks.confirm.mock.calls[0][0].onOk());
    expect(mocks.revoke).toHaveBeenCalledWith("client-1");
    expect(mocks.navigate).not.toHaveBeenCalled();
  });
  it("confirms primary reassignment for an invited client", async () => {
    mocks.clients.mockResolvedValue([{ ...device, role: "guest" }]);
    render(<BoxDevicesPage />);
    fireEvent.click(
      await screen.findByRole("button", { name: "boxAccount.setPrimary" }),
    );
    expect(mocks.confirm.mock.calls[0][0].content).toBe(
      "boxAccount.setPrimaryConfirm",
    );
    await act(async () => mocks.confirm.mock.calls[0][0].onOk());
    expect(mocks.primary).toHaveBeenCalledWith("client-1");
  });
  it("supports no primary or clients and explains independent management", async () => {
    mocks.clients.mockResolvedValue([]);
    render(<BoxDevicesPage />);
    await screen.findByText("boxAccount.noDevices");
    expect(screen.getByText("boxAccount.webIndependent")).toBeVisible();
    expect(
      screen.getByRole("button", { name: "boxAccount.shareAccess" }),
    ).toBeEnabled();
    expect(
      screen.queryByText("boxAccount.pendingRequests"),
    ).not.toBeInTheDocument();
  });
  it("shows and copies only the temporary code and removes it on closing", async () => {
    mocks.create.mockResolvedValue({
      secret: "one-use-code",
      expires_at: Math.floor(Date.now() / 1000) + 48 * 60 * 60,
    });
    render(<BoxDevicesPage />);
    await screen.findByText("Mac app");
    fireEvent.click(
      screen.getByRole("button", { name: "boxAccount.shareAccess" }),
    );
    expect(
      await screen.findByRole("textbox", { name: "boxAccount.shareCode" }),
    ).toHaveValue("one-use-code");
    expect(screen.getByRole("dialog")).toHaveTextContent(
      "boxAccount.shareInstructions",
    );
    fireEvent.click(
      screen.getByRole("button", { name: "boxAccount.copyShareCode" }),
    );
    await waitFor(() =>
      expect(mocks.copy).toHaveBeenCalledWith("one-use-code"),
    );
    fireEvent.click(screen.getByRole("button", { name: "common.close" }));
    await waitFor(() =>
      expect(
        screen.queryByDisplayValue("one-use-code"),
      ).not.toBeInTheDocument(),
    );
  });
  it("opens rotation on the independent connection page", async () => {
    render(<BoxDevicesPage />);
    await screen.findByText("Mac app");
    fireEvent.click(
      screen.getByRole("button", { name: "boxAccount.rotateKey" }),
    );
    expect(mocks.navigate).toHaveBeenCalledWith("/connect?rotate=1");
  });
});
