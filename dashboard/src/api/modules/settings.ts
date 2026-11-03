import { request } from "../request";

export interface InhereBoxTimezoneSettings {
  timezone: string;
}

export interface InhereBoxUploadSettings {
  max_upload_mb: number;
  max_upload_bytes: number;
}

export interface InhereBoxHitlSettings {
  enabled: boolean;
  tool_guard_require_approval: boolean;
  show_approval_ui: boolean;
}

export interface InhereBoxCapabilitiesSettings {
  mobile: { enabled: boolean; backend: string };
}

export interface CaptchaPairView {
  site_key: string;
  has_secret: boolean;
  cam_secret_id?: string;
  has_cam_secret?: boolean;
}

export interface CaptchaSettings {
  active: string;
  available: string[];
  providers: Record<string, CaptchaPairView>;
  source: "settings" | "env";
  v3_min_score: number;
}

export type CaptchaPairBody = {
  site_key?: string;
  secret?: string;
  cam_secret_id?: string;
  cam_secret?: string;
} | null;

export interface CaptchaSettingsPut {
  active?: string;
  providers?: Record<string, CaptchaPairBody>;
}

export const inhereBoxSettingsApi = {
  timezone: () => request<InhereBoxTimezoneSettings>("/settings/timezone"),
  upload: () => request<InhereBoxUploadSettings>("/settings/upload"),
  hitl: () => request<InhereBoxHitlSettings>("/settings/hitl"),
  capabilities: () =>
    request<InhereBoxCapabilitiesSettings>("/settings/capabilities", {
      cache: "no-store",
    }),
  captcha: () => request<CaptchaSettings>("/settings/captcha"),
  putCaptcha: (body: CaptchaSettingsPut) =>
    request<CaptchaSettings>("/settings/captcha", {
      method: "PUT",
      body: JSON.stringify(body),
    }),
};
