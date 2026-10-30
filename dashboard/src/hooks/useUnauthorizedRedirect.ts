import { useEffect } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { UNAUTHORIZED_EVENT } from "../api/request";
import { isBoxAccountMode } from "../api/modules/boxAccounts";

/**
 * Route an expired session to /login through the router.
 *
 * Claiming the event (``preventDefault``) keeps ``request.ts`` from doing a
 * full-page navigation, which would abort every in-flight lazy chunk and leave
 * the user staring at a blank page.
 */
export function useUnauthorizedRedirect(): void {
  const navigate = useNavigate();
  const { pathname } = useLocation();

  useEffect(() => {
    const handler = (event: Event) => {
      event.preventDefault();
      // Keep rotation results and their route state on the public connection
      // page when an older background request reports session revocation.
      if (isBoxAccountMode() && pathname === "/connect") return;
      navigate(isBoxAccountMode() ? "/connect" : "/login", { replace: true });
    };
    window.addEventListener(UNAUTHORIZED_EVENT, handler);
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, handler);
  }, [navigate, pathname]);
}
