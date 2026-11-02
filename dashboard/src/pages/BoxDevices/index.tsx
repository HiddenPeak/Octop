import { useCallback, useEffect, useRef, useState } from "react";
import { Alert, Button, Empty, Input, Modal, Spin, Tag } from "antd";
import {
  Copy,
  KeyRound,
  Monitor,
  Plus,
  RefreshCw,
  Smartphone,
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
  const visible = [...clients].sort(
    (a, b) => Number(b.role === "primary") - Number(a.role === "primary"),
  );

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <div>
          <h1 className={styles.title}>{t("boxAccount.devices")}</h1>
          <p className={styles.description}>
            {t("boxAccount.devicesDescription")}
          </p>
        </div>
        <div className={styles.actions}>
          <Button
            icon={<RefreshCw size={16} />}
            loading={refreshing}
            onClick={() => void refresh()}
          >
            {t("common.refresh")}
          </Button>
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
        <section className={styles.section}>
          {visible.length === 0 ? (
            <Empty description={t("boxAccount.noDevices")} />
          ) : (
            <div className={styles.list}>
              {visible.map((client) => (
                <article className={styles.device} key={client.client_id}>
                  <span className={styles.icon} aria-hidden="true">
                    {/ios|android|mobile/i.test(client.platform) ? (
                      <Smartphone size={20} />
                    ) : (
                      <Monitor size={20} />
                    )}
                  </span>
                  <div className={styles.details}>
                    <div className={styles.name}>
                      {client.name}
                      <Tag
                        color={
                          client.state === "revoked"
                            ? "default"
                            : client.role === "primary"
                            ? "blue"
                            : "green"
                        }
                      >
                        {t(
                          client.state === "revoked"
                            ? "boxAccount.revoked"
                            : client.role === "primary"
                            ? "boxAccount.ownerDevice"
                            : "boxAccount.guestDevice",
                        )}
                      </Tag>
                    </div>
                    <p className={styles.meta}>
                      {client.platform} ·{" "}
                      {t("boxAccount.lastActive", {
                        time: displayTime(client.last_seen_at),
                      })}
                    </p>
                  </div>
                  {client.state === "active" && (
                    <div className={styles.actions}>
                      {client.role !== "primary" && (
                        <Button
                          loading={busyId === client.client_id}
                          onClick={() => changeClient(client, true)}
                        >
                          {t("boxAccount.setOwner")}
                        </Button>
                      )}
                      <Button
                        danger
                        loading={busyId === client.client_id}
                        onClick={() => changeClient(client, false)}
                      >
                        {t("boxAccount.revoke")}
                      </Button>
                    </div>
                  )}
                </article>
              ))}
            </div>
          )}
          <p className={styles.meta}>{t("boxAccount.webIndependent")}</p>
        </section>
      )}
      <section className={styles.section}>
        <h2 className={styles.sectionTitle}>{t("boxAccount.managementKey")}</h2>
        <p className={styles.description}>
          {t("boxAccount.managementSecurityHint")}
        </p>
        <Button
          icon={<KeyRound size={16} />}
          onClick={() => navigate("/connect?rotate=1")}
        >
          {t("boxAccount.rotateKey")}
        </Button>
      </section>
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
  );
}
