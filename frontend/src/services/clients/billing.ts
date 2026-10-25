import { API_BASE, authFetch, readJson } from "./http";

export interface BillingAccount {
  tenant_id: string;
  plan: {
    code: "basic" | "pro" | "team";
    version: number;
    name: string;
    currency: "CNY";
    monthly_price_minor: number | null;
    monthly_provider_requests: number | null;
  };
  subscription: {
    status: "active" | "suspended" | "expired";
    expires_at: string | null;
    revision: number;
  };
  wallet: { currency: "CNY"; balance_minor: number };
  usage: { period: string; provider_requests: number; timezone: "UTC" };
  online_payments_enabled: boolean;
}

export async function getBillingAccount(signal?: AbortSignal): Promise<BillingAccount> {
  return readJson<BillingAccount>(
    await authFetch(`${API_BASE}/api/billing/me`, { signal }),
    "套餐与余额加载失败",
  );
}
