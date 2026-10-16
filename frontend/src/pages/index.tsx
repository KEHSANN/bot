/**
 * Main Persian RTL Web Administration Panel (Section 18) — Complete 9-Tab Interface.
 */
import React, { useState } from "react";
import { DashboardMetrics, BulkImportPreview, formatPersianIRT, apiFetch } from "../lib/api";
import {
  UsersAdminTab,
  CatalogPricingTab,
  PaymentsAndDepositsTab,
  ReferralsAndMilestonesTab,
} from "../components/AdminTabs";

type TabKey =
  | "dashboard"
  | "users"
  | "catalog"
  | "inventory"
  | "orders"
  | "payments"
  | "referrals"
  | "messaging"
  | "audit";

export default function AdminPanelRTL() {
  const [activeTab, setActiveTab] = useState<TabKey>("dashboard");
  const [token, setToken] = useState<string>("");
  const [metrics, setMetrics] = useState<DashboardMetrics | null>(null);
  const [importPayload, setImportPayload] = useState<string>("");
  const [importDelimiter, setImportDelimiter] = useState<string>("newline");
  const [preview, setPreview] = useState<BulkImportPreview | null>(null);
  const [reconcileJobId, setReconcileJobId] = useState<string>("");
  const [channelId, setChannelId] = useState<string>("@vpnstore_official");
  const [channelTitle, setChannelTitle] = useState<string>("کانال رسمی فروشگاه");
  const [channelUrl, setChannelUrl] = useState<string>("https://t.me/vpnstore_official");
  const [statusMessage, setStatusMessage] = useState<string>("");

  const tabs: Array<{ key: TabKey; label: string }> = [
    { key: "dashboard", label: "داشبورد و آمار مالی" },
    { key: "users", label: "مدیریت کاربران" },
    { key: "catalog", label: "محصولات و قیمت‌گذاری تصادفی" },
    { key: "inventory", label: "موجودی و درون‌ریزی دسته‌ای (Atomic)" },
    { key: "orders", label: "سفارش‌ها و تطبیق ساخت اشتراک" },
    { key: "payments", label: "کیف پول، واریز دستی و درگاه‌ها" },
    { key: "referrals", label: "سیستم دعوت و پاداش حدنصاب (M/K)" },
    { key: "messaging", label: "کانال‌های اجباری و قالب پیام‌ها" },
    { key: "audit", label: "لاگ‌های امنیتی و ممیزی (Audit)" },
  ];

  async function handlePreviewImport(productId: number) {
    try {
      const res = await apiFetch<BulkImportPreview>("/api/admin/inventory/preview", token, {
        method: "POST",
        body: JSON.stringify({
          product_id: productId,
          raw_payload: importPayload,
          delimiter: importDelimiter,
          mode: "atomic",
        }),
      });
      setPreview(res);
      setStatusMessage("پیش‌نمایش بررسی موجودی با موفقیت انجام شد.");
    } catch (e: any) {
      setStatusMessage(e.message);
    }
  }

  async function handleCommitAtomicImport(productId: number) {
    try {
      await apiFetch("/api/admin/inventory/import", token, {
        method: "POST",
        body: JSON.stringify({
          product_id: productId,
          raw_payload: importPayload,
          delimiter: importDelimiter,
          mode: "atomic",
        }),
      });
      setStatusMessage("درون‌ریزی اتمیک کانفیگ‌ها با موفقیت در دیتابیس ثبت و رمزنگاری شد.");
    } catch (e: any) {
      setStatusMessage(e.message);
    }
  }

  return (
    <div dir="rtl" lang="fa" style={{ fontFamily: "Vazirmatn, Tahoma, sans-serif", background: "#0f172a", color: "#f8fafc", minHeight: "100vh", padding: "24px" }}>
      <header style={{ display: "flex", justifyContent: "space-between", alignItems: "center", borderBottom: "1px solid #334155", paddingBottom: "16px", marginBottom: "24px" }}>
        <div>
          <h1 style={{ margin: 0, fontSize: "22px" }}>پنل مدیریت جامع فروشگاه VPN تلگرام</h1>
          <p style={{ margin: "4px 0 0", color: "#94a3b8", fontSize: "14px" }}>
            واحد پولی پایه: تومان (IRT صحیح) | همگام‌سازی کامل با پنل ادمین تلگرام
          </p>
        </div>
        <span style={{ background: "#1e293b", padding: "8px 14px", borderRadius: "8px", fontSize: "13px", border: "1px solid #334155" }}>
          کنترل دسترسی مبتنی بر نقش (RBAC) فعال است
        </span>
      </header>

      <nav style={{ display: "flex", gap: "8px", flexWrap: "wrap", marginBottom: "24px" }}>
        {tabs.map((t) => (
          <button
            key={t.key}
            onClick={() => setActiveTab(t.key)}
            style={{
              padding: "10px 16px",
              borderRadius: "8px",
              border: "none",
              cursor: "pointer",
              background: activeTab === t.key ? "#2563eb" : "#1e293b",
              color: "#ffffff",
              fontWeight: activeTab === t.key ? 700 : 400,
            }}
          >
            {t.label}
          </button>
        ))}
      </nav>

      {statusMessage && (
        <div role="alert" style={{ background: "#1e3a8a", padding: "12px 16px", borderRadius: "8px", marginBottom: "16px" }}>
          {statusMessage}
        </div>
      )}

      {activeTab === "dashboard" && (
        <section>
          <h2>داشبورد عملیاتی و تفکیک حسابداری (IRT)</h2>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))", gap: "16px" }}>
            <div style={{ background: "#1e293b", padding: "16px", borderRadius: "10px" }}>
              <div>درآمد قطعی سفارش‌های تحویل‌شده</div>
              <strong style={{ fontSize: "20px", color: "#38bdf8" }}>
                {metrics ? metrics.order_revenue_formatted : formatPersianIRT(0)}
              </strong>
            </div>
            <div style={{ background: "#1e293b", padding: "16px", borderRadius: "10px" }}>
              <div>مجموع واریزهای درگاه بانکی</div>
              <strong style={{ fontSize: "20px", color: "#4ade80" }}>
                {formatPersianIRT(metrics?.gross_gateway_deposits_irt || 0)}
              </strong>
            </div>
            <div style={{ background: "#1e293b", padding: "16px", borderRadius: "10px" }}>
              <div>تعهدات مالی کیف پول کاربران (Liabilities)</div>
              <strong style={{ fontSize: "20px", color: "#fbbf24" }}>
                {formatPersianIRT(metrics?.wallet_liabilities_irt || 0)}
              </strong>
            </div>
            <div style={{ background: "#1e293b", padding: "16px", borderRadius: "10px" }}>
              <div>درخواست‌های پاداش حدنصاب (Milestone) در انتظار</div>
              <strong style={{ fontSize: "20px", color: "#f472b6" }}>
                {metrics?.pending_milestones || 0} مورد
              </strong>
            </div>
          </div>
        </section>
      )}

      {activeTab === "users" && <UsersAdminTab token={token} onStatus={setStatusMessage} />}
      {activeTab === "catalog" && <CatalogPricingTab token={token} onStatus={setStatusMessage} />}

      {activeTab === "inventory" && (
        <section style={{ background: "#1e293b", padding: "20px", borderRadius: "12px" }}>
          <h2>درون‌ریزی دسته‌ای و اتمیک کانفیگ‌های VPN (با تشخیص تکراری SHA-256)</h2>
          <div style={{ marginBottom: "12px" }}>
            <label>انتخاب جداکننده (Delimiter): </label>
            <select value={importDelimiter} onChange={(e) => setImportDelimiter(e.target.value)}>
              <option value="newline">خط جدید (Newline)</option>
              <option value=",">کاما (,)</option>
              <option value=";">سمی‌کالن (;)</option>
              <option value="|">پایپ (|)</option>
            </select>
          </div>
          <textarea
            rows={6}
            style={{ width: "100%", borderRadius: "8px", padding: "12px", direction: "ltr" }}
            placeholder="vless://uuid@server:443#name..."
            value={importPayload}
            onChange={(e) => setImportPayload(e.target.value)}
          />
          <div style={{ marginTop: "12px", display: "flex", gap: "12px" }}>
            <button onClick={() => handlePreviewImport(1)} style={{ padding: "10px 18px", background: "#3b82f6", color: "#fff", border: "none", borderRadius: "8px" }}>
              پیش‌نمایش و اعتبارسنجی قبل از ثبت
            </button>
            <button onClick={() => handleCommitAtomicImport(1)} style={{ padding: "10px 18px", background: "#10b981", color: "#fff", border: "none", borderRadius: "8px" }}>
              تایید و ثبت اتمیک (Atomic Commit)
            </button>
          </div>
          {preview && (
            <div style={{ marginTop: "16px", background: "#0f172a", padding: "16px", borderRadius: "8px" }}>
              <p>کل موارد: {preview.total_count} | معتبر: {preview.valid_count} | نامعتبر: {preview.invalid_count} | تکراری: {preview.duplicate_count}</p>
              <p>آماده ثبت اتمیک (Atomic Commit): {preview.can_commit_atomic ? "بله ✅" : "خیر ❌ (ابتدا خطاها را برطرف کنید)"}</p>
            </div>
          )}
        </section>
      )}

      {activeTab === "orders" && (
        <section style={{ background: "#1e293b", padding: "20px", borderRadius: "12px" }}>
          <h2>مدیریت سفارش‌ها و تطبیق ایمن وضعیت ساخت اشتراک (Reconciliation)</h2>
          <p>در صورت بروز Timeout مبهم در سرویس‌دهنده VPN، با وارد کردن شناسه Job، وضعیت اشتراک از طریق کلید یکتا استعلام و بدون ساخت تکراری تحویل داده می‌شود.</p>
          <div style={{ display: "flex", gap: "12px", maxWidth: "480px" }}>
            <input placeholder="شناسه Provisioning Job (pjob_...)" value={reconcileJobId} onChange={(e) => setReconcileJobId(e.target.value)} />
            <button
              onClick={async () => {
                try {
                  await apiFetch(`/api/admin/provisioning/${reconcileJobId}/reconcile`, token, { method: "POST" });
                  setStatusMessage(`تطبیق وضعیت اشتراک برای ${reconcileJobId} با موفقیت انجام شد.`);
                } catch (e: any) {
                  setStatusMessage(e.message);
                }
              }}
              style={{ padding: "10px 16px", background: "#f59e0b", color: "#000", border: "none", borderRadius: "8px", fontWeight: 700 }}
            >
              استعلام و تطبیق ایمن
            </button>
          </div>
        </section>
      )}

      {activeTab === "payments" && <PaymentsAndDepositsTab token={token} onStatus={setStatusMessage} />}
      {activeTab === "referrals" && <ReferralsAndMilestonesTab token={token} onStatus={setStatusMessage} />}

      {activeTab === "messaging" && (
        <section style={{ background: "#1e293b", padding: "20px", borderRadius: "12px" }}>
          <h2>مدیریت کانال‌های عضویت اجباری (با کش ۲۴ ساعته) و قالب پیام‌های فارسی</h2>
          <div style={{ display: "grid", gap: "10px", maxWidth: "480px" }}>
            <input value={channelId} onChange={(e) => setChannelId(e.target.value)} placeholder="شناسه کانال (@channel)" />
            <input value={channelTitle} onChange={(e) => setChannelTitle(e.target.value)} placeholder="عنوان فارسی کانال" />
            <input value={channelUrl} onChange={(e) => setChannelUrl(e.target.value)} placeholder="لینک عضویت (https://t.me/...)" />
            <button
              onClick={async () => {
                try {
                  await apiFetch("/api/admin/channels", token, {
                    method: "POST",
                    body: JSON.stringify({ channel_id: channelId, title_fa: channelTitle, join_url: channelUrl, cache_ttl_seconds: 86400 }),
                  });
                  setStatusMessage(`کانال اجباری ${channelTitle} با کش ۲۴ ساعته ثبت شد.`);
                } catch (e: any) {
                  setStatusMessage(e.message);
                }
              }}
              style={{ padding: "10px", background: "#3b82f6", color: "#fff", border: "none", borderRadius: "8px" }}
            >
              افزودن / به‌روزرسانی کانال اجباری
            </button>
          </div>
        </section>
      )}

      {activeTab === "audit" && (
        <section style={{ background: "#1e293b", padding: "20px", borderRadius: "12px" }}>
          <h2>لاگ‌های ممیزی و امنیتی (Redacted Audit Logs)</h2>
          <p>تمامی اقدامات حساس مدیریتی به همراه شناسه ادمین، دلیل، زمان و Correlation ID بدون افشای کلیدها یا کانفیگ‌های خصوصی ثبت می‌شوند.</p>
        </section>
      )}
    </div>
  );
}
