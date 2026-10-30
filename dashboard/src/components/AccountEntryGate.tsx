import { useEffect, useState, type ReactNode } from "react";
import { Navigate } from "react-router-dom";
import { detectBoxStatus } from "../api/modules/boxAccounts";
import BootOfflinePanel from "./BootOfflinePanel";
import PageLoading from "./PageLoading";

/** Old bookmarks must not mount password/setup/SSO screens on a box. */
export default function AccountEntryGate({
  children,
}: {
  children: ReactNode;
}) {
  const [mode, setMode] = useState<"loading" | "box" | "server" | "offline">(
    "loading",
  );
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    let active = true;
    void detectBoxStatus().then(
      (box) => {
        if (active) setMode(box ? "box" : "server");
      },
      () => {
        if (active) setMode("offline");
      },
    );
    return () => {
      active = false;
    };
  }, [retry]);
  if (mode === "box") return <Navigate to="/connect" replace />;
  if (mode === "server") return children;
  if (mode === "offline")
    return (
      <BootOfflinePanel
        onRetry={() => {
          setMode("loading");
          setRetry((n) => n + 1);
        }}
      />
    );
  return <PageLoading />;
}
