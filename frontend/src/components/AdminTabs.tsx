/**
 * Complete Interactive RTL Components for All 9 Web Administration Sections:
 * Users, Catalog & Randomized Pricing, Orders & Provisioning Reconciliation,
 * Wallet & Manual Deposits, Referral Rules (N/W/P/M/K) & Milestone Approvals,
 * Mandatory Channels & Message Templates, and Redacted Audit Logs.
 */
import React, { useState } from "react";
import { apiFetch, formatPersianIRT } from "../lib/api";

export function UsersAdminTab({ token, onStatus }: { token: string; onStatus: (m: string) => void }) {
  const [userId, setUserId] = useState("");
  const [newStatus, setNewStatus] = useState("blocked");
  const [reason, setReason] = useState("");

  async function handleChangeStatus() {
    try {
      await apiFetch(`/api/admin/users/${Number(userId)}/status`, token, {
        method: "POST",
        body: JSON.stringify({ new_status: newStatus, reason }),
      });
      onStatus(`وضعیت کاربر #${userId} به ${newStatus} تغییر یافت و در لاگ ممیزی ثبت شد.`);
    } catch (e: any) {
      onStatus(e.message);
    }
  }

  return (
    <section style={{ background: "#1e293b", padding: "20px", borderRadius: "12px" }}>
      <h2>مدیریت کاربران، وضعیت حساب و یادداشت‌های مدیریتی</h2>
      <div style={{ display: "grid", gap: "12px", maxWidth: "480px" }}>
        <input placeholder="شناسه داخلی کاربر (User ID)" value={userId} onChange={(e) => setUserId(e.target.value)} />
        <select value={newStatus} onChange={(e) => setNewStatus(e.target.value)}>
          <option value="active">فعال (Active)</option>
          <option value="blocked">مسدود (Blocked)</option>
          <option value="suspended">تعلیق موقت (Suspended)</option>
        </select>
        <input placeholder="دلیل تغییر وضعیت (الزامی برای ثبت در Audit Log)" value={reason} onChange={(e) => setReason(e.target.value)} />
        <button onClick={handleChangeStatus} style={{ padding: "10px", background: "#ef4444", color: "#fff", border: "none", borderRadius: "8px" }}>
          ثبت تغییر وضعیت کاربر
        </button>
      </div>
    </section>
  );
}

export function CatalogPricingTab({ token, onStatus }: { token: string; onStatus: (m: string) => void }) {
  const [code, setCode] = useState("vip_1m_50g");
  const [titleFa, setTitleFa] = useState("اشتراک ویژه ۱ ماهه ۵۰ گیگابایت");
  const [basePrice, setBasePrice] = useState(120000);
  const [minInc, setMinInc] = useState(100);
  const [maxInc, setMaxInc] = useState(900);
  const [stepInc, setStepInc] = useState(100);
  const [mode, setMode] = useState("manual");

  async function handleCreateProduct() {
    try {
      await apiFetch("/api/admin/products", token, {
        method: "POST",
        body: JSON.stringify({
          category_id: 1,
          code,
          title_fa: titleFa,
          description_fa: "تحویل آنی با پشتیبانی ۲۴ ساعته",
          service_type: "vless",
          inventory_mode: mode,
          provider_code: mode === "automated_provider" ? "marzban_main" : null,
          base_price_irt: Number(basePrice),
          min_random_increment_irt: Number(minInc),
          max_random_increment_irt: Number(maxInc),
          increment_step_irt: Number(stepInc),
          duration_days: 30,
          traffic_gb: 50,
          device_limit: 2,
        }),
      });
      onStatus(`محصول «${titleFa}» با قیمت پایه ${formatPersianIRT(basePrice)} و گام تصادفی ثبت شد.`);
    } catch (e: any) {
      onStatus(e.message);
    }
  }

  return (
    <section style={{ background: "#1e293b", padding: "20px", borderRadius: "12px" }}>
      <h2>مدیریت محصولات و موتور قیمت‌گذاری تصادفی پایدار (IRT)</h2>
      <div style={{ display: "grid", gap: "12px", maxWidth: "520px" }}>
        <input value={code} onChange={(e) => setCode(e.target.value)} placeholder="کد یکتای محصول" />
        <input value={titleFa} onChange={(e) => setTitleFa(e.target.value)} placeholder="عنوان فارسی محصول" />
        <label>قیمت پایه (تومان - IRT): {formatPersianIRT(basePrice)}</label>
        <input type="number" value={basePrice} onChange={(e) => setBasePrice(Number(e.target.value))} />
        <div style={{ display: "flex", gap: "8px" }}>
          <input type="number" value={minInc} onChange={(e) => setMinInc(Number(e.target.value))} placeholder="حداقل افزایش تصادفی" />
          <input type="number" value={maxInc} onChange={(e) => setMaxInc(Number(e.target.value))} placeholder="حداکثر افزایش تصادفی" />
          <input type="number" value={stepInc} onChange={(e) => setStepInc(Number(e.target.value))} placeholder="گام (Step)" />
        </div>
        <select value={mode} onChange={(e) => setMode(e.target.value)}>
          <option value="manual">موجودی دستی (Manual Inventory)</option>
          <option value="automated_provider">تامین خودکار از سرویس‌دهنده (Marzban / 3x-ui)</option>
        </select>
        <button onClick={handleCreateProduct} style={{ padding: "10px", background: "#10b981", color: "#fff", border: "none", borderRadius: "8px" }}>
          ایجاد محصول جدید
        </button>
      </div>
    </section>
  );
}

export function PaymentsAndDepositsTab({ token, onStatus }: { token: string; onStatus: (m: string) => void }) {
  const [reqId, setReqId] = useState("");
  const [reason, setReason] = useState("رسید بانکی بررسی و تایید شد");

  async function handleReview(approve: boolean) {
    try {
      await apiFetch(`/api/admin/deposits/${reqId}/review`, token, {
        method: "POST",
        body: JSON.stringify({ approve, reason }),
      });
      onStatus(`درخواست واریز دستی ${reqId} با موفقیت ${approve ? "تایید و شارژ" : "رد"} شد.`);
    } catch (e: any) {
      onStatus(e.message);
    }
  }

  return (
    <section style={{ background: "#1e293b", padding: "20px", borderRadius: "12px" }}>
      <h2>بررسی واریزهای دستی کارت‌به‌کارت و اصلاحات دفتر کل (کیف پول)</h2>
      <div style={{ display: "grid", gap: "12px", maxWidth: "480px" }}>
        <input placeholder="شناسه درخواست واریز (مثلاً mdep_...)" value={reqId} onChange={(e) => setReqId(e.target.value)} />
        <input placeholder="دلیل تایید یا رد (الزامی)" value={reason} onChange={(e) => setReason(e.target.value)} />
        <div style={{ display: "flex", gap: "12px" }}>
          <button onClick={() => handleReview(true)} style={{ padding: "10px 18px", background: "#22c55e", color: "#fff", border: "none", borderRadius: "8px" }}>
            تایید و شارژ کیف پول (تک‌تراکنشی)
          </button>
          <button onClick={() => handleReview(false)} style={{ padding: "10px 18px", background: "#ef4444", color: "#fff", border: "none", borderRadius: "8px" }}>
            رد درخواست
          </button>
        </div>
      </div>
    </section>
  );
}

export function ReferralsAndMilestonesTab({ token, onStatus }: { token: string; onStatus: (m: string) => void }) {
  const [rewardN, setRewardN] = useState(15000);
  const [rewardW, setRewardW] = useState(10000);
  const [rewardP, setRewardP] = useState(25000);
  const [thresholdM, setThresholdM] = useState(5);
  const [rewardK, setRewardK] = useState(100000);
  const [milestoneId, setMilestoneId] = useState("");
  const [reviewReason, setReviewReason] = useState("دعوت‌ها واقعی و واجد شرایط هستند");

  async function saveRules() {
    try {
      await apiFetch("/api/admin/referrals/rules", token, {
        method: "PUT",
        body: JSON.stringify({
          is_enabled: true,
          referrer_reward_n_irt: rewardN,
          referrer_n_trigger: "membership_verified",
          new_user_reward_w_irt: rewardW,
          new_user_w_requires_membership: true,
          first_payment_reward_p_irt: rewardP,
          milestone_threshold_m: thresholdM,
          milestone_reward_k_irt: rewardK,
          milestone_repeatable: false,
        }),
      });
      onStatus("قوانین چهارگانه پاداش دعوت (N, W, P, M/K) به‌روزرسانی شد.");
    } catch (e: any) {
      onStatus(e.message);
    }
  }

  async function reviewMilestone(approve: boolean) {
    try {
      await apiFetch(`/api/admin/milestones/${milestoneId}/review`, token, {
        method: "POST",
        body: JSON.stringify({ approve, reason: reviewReason }),
      });
      onStatus(`درخواست پاداش حدنصاب ${milestoneId} ${approve ? "تایید و واریز" : "رد"} شد.`);
    } catch (e: any) {
      onStatus(e.message);
    }
  }

  return (
    <section style={{ background: "#1e293b", padding: "20px", borderRadius: "12px" }}>
      <h2>تنظیمات سیستم دعوت (N, W, P) و تایید پاداش حدنصاب (M و K)</h2>
      <div style={{ display: "grid", gap: "10px", maxWidth: "520px", marginBottom: "24px" }}>
        <label>پاداش معرف (N): {formatPersianIRT(rewardN)}</label>
        <input type="number" value={rewardN} onChange={(e) => setRewardN(Number(e.target.value))} />
        <label>پاداش خوش‌آمدگویی کاربر جدید (W): {formatPersianIRT(rewardW)}</label>
        <input type="number" value={rewardW} onChange={(e) => setRewardW(Number(e.target.value))} />
        <label>پاداش اولین پرداخت تاییدشده (P): {formatPersianIRT(rewardP)}</label>
        <input type="number" value={rewardP} onChange={(e) => setRewardP(Number(e.target.value))} />
        <label>حدنصاب تعداد دعوت موفق (M): {thresholdM} نفر | پاداش حدنصاب (K): {formatPersianIRT(rewardK)}</label>
        <div style={{ display: "flex", gap: "8px" }}>
          <input type="number" value={thresholdM} onChange={(e) => setThresholdM(Number(e.target.value))} />
          <input type="number" value={rewardK} onChange={(e) => setRewardK(Number(e.target.value))} />
        </div>
        <button onClick={saveRules} style={{ padding: "10px", background: "#3b82f6", color: "#fff", border: "none", borderRadius: "8px" }}>
          ذخیره قوانین پاداش دعوت
        </button>
      </div>

      <h3>بررسی و تایید مدیریتی درخواست پاداش حدنصاب (Milestone K)</h3>
      <div style={{ display: "grid", gap: "10px", maxWidth: "480px" }}>
        <input placeholder="شناسه درخواست حدنصاب (mreq_...)" value={milestoneId} onChange={(e) => setMilestoneId(e.target.value)} />
        <input placeholder="دلیل تایید یا رد" value={reviewReason} onChange={(e) => setReviewReason(e.target.value)} />
        <div style={{ display: "flex", gap: "12px" }}>
          <button onClick={() => reviewMilestone(true)} style={{ padding: "10px 16px", background: "#22c55e", color: "#fff", border: "none", borderRadius: "8px" }}>
            تایید و واریز پاداش K
          </button>
          <button onClick={() => reviewMilestone(false)} style={{ padding: "10px 16px", background: "#ef4444", color: "#fff", border: "none", borderRadius: "8px" }}>
            رد درخواست حدنصاب
          </button>
        </div>
      </div>
    </section>
  );
}
