import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import {
  MemoryRouter,
  Route,
  Routes,
  useLocation,
  useNavigate,
} from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";
import BoxConnectPage from ".";
import { useUnauthorizedRedirect } from "../../hooks/useUnauthorizedRedirect";

const mocks = vi.hoisted(() => ({ restore: vi.fn(), rotate: vi.fn() }));
vi.mock("../../api/modules/boxAccounts", () => ({
  detectBoxStatus: async () => ({
    account_mode: "box_local",
    state: "claimed",
  }),
  refreshBoxStatus: async () => ({
    account_mode: "box_local",
    state: "claimed",
  }),
  isBoxAccountMode: () => true,
  restoreBoxSession: mocks.restore,
  connectBoxSession: vi.fn(),
  boxAccountsApi: { rotateManagementKey: mocks.rotate },
}));
vi.mock("../../api/request", () => ({
  UNAUTHORIZED_EVENT: "octop:unauthorized",
}));
vi.mock("../../utils/antdMessage", () => ({
  message: { success: vi.fn(), error: vi.fn() },
}));
function Harness() {
  useUnauthorizedRedirect();
  const location = useLocation();
  const navigate = useNavigate();
  return (
    <>
      <output data-testid="location">
        {location.pathname}
        {location.search}
      </output>
      <button onClick={() => navigate("/connect")}>plain route</button>
      <Routes>
        <Route path="/connect" element={<BoxConnectPage />} />
        <Route path="/conversations" element={<div>private dashboard</div>} />
      </Routes>
    </>
  );
}
beforeEach(() => {
  vi.clearAllMocks();
  mocks.restore.mockResolvedValue(true);
});
it("keeps an in-flight rotation and its new key through background 401s and connection-route updates", async () => {
  let resolveRotation: (result: { management_key: string }) => void = () => {};
  mocks.rotate.mockReturnValue(
    new Promise((resolve) => {
      resolveRotation = resolve;
    }),
  );
  render(
    <MemoryRouter initialEntries={["/connect?rotate=1"]}>
      <Harness />
    </MemoryRouter>,
  );
  fireEvent.click(
    await screen.findByRole("button", { name: "boxAccount.rotateKey" }),
  );
  await act(async () => {
    window.dispatchEvent(new Event("octop:unauthorized", { cancelable: true }));
  });
  expect(screen.getByTestId("location")).toHaveTextContent("/connect?rotate=1");
  await act(async () => {
    resolveRotation({ management_key: "one-time-rotation-result" });
  });
  expect(
    await screen.findByLabelText("boxAccount.newManagementKey"),
  ).toHaveValue("one-time-rotation-result");
  await act(async () => {
    window.dispatchEvent(new Event("octop:unauthorized", { cancelable: true }));
  });
  // Even if another event changes the query, restoring a valid shared cookie
  // must not auto-navigate away from the still-visible one-time result.
  fireEvent.click(screen.getByRole("button", { name: "plain route" }));
  await waitFor(() =>
    expect(screen.getByTestId("location")).toHaveTextContent("/connect"),
  );
  expect(screen.getByLabelText("boxAccount.newManagementKey")).toHaveValue(
    "one-time-rotation-result",
  );
  expect(screen.queryByText("private dashboard")).not.toBeInTheDocument();
  expect(mocks.restore).toHaveBeenCalledOnce();
});
