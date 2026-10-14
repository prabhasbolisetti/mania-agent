import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import Sidebar from "../components/Sidebar";
import { getLeads } from "../api";
import "./CallTracking.css";

function getPhone(lead) {
  const customer = lead?.customer || lead?.customer_data || {};
  return String(customer.phone || customer.phone_number || customer.mobile || lead.phone_number || "");
}

function getProductName(lead) {
  const customer = lead?.customer || lead?.customer_data || {};
  return customer.product_interest || customer.product || lead.product_interest || "Product not specified";
}

function isCompletedToday(lead) {
  if (!lead.call_completed || !lead.call_completed_at) return false;
  const completedAt = new Date(lead.call_completed_at);
  if (Number.isNaN(completedAt.getTime())) return false;
  return completedAt.toDateString() === new Date().toDateString();
}

async function copyPhone(phone) {
  if (!phone) return;
  try { await navigator.clipboard.writeText(phone); } catch { /* Clipboard permission may be unavailable. */ }
}

function CallLeadTable({ leads, completed, emptyMessage }) {
  if (!leads.length) {
    return <div className="call-empty-state">{emptyMessage || (completed ? "No completed calls yet." : "You have no pending calls.")}</div>;
  }

  return (
    <div className="call-table-wrap">
      <table className="call-tracking-table">
        <thead>
          <tr>
            <th>Mobile number</th><th>Product</th><th>Intent</th>
            <th>{completed ? "Completed at" : "Lead added"}</th><th>Conversation</th>
          </tr>
        </thead>
        <tbody>
          {leads.map((lead) => {
            const phone = getPhone(lead);
            const when = completed ? lead.call_completed_at : (lead.created_at || lead.updated_at);
            return (
              <tr key={lead.lead_id}>
                <td>
                  <div className="call-phone-value">
                    <strong>{phone || "No mobile number"}</strong>
                    {phone && (
                      <button type="button" className="copy-phone-button" onClick={() => copyPhone(phone)} aria-label={`Copy ${phone}`} title="Copy mobile number">
                        <svg viewBox="0 0 24 24" aria-hidden="true">
                          <rect x="8" y="8" width="12" height="12" rx="2" fill="none" stroke="currentColor" strokeWidth="1.8" />
                          <path d="M16 8V5a1 1 0 0 0-1-1H5a1 1 0 0 0-1 1v10a1 1 0 0 0 1 1h3" fill="none" stroke="currentColor" strokeWidth="1.8" />
                        </svg>
                      </button>
                    )}
                  </div>
                </td>
                <td>{getProductName(lead)}</td>
                <td><span className={`call-intent-pill ${String(lead.intent_status || lead.intent?.status || "low").toLowerCase()}`}>{lead.intent_status || lead.intent?.status || "low"}</span></td>
                <td>{when ? new Date(when).toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" }) : "—"}</td>
                <td><Link className="lead-conversation-button" to={`/leads/${encodeURIComponent(lead.lead_id)}#conversation`} aria-label={`View conversation for ${phone || "this lead"}`}>Conversation <span aria-hidden="true">↗</span></Link></td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function CallTracking() {
  const [searchParams] = useSearchParams();
  const activeFilter = searchParams.get("filter") || "all";
  const [leads, setLeads] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const response = await getLeads();
      setLeads(Array.isArray(response) ? response : (response?.leads || []));
    } catch (err) {
      setError(err?.message || "Unable to load call tracking data.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const completed = useMemo(() => leads.filter((lead) => Boolean(lead.call_completed)), [leads]);
  const pending = useMemo(() => leads.filter((lead) => !lead.call_completed), [leads]);
  const completedTodayLeads = useMemo(() => completed.filter(isCompletedToday), [completed]);
  const sortedCompleted = useMemo(
    () => [...completed].sort((a, b) => new Date(b.call_completed_at || 0) - new Date(a.call_completed_at || 0)),
    [completed]
  );

  const filterConfig = {
    all: { title: "All Call Tracking", description: "Review calls that are completed and still required." },
    done: { title: "Calls Done", description: "All leads whose calls have been marked completed." },
    done_today: { title: "Calls Done Today", description: "Calls marked completed today." },
    required: { title: "Calls Required", description: "Leads that still need a follow-up call." },
  };
  const page = filterConfig[activeFilter] || filterConfig.all;

  return (
    <div className="dashboard-layout call-tracking-layout">
      <Sidebar />
      <main className="dashboard-main">
        <div className="dashboard-container call-tracking-container">
          <header className="dashboard-top-header call-tracking-header">
            <div>
              <div className="dashboard-eyebrow">MANIA MOBILES</div>
              <h1>{page.title}</h1>
              <p>{page.description}</p>
            </div>
            <button type="button" className="call-refresh-button" onClick={load} disabled={loading}>{loading ? "Refreshing…" : "↻ Refresh"}</button>
          </header>

          {error && <div className="call-error" role="alert">{error}</div>}

          <section className="call-stats-grid" aria-label="Call statistics">
            <article className="call-stat-card"><span className="call-stat-label">Calls done</span><strong>{loading ? "—" : completed.length}</strong><span className="call-stat-note">All completed calls</span></article>
            <article className="call-stat-card"><span className="call-stat-label">Calls done today</span><strong>{loading ? "—" : completedTodayLeads.length}</strong><span className="call-stat-note">Completed since local midnight</span></article>
            <article className="call-stat-card"><span className="call-stat-label">Calls required</span><strong>{loading ? "—" : pending.length}</strong><span className="call-stat-note">Leads still to contact</span></article>
          </section>

          {loading ? <div className="call-loading">Loading call tracking…</div> : (
            <>
              {(activeFilter === "all" || activeFilter === "required") && (
                <section className="call-list-card">
                  <div className="call-list-heading"><div><h2>Calls Required</h2><p>Leads that still need a call</p></div><span className="call-count pending-count">{pending.length}</span></div>
                  <CallLeadTable leads={pending} completed={false} emptyMessage="There are no calls requiring follow-up." />
                </section>
              )}

              {(activeFilter === "all" || activeFilter === "done" || activeFilter === "done_today") && (
                <section className="call-list-card">
                  <div className="call-list-heading">
                    <div><h2>{activeFilter === "done_today" ? "Calls Done Today" : "Calls Done"}</h2><p>{activeFilter === "done_today" ? "Calls completed today" : "Leads marked as called"}</p></div>
                    <span className="call-count completed-count">{(activeFilter === "done_today" ? completedTodayLeads : completed).length}</span>
                  </div>
                  <CallLeadTable
                    leads={activeFilter === "done_today" ? completedTodayLeads : sortedCompleted}
                    completed
                    emptyMessage={activeFilter === "done_today" ? "No calls have been completed today." : "No completed calls yet."}
                  />
                </section>
              )}
            </>
          )}
        </div>
      </main>
    </div>
  );
}

export default CallTracking;
