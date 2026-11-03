import { useEffect, useRef, useState } from "react";
import { Alert, Button, Checkbox, Input, Spin } from "antd";
import { Box, Copy, KeyRound } from "lucide-react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import {
  boxAccountsApi,
  connectBoxSession,
  detectBoxStatus,
  refreshBoxStatus,
  restoreBoxSession,
} from "../../api/modules/boxAccounts";
import { message } from "../../utils/antdMessage";
import { boxErrorMessage, boxErrorReason } from "./errors";
import styles from "./index.module.less";

type BoxStatus = NonNullable<Awaited<ReturnType<typeof detectBoxStatus>>>;

export default function BoxConnectPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const rotating = params.get("rotate") === "1";
  const [status, setStatus] = useState<BoxStatus | null>(null);
  const [initializing, setInitializing] = useState(true);
  const [authenticated, setAuthenticated] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [managementKey, setManagementKey] = useState("");
  // Rotation results live only in this page. Do not put the key in navigation
  // state or browser storage: it must survive the old session's revocation only.
  const [newKey, setNewKey] = useState("");
  const holdRotationPage = useRef(false);
  const [revokeClients, setRevokeClients] = useState(false);
  const [loadAttempt, setLoadAttempt] = useState(0);

  useEffect(() => {
    if (holdRotationPage.current) return;
    let active = true;
    setInitializing(true);
    setError(null);
    void (async () => {
      try {
        const next = await (loadAttempt
          ? refreshBoxStatus()
          : detectBoxStatus());
        if (!active) return;
        if (!next) {
          navigate("/login", { replace: true });
          return;
        }
        setStatus(next);
        if (next.state !== "claimed") return;
        const restored = await restoreBoxSession();
        if (!active || holdRotationPage.current) return;
        setAuthenticated(restored);
        if (restored && !rotating)
          navigate("/conversations", { replace: true });
      } catch (err) {
        if (active) setError(err);
      } finally {
        if (active) setInitializing(false);
      }
    })();
    return () => {
      active = false;
    };
  }, [navigate, loadAttempt, rotating]);

  const connect = async (key: string) => {
    if (!key.trim()) return;
    setBusy(true);
    setError(null);
    setManagementKey("");
    try {
      await connectBoxSession(key);
      if (rotating && !newKey) setAuthenticated(true);
      else {
        setNewKey("");
        navigate("/conversations", { replace: true });
      }
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  const rotate = async () => {
    holdRotationPage.current = true;
    setBusy(true);
    setError(null);
    try {
      const result = await boxAccountsApi.rotateManagementKey(revokeClients);
      setNewKey(result.management_key);
      setAuthenticated(false);
    } catch (err) {
      holdRotationPage.current = false;
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  const copyKey = async () => {
    try {
      await navigator.clipboard.writeText(newKey);
      message.success(t("boxAccount.copied"));
    } catch {
      message.error(t("boxAccount.copyFailed"));
    }
  };
  const recoveryRequired =
    status?.state === "recovery_required" ||
    boxErrorReason(error) === "recovery_required";

  return (
    <main className={styles.page}>
      <section className={styles.card} aria-labelledby="box-connect-title">
        <span className={styles.mark} aria-hidden="true">
          <Box size={28} />
        </span>
        <h1 id="box-connect-title" className={styles.title}>
          {t(rotating ? "boxAccount.rotateKey" : "boxAccount.connect")}
        </h1>
        <p className={styles.description}>
          {t("boxAccount.connectDescription")}
        </p>
        <code className={styles.origin}>{window.location.origin}</code>
        <div className={styles.stack}>
          {initializing ? (
            <Spin aria-label={t("common.loading")} />
          ) : (
            <>
              {error != null && (
                <Alert
                  type="error"
                  showIcon
                  message={boxErrorMessage(error, t)}
                />
              )}
              {!status ? (
                <Button onClick={() => setLoadAttempt((value) => value + 1)}>
                  {t("common.retry")}
                </Button>
              ) : recoveryRequired ? (
                <Alert
                  type="warning"
                  showIcon
                  message={t("boxAccount.recoveryRequired")}
                  description={t("boxAccount.recoveryHint")}
                />
              ) : status.state === "unclaimed" ? (
                <>
                  <Alert
                    type="info"
                    showIcon
                    message={t("boxAccount.initializeRequired")}
                    description={
                      <>
                        <p>{t("boxAccount.initializeHint")}</p>
                        <code>inhere-box box key init</code>
                        <p>{t("boxAccount.initializeLegacyHint")}</p>
                        <code>inhere-box box key init --upgrade-legacy</code>
                      </>
                    }
                  />
                  <Button onClick={() => setLoadAttempt((value) => value + 1)}>
                    {t("common.refresh")}
                  </Button>
                </>
              ) : newKey ? (
                <>
                  <Alert
                    type="success"
                    showIcon
                    message={t("boxAccount.keyRotated")}
                    description={t("boxAccount.saveNewKey")}
                  />
                  <label className={styles.field}>
                    {t("boxAccount.newManagementKey")}
                    <Input.TextArea
                      value={newKey}
                      readOnly
                      autoSize={{ minRows: 2, maxRows: 4 }}
                      onFocus={(event) => event.target.select()}
                    />
                  </label>
                  <Button
                    icon={<Copy size={16} />}
                    onClick={() => void copyKey()}
                  >
                    {t("boxAccount.copyKey")}
                  </Button>
                  <Button
                    type="primary"
                    loading={busy}
                    onClick={() => void connect(newKey)}
                  >
                    {t("boxAccount.loginWithNewKey")}
                  </Button>
                </>
              ) : rotating && authenticated ? (
                <>
                  <Alert
                    type="warning"
                    showIcon
                    message={t("boxAccount.rotateConfirm")}
                    description={t("boxAccount.rotateEffects")}
                  />
                  <Checkbox
                    checked={revokeClients}
                    onChange={(event) => setRevokeClients(event.target.checked)}
                  >
                    {t("boxAccount.revokeClientsOnRotate")}
                  </Checkbox>
                  <Button
                    type="primary"
                    danger
                    loading={busy}
                    onClick={() => void rotate()}
                  >
                    {t("boxAccount.rotateKey")}
                  </Button>
                  <Button
                    disabled={busy}
                    onClick={() => navigate("/box/devices")}
                  >
                    {t("common.cancel")}
                  </Button>
                </>
              ) : (
                <form
                  className={styles.stack}
                  onSubmit={(event) => {
                    event.preventDefault();
                    void connect(managementKey);
                  }}
                >
                  {rotating && (
                    <p className={styles.hint}>
                      {t("boxAccount.loginBeforeRotate")}
                    </p>
                  )}
                  <label className={styles.field}>
                    {t("boxAccount.managementKey")}
                    <Input.Password
                      value={managementKey}
                      onChange={(event) => setManagementKey(event.target.value)}
                      placeholder={t("boxAccount.managementKeyPlaceholder")}
                      autoComplete="off"
                      spellCheck={false}
                    />
                  </label>
                  <p className={styles.hint}>
                    {t("boxAccount.managementKeyHint")}
                  </p>
                  <Button
                    type="primary"
                    htmlType="submit"
                    icon={<KeyRound size={16} />}
                    loading={busy}
                    disabled={!managementKey.trim()}
                  >
                    {t("boxAccount.connect")}
                  </Button>
                  <p className={styles.hint}>
                    {t("boxAccount.webSessionHint")}
                  </p>
                </form>
              )}
            </>
          )}
        </div>
      </section>
    </main>
  );
}
