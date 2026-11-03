import { request } from "../request";
import type {
  InhereBoxCronRow,
  InhereBoxCronCreateBody,
  InhereBoxCronPatchBody,
} from "../types";

export interface InhereBoxCronSettings {
  timezone: string;
}

export interface CronTaskExamples {
  zh?: string[];
  en?: string[];
}

export const inhereBoxCronApi = {
  settings: () => request<InhereBoxCronSettings>("/settings/timezone"),

  list: () => request<InhereBoxCronRow[]>("/cron", { cache: "no-store" }),

  create: (body: InhereBoxCronCreateBody) =>
    request<InhereBoxCronRow>("/cron", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  get: (cronId: string) =>
    request<InhereBoxCronRow>(`/cron/${encodeURIComponent(cronId)}`),

  patch: (cronId: string, body: InhereBoxCronPatchBody) =>
    request<InhereBoxCronRow>(`/cron/${encodeURIComponent(cronId)}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),

  delete: (cronId: string) =>
    request<void>(`/cron/${encodeURIComponent(cronId)}`, { method: "DELETE" }),

  runNow: (cronId: string) =>
    request<void>(`/cron/${encodeURIComponent(cronId)}/run-now`, {
      method: "POST",
    }),
};
