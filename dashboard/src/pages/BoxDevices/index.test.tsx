import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import BoxDevicesPage from ".";
const mocks = vi.hoisted(() => ({
  clients: vi.fn(),
  create: vi.fn(),
  primary: vi.fn(),
  revoke: vi.fn(),
  deleteClient: vi.fn(),
  confirm: vi.fn(),
  navigate: vi.fn(),
  copy: vi.fn(),
}));
vi.mock("../../api/modules/boxAccounts", () => ({
  boxAccountsApi: {
    clients: mocks.clients,
    createShare: mocks.create,
    setOwner: mocks.primary,
    revoke: mocks.revoke,
    deleteClient: mocks.deleteClient,
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
  it("confirms the device ID before deleting only a revoked registration", async () => {
    mocks.clients
      .mockResolvedValueOnce([
        device,
        { ...device, client_id: "revoked-1", state: "revoked" },
      ])
      .mockResolvedValueOnce([device]);
    render(<BoxDevicesPage />);
    await screen.findByText("revoked-1");
    expect(
      within(
        screen.getByRole("region", { name: "boxAccount.ownerDevice" }),
      ).queryByRole("button", { name: "boxAccount.deleteDeviceTitle" }),
    ).not.toBeInTheDocument();
    fireEvent.click(screen.getByText("boxAccount.revokedDevices"));
    fireEvent.click(
      screen.getByRole("button", { name: "boxAccount.deleteDeviceTitle" }),
    );
    expect(mocks.deleteClient).not.toHaveBeenCalled();
    const confirmation = mocks.confirm.mock.calls[0][0];
    const preview = render(confirmation.content);
    expect(preview.container).toHaveTextContent("revoked-1");
    expect(preview.container).toHaveTextContent(
      "boxAccount.deleteDeviceConfirm",
    );
    preview.unmount();
    await act(async () => confirmation.onOk());
    expect(mocks.deleteClient).toHaveBeenCalledWith("revoked-1");
    expect(screen.queryByText("revoked-1")).not.toBeInTheDocument();
    expect(screen.getByText("client-1")).toBeVisible();
  });
  it("allows revoking the final device without ending web management", async () => {
    render(<BoxDevicesPage />);
    await screen.findByText("Mac app");
    expect(screen.getByText("boxAccount.ownerDevice")).toBeVisible();
    fireEvent.click(
      screen.getByRole("button", { name: "boxAccount.deviceActions" }),
    );
    const revoke = await screen.findByRole("menuitem", {
      name: "boxAccount.revoke",
    });
    expect(revoke).toBeEnabled();
    fireEvent.click(revoke);
    expect(mocks.revoke).not.toHaveBeenCalled();
    await act(async () => mocks.confirm.mock.calls[0][0].onOk());
    expect(mocks.revoke).toHaveBeenCalledWith("client-1");
    expect(mocks.navigate).not.toHaveBeenCalled();
  });
  it("confirms granting owner access to an invited client", async () => {
    mocks.clients.mockResolvedValue([{ ...device, role: "guest" }]);
    render(<BoxDevicesPage />);
    fireEvent.click(
      await screen.findByRole("button", { name: "boxAccount.deviceActions" }),
    );
    fireEvent.click(
      await screen.findByRole("menuitem", { name: "boxAccount.setOwner" }),
    );
    expect(mocks.confirm.mock.calls[0][0].content).toBe(
      "boxAccount.setOwnerConfirm",
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
  it("keeps all owner devices visible and revoked devices out of active groups", async () => {
    mocks.clients.mockResolvedValue([
      device,
      { ...device, client_id: "owner-2", name: "My phone", platform: "iOS" },
      { ...device, client_id: "guest-1", name: "Guest laptop", role: "guest" },
      {
        ...device,
        client_id: "revoked-1",
        name: "Old laptop",
        state: "revoked",
      },
    ]);
    render(<BoxDevicesPage />);
    await screen.findByText("Mac app");
    const owners = screen.getByRole("region", {
      name: "boxAccount.ownerDevice",
    });
    expect(within(owners).getByText("Mac app")).toBeVisible();
    expect(within(owners).getByText("My phone")).toBeVisible();
    expect(within(owners).queryByText("Old laptop")).not.toBeInTheDocument();
    const guests = screen.getByRole("region", {
      name: "boxAccount.guestDevice",
    });
    expect(within(guests).getByText("Guest laptop")).toBeVisible();
    const history = screen
      .getByText("boxAccount.revokedDevices")
      .closest("details")!;
    expect(history).not.toHaveAttribute("open");
    expect(
      within(history).queryByRole("button", {
        name: "boxAccount.deviceActions",
      }),
    ).not.toBeInTheDocument();
    fireEvent.click(screen.getByText("boxAccount.revokedDevices"));
    expect(history).toHaveAttribute("open");
    expect(screen.getByText("Old laptop")).toBeVisible();
    fireEvent.click(within(owners).getAllByRole("button")[1]);
    await waitFor(() =>
      expect(
        screen.getByRole("menuitem", { name: "boxAccount.revoke" }),
      ).toBeVisible(),
    );
    expect(
      screen.queryByRole("menuitem", { name: "boxAccount.setOwner" }),
    ).not.toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("menuitem", { name: "boxAccount.revoke" }),
    );
    await act(async () => mocks.confirm.mock.calls[0][0].onOk());
    expect(mocks.revoke).toHaveBeenCalledWith("owner-2");
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
