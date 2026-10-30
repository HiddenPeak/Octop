import type { TFunction } from "i18next";
import { parseApiError } from "../../utils/apiError";

export function boxErrorReason(error: unknown): string | undefined {
  const reason = parseApiError(error)?.details?.reason;
  if (typeof reason === "string") return reason;
  if (error && typeof error === "object" && "reason" in error) {
    return typeof error.reason === "string" ? error.reason : undefined;
  }
  return error instanceof Error && /^[a-z_]+$/.test(error.message)
    ? error.message
    : undefined;
}

export function boxErrorMessage(error: unknown, t: TFunction): string {
  const reason = boxErrorReason(error);
  return reason
    ? t(`boxAccount.errors.${reason}`, {
        defaultValue: t("boxAccount.operationFailed"),
      })
    : t("boxAccount.operationFailed");
}
