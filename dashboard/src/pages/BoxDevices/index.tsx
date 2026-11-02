import { useCallback, useEffect, useRef, useState } from "react";
import { Alert, Button, Dropdown, Input, Modal, Spin, Tooltip } from "antd";
import {
  ChevronRight,
  Copy,
  Ellipsis,
  KeyRound,
  Laptop,
  LaptopMinimalCheck,
  Monitor,
  Plus,
  RefreshCw,
  ShieldCheck,
  Smartphone,
  Unlink,
} from "lucide-react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";
import {
  boxAccountsApi,
  type BoxClient,
  type BoxShareCode,
} from "../../api/modules/boxAccounts";
import { useServerTimezone } from "../../hooks/useServerTimezone";
import { formatServerDateTime } from "../../utils/formatMessageTime";
import { message } from "../../utils/antdMessage";
import { modal } from "../../utils/antdModal";
import { boxErrorMessage } from "../BoxConnect/errors";
import styles from "./index.module.less";

export default function BoxDevicesPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const timezone = useServerTimezone();
  const [clients, setClients] = useState<BoxClient[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [creating, setCreating] = useState(false);
  const [busyId, setBusyId] = useState<string>();
  const [error, setError] = useState<unknown>(null);
  const [share, setShare] = useState<BoxShareCode | null>(null);
  const [now, setNow] = useState(Date.now());
  const active = useRef(true);
  const fetching = useRef(false);
  const refresh = useCallback(async () => {
    if (fetching.current) return;
    fetching.current = true;
    setRefreshing(true);
    try {
      const next = await boxAccountsApi.clients();
      if (active.current) {
        setClients(next);
        setError(null);
      }
    } catch (err) {
      if (active.current) setError(err);
    } finally {
      fetching.current = false;
      if (active.current) {
        setLoading(false);
        setRefreshing(false);
      }
    }
  }, []);
  useEffect(() => {
    active.current = true;
    void refresh();
    const timer = setInterval(() => {
      if (document.visibilityState !== "hidden") void refresh();
    }, 15000);
    return () => {
      active.current = false;
      clearInterval(timer);
    };
  }, [refresh]);
  useEffect(() => {
    if (!share) return;
    setNow(Date.now());
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [share]);

  const createShare = async () => {
    setCreating(true);
    try {
      setShare(await boxAccountsApi.createShare());
      setNow(Date.now());
    } catch (err) {
      message.error(boxErrorMessage(err, t));
    } finally {
      setCreating(false);
    }
  };
  const changeClient = (client: BoxClient, primary: boolean) => {
    modal.confirm({
      title: t(
        primary ? "boxAccount.setOwnerTitle" : "boxAccount.revokeTitle",
        { name: client.name },
      ),
      content: t(
        primary ? "boxAccount.setOwnerConfirm" : "boxAccount.revokeConfirm",
      ),
      okText: t(primary ? "boxAccount.setOwner" : "boxAccount.revoke"),
      okButtonProps: { danger: !primary },
      cancelText: t("common.cancel"),
      onOk: async () => {
        setBusyId(client.client_id);
        try {
          if (primary) await boxAccountsApi.setOwner(client.client_id);
          else await boxAccountsApi.revoke(client.client_id);
          message.success(
            t(primary ? "boxAccount.ownerAdded" : "boxAccount.revokedSuccess"),
          );
          await refresh();
        } catch (err) {
          message.error(boxErrorMessage(err, t));
          throw err;
        } finally {
          setBusyId(undefined);
        }
      },
    });
  };
  const copyShare = async () => {
    if (!share) return;
    try {
      await navigator.clipboard.writeText(share.secret);
      message.success(t("boxAccount.copied"));
    } catch {
      message.error(t("boxAccount.copyFailed"));
    }
  };
  const expired = Boolean(share && share.expires_at * 1000 <= now);
  const displayTime = (value: number | null) =>
    value ? formatServerDateTime(value, timezone) : t("boxAccount.neverActive");
  const ownerClients = clients.filter(
    (client) => client.state === "active" && client.role === "primary",
  );
  const otherClients = clients
    .filter((client) => client.state === "active" && client.role !== "primary")
    .sort((a, b) => (b.last_seen_at ?? 0) - (a.last_seen_at ?? 0));
  const revokedClients = clients
    .filter((client) => client.state === "revoked")
    .sort((a, b) => (b.revoked_at ?? 0) - (a.revoked_at ?? 0));
  const platformName = (platform: string) => {
    switch (platform.toLowerCase()) {
      case "macos":
        return "macOS";
      case "ios":
        return "iOS";
      case "android":
        return "Android";
      case "windows":
        return "Windows";
      case "linux":
        return "Linux";
      case "browser":
        return t("boxAccount.webBrowser");
      default:
        return platform;
    }
  };
  const deviceIcon = (platform: string) =>
    /ios|android|mobile/i.test(platform) ? (
      <Smartphone size={21} />
    ) : /browser/i.test(platform) ? (
      <Monitor size={21} />
    ) : (
      <Laptop size={21} />
    );
  const renderDevice = (client: BoxClient, primary = false) => (
    <article
      className={styles.device}
      key={client.client_id}
      aria-label={client.name}
    >
      <span className={styles.deviceIcon} aria-hidden="true">
        {deviceIcon(client.platform)}
      </span>
      <div className={styles.deviceDetails}>
        <h3 className={styles.deviceName} title={client.name}>
          {client.name}
        </h3>
        <p className={styles.meta}>
          {platformName(client.platform)}
          <span className={styles.separator} aria-hidden="true">
            ·
          </span>
          {t("boxAccount.lastActive", {
            time: displayTime(client.last_seen_at),
          })}
        </p>
      </div>
      <Dropdown
        trigger={["click"]}
        placement="bottomRight"
        menu={{
          items: [
            ...(!primary
              ? [
                  {
                    key: "primary",
                    icon: <LaptopMinimalCheck size={16} />,
                    label: t("boxAccount.setOwner"),
                  },
                ]
              : []),
            {
              key: "revoke",
              icon: <Unlink size={16} />,
              label: t("boxAccount.revoke"),
              danger: true,
            },
          ],
          onClick: ({ key }) => changeClient(client, key === "primary"),
        }}
        disabled={busyId != null}
      >
        <Button
          className={styles.menuButton}
          type="text"
          icon={<Ellipsis size={20} />}
          loading={busyId === client.client_id}
          disabled={busyId != null}
          aria-label={t("boxAccount.deviceActions", { name: client.name })}
        />
      </Dropdown>
    </article>
  );

  return (
    <div className={styles.scrollArea}>
      <div className={styles.page}>
        <header className={styles.header}>
          <div>
            <h1 className={styles.title}>{t("boxAccount.devices")}</h1>
            <p className={styles.description}>
              {t("boxAccount.devicesSubtitle")}
            </p>
          </div>
          <div className={styles.actions}>
            <Tooltip title={t("common.refresh")}>
              <Button
                className={styles.refreshButton}
                type="text"
                icon={<RefreshCw size={17} />}
                loading={refreshing}
                onClick={() => void refresh()}
                aria-label={t("common.refresh")}
              />
            </Tooltip>
            <Button
              type="primary"
              icon={<Plus size={16} />}
              loading={creating}
              onClick={() => void createShare()}
            >
              {t("boxAccount.shareAccess")}
            </Button>
          </div>
        </header>
        {error != null && (
          <Alert type="error" showIcon message={boxErrorMessage(error, t)} />
        )}
        {loading ? (
          <div className={styles.center}>
            <Spin />
          </div>
        ) : (
          <div className={styles.content}>
            <section
              className={styles.primaryCard}
              aria-labelledby="box-primary-heading"
            >
              <div className={styles.primaryHeader}>
                <h2 id="box-primary-heading" className={styles.primaryTitle}>
                  <LaptopMinimalCheck size={17} aria-hidden="true" />
                  {t("boxAccount.ownerDevice")}
                </h2>
                <span className={styles.count}>{ownerClients.length}</span>
              </div>
              {ownerClients.length > 0 ? (
                ownerClients.map((client) => renderDevice(client, true))
              ) : (
                <div className={styles.emptyPrimary}>
                  <span className={styles.emptyIcon} aria-hidden="true">
                    <Laptop size={28} />
                  </span>
                  <h3>
                    {t(
                      clients.length === 0
                        ? "boxAccount.noDevices"
                        : "boxAccount.noOwnerDevices",
                    )}
                  </h3>
                  <p className={styles.description}>
                    {t("boxAccount.noOwnerDevicesHint")}
                  </p>
                </div>
              )}
              <p className={styles.primaryHint}>
                {t("boxAccount.ownerAccessHint")}
              </p>
            </section>
            <section aria-labelledby="box-clients-heading">
              <div className={styles.sectionHeader}>
                <div className={styles.sectionLabel}>
                  <h2 id="box-clients-heading" className={styles.sectionTitle}>
                    {t("boxAccount.guestDevice")}
                  </h2>
                  <span className={styles.count}>{otherClients.length}</span>
                </div>
                <p className={styles.description}>
                  {t("boxAccount.otherClientsHint")}
                </p>
              </div>
              <div className={styles.list}>
                {otherClients.length > 0 ? (
                  otherClients.map((client) => renderDevice(client))
                ) : (
                  <p className={styles.emptyList}>
                    {t("boxAccount.noGuestClients")}
                  </p>
                )}
              </div>
            </section>
            {revokedClients.length > 0 && (
              <details className={styles.history}>
                <summary>
                  <ChevronRight size={15} aria-hidden="true" />
                  <span>{t("boxAccount.revokedDevices")}</span>
                  <span className={styles.count}>{revokedClients.length}</span>
                </summary>
                <ul className={styles.historyList}>
                  {revokedClients.map((client) => (
                    <li key={client.client_id} className={styles.historyRow}>
                      <span className={styles.historyName} title={client.name}>
                        {client.name}
                      </span>
                      <span className={styles.meta}>
                        {platformName(client.platform)}
                        <span className={styles.separator} aria-hidden="true">
                          ·
                        </span>
                        {t("boxAccount.lastActive", {
                          time: displayTime(client.last_seen_at),
                        })}
                      </span>
                    </li>
                  ))}
                </ul>
              </details>
            )}
          </div>
        )}
        <section className={styles.keyCard}>
          <span className={styles.keyIcon} aria-hidden="true">
            <KeyRound size={20} />
          </span>
          <div className={styles.keyDetails}>
            <h2 className={styles.sectionTitle}>
              {t("boxAccount.managementKey")}
            </h2>
            <p className={styles.description}>
              {t("boxAccount.managementSecurityHint")}
            </p>
          </div>
          <Button onClick={() => navigate("/connect?rotate=1")}>
            {t("boxAccount.rotateKey")}
          </Button>
        </section>
        <p className={styles.footnote}>
          <ShieldCheck size={15} aria-hidden="true" />
          <span>{t("boxAccount.webIndependent")}</span>
        </p>
        <Modal
          open={share != null}
          title={t("boxAccount.shareAccess")}
          onCancel={() => setShare(null)}
          destroyOnHidden
          footer={
            <Button onClick={() => setShare(null)}>{t("common.close")}</Button>
          }
        >
          {share && (
            <div className={styles.modalContent}>
              <Alert
                type={expired ? "warning" : "info"}
                showIcon
                message={t(
                  expired
                    ? "boxAccount.shareExpired"
                    : "boxAccount.shareInstructions",
                )}
                description={
                  !expired ? t("boxAccount.shareSecurityHint") : undefined
                }
              />
              <p className={styles.meta}>{t("boxAccount.guestAccessHint")}</p>
              <Input.TextArea
                value={share.secret}
                readOnly
                autoSize={{ minRows: 2, maxRows: 4 }}
                aria-label={t("boxAccount.shareCode")}
                onFocus={(event) => event.target.select()}
              />
              <p className={styles.meta}>
                {t("boxAccount.expiresAt", {
                  time: displayTime(share.expires_at),
                })}
              </p>
              <Button
                type="primary"
                icon={<Copy size={16} />}
                disabled={expired}
                onClick={() => void copyShare()}
              >
                {t("boxAccount.copyShareCode")}
              </Button>
            </div>
          )}
        </Modal>
      </div>
    </div>
  );
}
