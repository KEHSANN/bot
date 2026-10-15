/**
 * Typed RTL Web Admin Panel API Client.
 * Enforces server-side session token authentication and IRT integer formatting.
 */
export interface DashboardMetrics {
  total_users: number;
  active_users: number;
  total_orders: number;
  delivered_orders: number;
  failed_deliveries: number;
  order_revenue_irt: number;
  order_revenue_formatted: string;
  gross_gateway_deposits_irt: number;
  gross_manual_deposits_irt: number;
  wallet_liabilities_irt: number;
  pending_manual_deposits: number;
  pending_milestones: number;
  available_inventory: number;
}

export interface BulkImportPreview {
  product_id: number;
  service_type: string;
  delimiter: string;
  total_count: number;
  valid_count: number;
  invalid_count: number;
  duplicate_count: number;
  can_commit_atomic: boolean;
  errors: Array<{ line: number; type: string; error: string }>;
}

export function formatPersianIRT(amount: number): string {
  return new Intl.NumberFormat("fa-IR").format(amount) + " تومان";
}

export async function apiFetch<T>(
  path: string,
  token: string,
  options: RequestInit = {}
): Promise<T> {
  const res = await fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
      ...(options.headers || {}),
    },
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: "خطای ناشناخته در ارتباط با سرور" }));
    throw new Error(err.detail || `HTTP ${res.status}`);
  }
  return res.json() as Promise<T>;
}
