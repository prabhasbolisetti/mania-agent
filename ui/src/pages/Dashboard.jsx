import {
  useCallback,
  useEffect,
  useMemo,
  useState,
} from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import Sidebar from "../components/Sidebar";
import StatCard from "../components/StatCard";
import {
  getCurrentUser,
  getLeads,
  updateLeadCallStatus,
} from "../api";
/* =========================================================
   HELPERS
========================================================= */
function getCustomer(lead) {
  return (
    lead?.customer ||
    lead?.customer_data ||
    { phone_number: lead?.phone_number || "" }
  );
}
function formatDayLabel(dateString) {
  if (!dateString) {
    return "Unknown date";
  }
  const date = new Date(dateString);
  if (Number.isNaN(date.getTime())) {
    return "Unknown date";
  }
  const now = new Date();
  const today = new Date(
    now.getFullYear(),
    now.getMonth(),
    now.getDate()
  );
  const target = new Date(
    date.getFullYear(),
    date.getMonth(),
    date.getDate()
  );
  const difference = Math.round(
    (today - target) /
      (1000 * 60 * 60 * 24)
  );
  if (difference === 0) {
    return "Today";
  }
  if (difference === 1) {
    return "Yesterday";
  }
  return date.toLocaleDateString(
    "en-IN",
    {
      weekday: "long",
      day: "numeric",
      month: "short",
      year: "numeric",
    }
  );
}
function dateKey(dateString) {
  if (!dateString) {
    return "unknown";
  }
  const date = new Date(dateString);
  if (Number.isNaN(date.getTime())) {
    return "unknown";
  }
  return [
    date.getFullYear(),
    String(date.getMonth() + 1).padStart(
      2,
      "0"
    ),
    String(date.getDate()).padStart(
      2,
      "0"
    ),
  ].join("-");
}
function getLeadStatus(lead) {
  const status = String(
    lead?.intent?.status || lead?.qualification?.status || ""
  ).toLowerCase();
  if (["hot", "warm", "low"].includes(status)) return status;
  const score = getLeadScore(lead);
  return score >= 60 ? "hot" : score >= 35 ? "warm" : "low";
}
function getLeadScore(lead) {
  return Number(
    lead?.intent?.score ??
      lead?.qualification?.score ??
      0
  );
}
function getCustomerName(lead) {
  const customer = getCustomer(lead);
  return (
    customer?.name ||
    customer?.customer_name ||
    customer?.full_name ||
    customer?.customerName ||
    customer?.phone ||
    customer?.phone_number ||
    "Unknown customer"
  );
}
function getCustomerPhone(lead) {
  const customer = getCustomer(lead);
  return (
    customer?.phone_number ||
    customer?.phone ||
    customer?.mobile ||
    lead?.phone_number ||
    ""
  );
}
async function copyPhoneNumber(phone) {
  if (!phone) return;
  try {
    await navigator.clipboard.writeText(String(phone));
  } catch {
    // Clipboard access can be blocked by browser permissions.
  }
}
function getProduct(lead) {
  const customer = getCustomer(lead);
  return (
    customer?.product_interest ||
    customer?.product ||
    "Product not specified"
  );
}
function getStorage(lead) {
  const customer = getCustomer(lead);
  return (
    customer?.storage ||
    ""
  );
}
function getBudget(lead) {
  const customer = getCustomer(lead);
  const budget =
    customer?.budget;
  if (!budget) {
    if (
      customer?.budget_min ||
      customer?.budget_max
    ) {
      return `${customer?.budget_min || ""} - ${
        customer?.budget_max || ""
      }`;
    }
    return "";
  }
  if (typeof budget === "string") {
    try {
      const parsed = JSON.parse(budget);
      if (
        parsed &&
        typeof parsed === "object"
      ) {
        const min =
          parsed.min ??
          parsed.budget_min;
        const max =
          parsed.max ??
          parsed.budget_max;
        if (min || max) {
          if (min && max) {
            return `₹${Number(min).toLocaleString(
              "en-IN"
            )} - ₹${Number(max).toLocaleString(
              "en-IN"
            )}`;
          }
          if (min) {
            return `₹${Number(min).toLocaleString(
              "en-IN"
            )}+`;
          }
          if (max) {
            return `Up to ₹${Number(max).toLocaleString(
              "en-IN"
            )}`;
          }
        }
      }
    } catch {
      return budget;
    }
    return budget;
  }
  if (
    typeof budget === "object"
  ) {
    const min =
      budget.min ??
      budget.budget_min;
    const max =
      budget.max ??
      budget.budget_max;
    if (min && max) {
      return `₹${Number(min).toLocaleString(
        "en-IN"
      )} - ₹${Number(max).toLocaleString(
        "en-IN"
      )}`;
    }
    if (min) {
      return `₹${Number(min).toLocaleString(
        "en-IN"
      )}+`;
    }
    if (max) {
      return `Up to ₹${Number(max).toLocaleString(
        "en-IN"
      )}`;
    }
  }
  return String(budget);
}
function formatTime(dateString) {
  if (!dateString) {
    return "--";
  }
  const date = new Date(dateString);
  if (Number.isNaN(date.getTime())) {
    return "--";
  }
  return date.toLocaleTimeString(
    "en-IN",
    {
      hour: "2-digit",
      minute: "2-digit",
    }
  );
}
function formatShortDate(dateString) {
  if (!dateString) {
    return "--";
  }
  const date = new Date(dateString);
  if (Number.isNaN(date.getTime())) {
    return "--";
  }
  return date.toLocaleDateString(
    "en-IN",
    {
      day: "2-digit",
      month: "short",
    }
  );
}
/* =========================================================
   STATUS BADGE
========================================================= */
function StatusBadge({
  status,
}) {
  return (
    <span
      className={`dashboard-status dashboard-status-${status}`}
    >
      <span className="dashboard-status-dot" />
      {status}
    </span>
  );
}
/* =========================================================
   TODAY DRAWER
========================================================= */
function TodayDrawer({
  open,
  onClose,
  leads,
  onOpenLead,
}) {
  const todayLeads = leads;
  const hot = todayLeads.filter(
    (lead) =>
      getLeadStatus(lead) === "hot"
  ).length;
  const warm = todayLeads.filter(
    (lead) =>
      getLeadStatus(lead) === "warm"
  ).length;
  const low = todayLeads.filter(
    (lead) =>
      getLeadStatus(lead) === "low"
  ).length;
  const averageScore =
    todayLeads.length > 0
      ? Math.round(
          todayLeads.reduce(
            (total, lead) =>
              total +
              getLeadScore(lead),
            0
          ) / todayLeads.length
        )
      : 0;
  return (
    <>
      {open && (
        <button
          type="button"
          className="today-drawer-overlay"
          onClick={onClose}
          aria-label="Close today's panel"
        />
      )}
      <aside
        className={`today-drawer ${
          open
            ? "today-drawer-open"
            : ""
        }`}
      >
        <div className="today-drawer-header">
          <div>
            <div className="today-drawer-eyebrow">
              LIVE OVERVIEW
            </div>
            <h2>
              Today's activity
            </h2>
            <p>
              {todayLeads.length} lead
              {todayLeads.length === 1
                ? ""
                : "s"} received today
            </p>
          </div>
          <button
            type="button"
            className="today-drawer-close"
            onClick={onClose}
            aria-label="Close today's activity"
          >
            ×
          </button>
        </div>
        <div className="today-highlight">
          <div className="today-highlight-icon">
            ✦
          </div>
          <div>
            <strong>
              Today's focus
            </strong>
            <span>
              Prioritize high-intent
              customers first.
            </span>
          </div>
        </div>
        <div className="today-mini-grid">
          <div className="today-mini-card">
            <span>HOT</span>
            <strong>{hot}</strong>
          </div>
          <div className="today-mini-card">
            <span>WARM</span>
            <strong>{warm}</strong>
          </div>
          <div className="today-mini-card">
            <span>LOW</span>
            <strong>{low}</strong>
          </div>
          <div className="today-mini-card">
            <span>AVG SCORE</span>
            <strong>
              {averageScore}
            </strong>
          </div>
        </div>
        <div className="today-drawer-section">
          <div className="today-section-heading">
            <span>
              Recent leads
            </span>
            <span>
              {todayLeads.length}
            </span>
          </div>
          {todayLeads.length === 0 ? (
            <div className="today-empty">
              <div className="today-empty-icon">
                ○
              </div>
              <strong>
                No leads today
              </strong>
              <span>
                New WhatsApp leads
                will appear here.
              </span>
            </div>
          ) : (
            <div className="today-lead-list">
              {todayLeads
                .slice()
                .sort(
                  (a, b) =>
                    new Date(
                      b.updated_at ||
                        b.created_at ||
                        0
                    ) -
                    new Date(
                      a.updated_at ||
                        a.created_at ||
                        0
                    )
                )
                .slice(0, 10)
                .map((lead) => {
                  const status =
                    getLeadStatus(lead);
                  return (
                    <button
                      type="button"
                      key={lead.lead_id}
                      className="today-lead-item"
                      onClick={() =>
                        onOpenLead(
                          lead.lead_id
                        )
                      }
                    >
                      <div className="today-lead-avatar">
                        {getCustomerName(
                          lead
                        )
                          .charAt(0)
                          .toUpperCase()}
                      </div>
                      <div className="today-lead-info">
                        <strong>
                          {getCustomerName(
                            lead
                          )}
                        </strong>
                        <span>
                          {getProduct(
                            lead
                          )}
                        </span>
                      </div>
                      <div className="today-lead-meta">
                        <StatusBadge
                          status={status}
                        />
                        <small>
                          {formatTime(
                            lead.updated_at ||
                              lead.created_at
                          )}
                        </small>
                      </div>
                    </button>
                  );
                })}
            </div>
          )}
        </div>
      </aside>
    </>
  );
}
/* =========================================================
   DAY SECTION
========================================================= */
function DaySection({
  group,
  onOpenLead,
  onToggleCall,
  updatingCallIds,
}) {
  const hot = group.leads.filter(
    (lead) =>
      getLeadStatus(lead) === "hot"
  ).length;
  const warm = group.leads.filter(
    (lead) =>
      getLeadStatus(lead) === "warm"
  ).length;
  const low = group.leads.filter(
    (lead) =>
      getLeadStatus(lead) === "low"
  ).length;
  const isToday =
    group.label === "Today";
  return (
    <section
      className={`day-section ${
        isToday
          ? "day-section-today"
          : ""
      }`}
    >
      <div className="day-section-header">
        <div className="day-heading">
          <div
            className={`day-calendar ${
              isToday
                ? "day-calendar-today"
                : ""
            }`}
          >
            <span>
              {isToday
                ? "TODAY"
                : formatShortDate(
                    group.leads[0]
                      ?.created_at ||
                      group.leads[0]
                        ?.updated_at
                  )}
            </span>
          </div>
          <div>
            <h3>
              {group.label}
            </h3>
            <p>
              {group.leads.length} lead
              {group.leads.length === 1
                ? ""
                : "s"}
            </p>
          </div>
        </div>
        <div className="day-summary">
          <span className="day-summary-hot">
            {hot} hot
          </span>
          <span className="day-summary-warm">
            {warm} warm
          </span>
          <span className="day-summary-low">
            {low} low
          </span>
        </div>
      </div>
      <div className="day-table-wrapper">
        <table className="day-lead-table" style={{ minWidth: "1120px" }}>
          <thead>
            <tr>
              <th>
                Mobile Number
              </th>
              <th>
                Product
              </th>
              <th>
                Intent
              </th>
              <th>
                Score
              </th>
              <th>
                Updated
              </th>
              <th>Call done</th>
              <th style={{ width: "150px", minWidth: "150px" }}>Conversation</th>
            </tr>
          </thead>
          <tbody>
            {group.leads.map(
              (lead) => {
                const status =
                  getLeadStatus(
                    lead
                  );
                const score =
                  getLeadScore(
                    lead
                  );
                return (
                  <tr
                    key={
                      lead.lead_id
                    }
                  >
                    <td>
                      <div className="customer-cell call-phone-cell">
                        <div className="customer-info">
                          <strong className="phone-number-text">
                            {getCustomerPhone(lead) || "No mobile number"}
                          </strong>
                          {getCustomerPhone(lead) && (
                            <button
                              type="button"
                              className="copy-phone-button"
                              aria-label={`Copy ${getCustomerPhone(lead)}`}
                              title="Copy mobile number"
                              onClick={(event) => {
                                event.stopPropagation();
                                copyPhoneNumber(getCustomerPhone(lead));
                              }}
                            >
                              <svg viewBox="0 0 24 24" aria-hidden="true">
                                <rect x="8" y="8" width="12" height="12" rx="2" fill="none" stroke="currentColor" strokeWidth="1.8" />
                                <path d="M16 8V5a1 1 0 0 0-1-1H5a1 1 0 0 0-1 1v10a1 1 0 0 0 1 1h3" fill="none" stroke="currentColor" strokeWidth="1.8" />
                              </svg>
                            </button>
                          )}
                        </div>
                      </div>
                    </td>
                    <td>
                      <span className="product-cell">
                        {getProduct(
                          lead
                        )}
                        {getStorage(
                          lead
                        ) && (
                          <>
                            {" "}
                            ·{" "}
                            {getStorage(
                              lead
                            )}
                          </>
                        )}
                      </span>
                    </td>
                    <td>
                      <StatusBadge
                        status={status}
                      />
                    </td>
                    <td>
                      <div className="score-cell">
                        <strong>
                          {score}
                        </strong>
                        <div className="score-bar">
                          <span
                            style={{
                              width: `${Math.min(
                                100,
                                Math.max(
                                  0,
                                  score
                                )
                              )}%`,
                            }}
                          />
                        </div>
                      </div>
                    </td>
                    <td>
                      <div className="updated-cell">
                        <strong>
                          {formatTime(
                            lead.updated_at ||
                              lead.created_at
                          )}
                        </strong>
                        <span>
                          {formatShortDate(
                            lead.updated_at ||
                              lead.created_at
                          )}
                        </span>
                      </div>
                    </td>
                    <td>
                      <label className={`call-completed-control ${lead.call_completed ? "is-complete" : ""}`}>
                        <input
                          type="checkbox"
                          checked={Boolean(lead.call_completed)}
                          disabled={updatingCallIds.has(String(lead.lead_id))}
                          onChange={() => onToggleCall(lead)}
                          aria-label={`Mark call ${lead.call_completed ? "pending" : "completed"} for ${getCustomerPhone(lead) || "this lead"}`}
                        />
                        <span>{lead.call_completed ? "Done" : "Pending"}</span>
                      </label>
                    </td>
                    <td style={{ width: "150px", minWidth: "150px" }}>
                      <button
                        type="button"
                        className="lead-view-button"
                        aria-label={`View conversation with ${getCustomerPhone(lead) || getCustomerName(lead)}`}
                        title="Open the full customer conversation"
                        onClick={() => onOpenLead(lead.lead_id)}
                        style={{ display: "inline-flex", alignItems: "center", gap: "8px", minWidth: "112px", whiteSpace: "nowrap" }}
                      >
                        Conversation
                        <span aria-hidden="true">→</span>
                      </button>
                    </td>
                  </tr>
                );
              }
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}
/* =========================================================
   DASHBOARD
========================================================= */
function Dashboard() {
  const navigate =
    useNavigate();
  const [searchParams] = useSearchParams();
  const activeScope = searchParams.get("scope") || "all";
  const activeIntent = searchParams.get("intent") || "";
  const [leads, setLeads] =
    useState([]);
  const [currentUser, setCurrentUser] =
    useState(null);
  const [loading, setLoading] =
    useState(true);
  const [error, setError] =
    useState("");
  const [todayOpen, setTodayOpen] =
    useState(false);
  const [search, setSearch] =
    useState("");
  const [updatingCallIds, setUpdatingCallIds] = useState(() => new Set());
  /* -------------------------------------------------------
     LOAD DASHBOARD
  ------------------------------------------------------- */
  const loadDashboard =
    useCallback(
      async () => {
        setLoading(true);
        setError("");
        try {
          const user = await getCurrentUser();
          if (!user?.authenticated) {
            sessionStorage.removeItem("mania_authenticated");
            sessionStorage.removeItem("mania_username");
            setCurrentUser(null);
            setLeads([]);
            navigate("/login", {
              replace: true,
              state: { from: "/" },
            });
            return;
          }
          setCurrentUser(user);
          const leadResponse = await getLeads();
          const leadData = Array.isArray(leadResponse)
            ? leadResponse
            : leadResponse?.leads || [];
          setLeads(leadData);
        } catch (err) {
          if (err?.status === 401) {
            sessionStorage.removeItem("mania_authenticated");
            sessionStorage.removeItem("mania_username");
            setCurrentUser(null);
            navigate("/login", {
              replace: true,
              state: { from: "/" },
            });
            return;
          }
          setError(err?.message || "Unable to load dashboard data.");
        } finally {
          setLoading(false);
        }
      },
      [navigate]
    );
  useEffect(() => {
    loadDashboard();
  }, [loadDashboard]);
  /* -------------------------------------------------------
     SEARCH
  ------------------------------------------------------- */
  const filteredLeads =
    useMemo(() => {
      const query =
        search
          .trim()
          .toLowerCase();
      return leads.filter(
        (lead) => {
          const leadDate = lead.updated_at || lead.created_at;
          const now = new Date();
          const localToday = [now.getFullYear(), String(now.getMonth() + 1).padStart(2, "0"), String(now.getDate()).padStart(2, "0")].join("-");
          if (activeScope === "today" && dateKey(leadDate) !== localToday) return false;
          if (activeIntent) {
            const score = getLeadScore(lead);
            const bucket = score >= 60 ? "hot" : score >= 35 ? "warm" : "low";
            if (bucket !== activeIntent) return false;
          }
          const customer =
            getCustomerName(
              lead
            );
          const phone =
            getCustomerPhone(
              lead
            );
          const product =
            getProduct(lead);
          const storage =
            getStorage(lead);
          const budget =
            getBudget(lead);
          const status =
            getLeadStatus(
              lead
            );
          const customerData =
            getCustomer(lead);
          const matchesSearch = !query || [
            customer, phone, product, storage, budget, status,
            JSON.stringify(customerData),
          ].join(" ").toLowerCase().includes(query);
          return matchesSearch;
        }
      );
    }, [leads, search, activeScope, activeIntent]);
  /* -------------------------------------------------------
     STATS
     IMPORTANT:
     Keep exactly 4 cards here.
     The stylesheet is designed for 4 columns.
  ------------------------------------------------------- */
  const stats =
    useMemo(() => {
      const hot =
        leads.filter(
          (lead) =>
            getLeadStatus(
              lead
            ) === "hot"
        ).length;
      const warm =
        leads.filter(
          (lead) =>
            getLeadStatus(
              lead
            ) === "warm"
        ).length;
      const low =
        leads.filter(
          (lead) =>
            getLeadStatus(
              lead
            ) === "low"
        ).length;
      return {
        total: leads.length,
        hot,
        warm,
        low,
      };
    }, [leads]);
  /* -------------------------------------------------------
     GROUP BY DAY
  ------------------------------------------------------- */
  const dayGroups =
    useMemo(() => {
      const groups =
        new Map();
      filteredLeads.forEach(
        (lead) => {
          const key =
            dateKey(
              lead.updated_at ||
                lead.created_at
            );
          if (!groups.has(key)) {
            groups.set(
              key,
              {
                key,
                label:
                  formatDayLabel(
                    lead.updated_at ||
                      lead.created_at
                  ),
                leads: [],
              }
            );
          }
          groups
            .get(key)
            .leads.push(
              lead
            );
        }
      );
      return Array.from(
        groups.values()
      ).sort(
        (a, b) => {
          const aDate =
            new Date(
              a.leads[0]
                ?.updated_at ||
                a.leads[0]
                  ?.created_at ||
                0
            );
          const bDate =
            new Date(
              b.leads[0]
                ?.updated_at ||
                b.leads[0]
                  ?.created_at ||
                0
            );
          return (
            bDate - aDate
          );
        }
      );
    }, [filteredLeads]);
  /* -------------------------------------------------------
     TODAY
  ------------------------------------------------------- */
  const todayLeads =
    useMemo(() => {
      const localNow = new Date();
      const todayKey = [
        localNow.getFullYear(),
        String(localNow.getMonth() + 1).padStart(2, "0"),
        String(localNow.getDate()).padStart(2, "0"),
      ].join("-");
      return leads.filter(
        (lead) =>
          dateKey(
            lead.updated_at ||
              lead.created_at
          ) === todayKey
      );
    }, [leads]);
  const toggleCallCompleted = async (lead) => {
    const leadId = String(lead.lead_id);
    const nextCompleted = !Boolean(lead.call_completed);
    setUpdatingCallIds((current) => new Set(current).add(leadId));
    setError("");
    try {
      const updated = await updateLeadCallStatus(lead.lead_id, nextCompleted);
      setLeads((current) => current.map((item) =>
        String(item.lead_id) === leadId
          ? { ...item, ...(updated || {}), call_completed: nextCompleted,
              call_completed_at: nextCompleted
                ? (updated?.call_completed_at || new Date().toISOString())
                : null }
          : item
      ));
    } catch (err) {
      setError(err?.message || "Unable to update call status.");
    } finally {
      setUpdatingCallIds((current) => {
        const next = new Set(current);
        next.delete(leadId);
        return next;
      });
    }
  };
  /* -------------------------------------------------------
     OPEN LEAD
  ------------------------------------------------------- */
  const openLead = (
    leadId
  ) => {
    navigate(
      `/leads/${encodeURIComponent(
        leadId
      )}`
    );
  };
  /* -------------------------------------------------------
     RENDER
  ------------------------------------------------------- */
  return (
    <div className="dashboard-layout">
      <Sidebar />
      <main className="dashboard-main">
        <div className="dashboard-container">
          {/* HEADER */}
          <header className="dashboard-top-header">
            <div>
              <div className="dashboard-eyebrow">
                MANIA MOBILES
              </div>
              <h1>
                Lead Intelligence
              </h1>
              <p>
                Track customer intent
                and prioritize the
                leads that matter most.
              </p>
            </div>
            <div className="dashboard-header-actions">
              <button
                type="button"
                className="refresh-button"
                onClick={() => navigate("/products-campaigns")}
                aria-label="Open products and campaigns"
                title="Manage products and campaign previews"
              >
                <span aria-hidden="true">▣</span>
                <span>Products &amp; Campaigns</span>
              </button>
              <button
                type="button"
                className="today-button"
                onClick={() =>
                  setTodayOpen(
                    true
                  )
                }
              >
                <span className="today-button-dot" />
                Today
                <span className="today-button-count">
                  {todayLeads.length}
                </span>
              </button>
              <button
                type="button"
                className="refresh-button"
                onClick={
                  loadDashboard
                }
                disabled={loading}
              >
                ↻
                <span>
                  {loading
                    ? "Loading..."
                    : "Refresh"}
                </span>
              </button>
            </div>
          </header>
          {/* ERROR */}
          {error && (
            <div className="dashboard-error">
              <strong>
                Couldn't load the
                dashboard
              </strong>
              <span>
                {error}
              </span>
              <button
                type="button"
                onClick={
                  loadDashboard
                }
              >
                Retry
              </button>
            </div>
          )}
          {/* =================================================
              STATS
              DO NOT ADD A 5TH CARD.
              CSS uses repeat(4, ...).
          ================================================= */}
          <section className="dashboard-stats">
            <StatCard
              label="Total Leads"
              value={stats.total}
              description="All captured leads"
            />
            <StatCard
              label="Hot Leads"
              value={stats.hot}
              description="High purchase intent"
              variant="stat-hot"
            />
            <StatCard
              label="Warm Leads"
              value={stats.warm}
              description="Active interest"
              variant="stat-warm"
            />
            <StatCard
              label="Low Intent"
              value={stats.low}
              description="Needs nurturing"
              variant="stat-low"
            />
          </section>
          {/* LEAD WORKSPACE */}
          <section className="leads-workspace">
            <div className="workspace-header">
              <div>
                <div className="workspace-title">
                  Lead activity
                </div>
                <div className="workspace-subtitle">
                  Leads organized by day
                </div>
              </div>
              <div className="workspace-tools">
                <div className="lead-search">
                  <span>
                    ⌕
                  </span>
                  <input
                    type="text"
                    value={search}
                    onChange={(
                      event
                    ) =>
                      setSearch(
                        event.target
                          .value
                      )
                    }
                    placeholder="Search leads..."
                  />
                  {search && (
                    <button
                      type="button"
                      onClick={() =>
                        setSearch(
                          ""
                        )
                      }
                      aria-label="Clear search"
                    >
                      ×
                    </button>
                  )}
                </div>
              </div>
            </div>
            {/* CONTENT */}
            {loading ? (
              <div className="dashboard-loading">
                <div className="loading-spinner" />
                <span>
                  Loading lead activity...
                </span>
              </div>
            ) : dayGroups.length ===
              0 ? (
              <div className="dashboard-empty">
                <div className="dashboard-empty-icon">
                  ◌
                </div>
                <strong>
                  No leads found
                </strong>
                <span>
                  Try changing your
                  search or wait for
                  new WhatsApp leads.
                </span>
              </div>
            ) : (
              <div className="day-groups">
                {dayGroups.map(
                  (group) => (
                    <DaySection
                      key={
                        group.key
                      }
                      group={
                        group
                      }
                      onOpenLead={openLead}
                      onToggleCall={toggleCallCompleted}
                      updatingCallIds={updatingCallIds}
                    />
                  )
                )}
              </div>
            )}
          </section>
        </div>
      </main>
      {/* TODAY DRAWER */}
      <TodayDrawer
        open={todayOpen}
        onClose={() =>
          setTodayOpen(
            false
          )
        }
        leads={todayLeads}
        onOpenLead={
          openLead
        }
      />
    </div>
  );
}
export default Dashboard;
