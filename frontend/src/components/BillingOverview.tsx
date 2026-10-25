import { useEffect, useState } from "react";
import { getBillingAccount, type BillingAccount } from "../services/clients/billing";

const money = (minor: number) => `¥${(minor / 100).toFixed(2)}`;
const statuses = { active: "使用中", suspended: "已暂停", expired: "已到期" };

// AccountPanel remounts this view for a new user/workspace. Abort and ignore a
// stale response as well, so a slow read cannot repopulate the next account.
export function BillingOverview() {
  const [account, setAccount] = useState<BillingAccount | null>(null);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const request = new AbortController();
    void getBillingAccount(request.signal).then(
      value => { if (!request.signal.aborted) setAccount(value); },
      reason => {
        if (!request.signal.aborted) setError(reason instanceof Error ? reason.message : "套餐与余额加载失败");
      },
    );
    return () => request.abort();
  }, [revision]);

  const refresh = () => {
    setAccount(null);
    setError("");
    setRevision(value => value + 1);
  };
  return (
    <section aria-label="工作区套餐与余额" className="space-y-2 border-t border-[var(--line)] pt-3">
      <div className="flex items-center justify-between gap-2">
        <span className="type-section-heading">工作区套餐与余额</span>
        <button type="button" className="workspace-button" onClick={refresh} disabled={!account && !error}>刷新</button>
      </div>
      {error ? <p role="alert" className="type-body text-red-700" data-i18n-skip>{error}</p> : !account ? (
        <p role="status" className="type-body">正在读取套餐与余额…</p>
      ) : (
        <div className="space-y-1 type-body" data-testid="billing-account">
          <p><span data-i18n-skip>{account.plan.name}</span> · {statuses[account.subscription.status]}</p>
          <p>账户余额：<span data-i18n-skip>{money(account.wallet.balance_minor)}</span></p>
          <p>本月模型请求：<span data-i18n-skip>{account.usage.provider_requests}</span>
            {account.plan.monthly_provider_requests === null
              ? "（沿用系统额度）"
              : <span data-i18n-skip> / {account.plan.monthly_provider_requests}</span>}
          </p>
          <p className="type-caption text-[var(--faint)]">
            <span data-i18n-skip>{account.usage.period}</span>，按 UTC 自然月统计。一个建模任务可能产生多次模型请求。
          </p>
          {account.plan.monthly_price_minor !== null && <p>套餐标价：<span data-i18n-skip>{money(account.plan.monthly_price_minor)}</span> / 月</p>}
          {account.subscription.expires_at && <p>到期时间：<span data-i18n-skip>{new Date(account.subscription.expires_at).toLocaleString()}</span></p>}
          {!account.online_payments_enabled && <p className="type-caption text-[var(--faint)]">在线充值与自动扣费尚未开放。</p>}
        </div>
      )}
    </section>
  );
}
