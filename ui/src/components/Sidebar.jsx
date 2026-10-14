import { useEffect, useState } from "react";
import { useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { logout } from "../api";

function Sidebar() {
  const navigate = useNavigate();
  const location = useLocation();
  const [searchParams] = useSearchParams();
  const [mobileOpen, setMobileOpen] = useState(false);
  const username = sessionStorage.getItem("mania_username") || "admin";
  const isCallTracking = location.pathname === "/call-tracking";
  const activeScope = searchParams.get("scope") || "all";
  const activeIntent = searchParams.get("intent") || "";
  const activeCallFilter = searchParams.get("filter") || "all";

  useEffect(() => {
    document.body.classList.remove("sidebar-collapsed");
    return () => document.body.classList.remove("sidebar-collapsed");
  }, []);

  const navigateFilter = (scope, intent = "") => {
    setMobileOpen(false);
    const params = new URLSearchParams();
    if (scope && scope !== "all") params.set("scope", scope);
    if (intent) params.set("intent", intent);
    navigate({ pathname: "/", search: params.toString() ? `?${params}` : "" });
  };

  const navigateCallFilter = (filter = "all") => {
    setMobileOpen(false);
    const params = new URLSearchParams();
    if (filter && filter !== "all") params.set("filter", filter);
    navigate({ pathname: "/call-tracking", search: params.toString() ? `?${params}` : "" });
  };

  const handleLogout = async () => {
    try { await logout(); } catch { /* Continue clearing the client session. */ }
    sessionStorage.removeItem("mania_authenticated");
    sessionStorage.removeItem("mania_username");
    navigate("/login", { replace: true });
  };

  const itemClass = (active) => `sidebar-filter-item ${active ? "sidebar-filter-active" : ""}`;

  return (
    <>
      <button type="button" className="mobile-sidebar-trigger" onClick={() => setMobileOpen(true)} aria-label="Open navigation">
        <span /><span /><span />
      </button>
      {mobileOpen && <button type="button" className="sidebar-overlay" onClick={() => setMobileOpen(false)} aria-label="Close navigation" />}
      <aside className={`app-sidebar ${mobileOpen ? "app-sidebar-open" : ""}`}>
        <div className="sidebar-brand">
          <div className="sidebar-brand-icon">M</div>
          <div className="sidebar-brand-text"><strong>Mania Mobiles</strong><span>Lead Intelligence</span></div>
          <button type="button" className="mobile-sidebar-close" onClick={() => setMobileOpen(false)} aria-label="Close navigation">×</button>
        </div>

        <nav className="sidebar-navigation">
          <div className="sidebar-section-label">OVERVIEW</div>
          <div className="sidebar-filter-group">
            <button type="button" className={itemClass(location.pathname === "/" && activeScope === "all" && !activeIntent)} onClick={() => navigateFilter("all")}>
              <span className="sidebar-filter-icon">▦</span><span className="sidebar-filter-label">All Leads</span>
            </button>
            <button type="button" className={itemClass(isCallTracking && activeCallFilter === "all")} onClick={() => navigateCallFilter("all")}>
              <span className="sidebar-filter-icon">✓</span><span className="sidebar-filter-label">Call Tracking</span>
            </button>
          </div>

          {isCallTracking && (
            <>
              <div className="sidebar-section-label sidebar-today-label">CALL TRACKING</div>
              <div className="sidebar-filter-group" aria-label="Call tracking filters">
                <button type="button" className={itemClass(activeCallFilter === "done")} onClick={() => navigateCallFilter("done")}>
                  <span className="sidebar-filter-icon">✓</span><span className="sidebar-filter-label">Calls Done</span>
                </button>
                <button type="button" className={itemClass(activeCallFilter === "done_today")} onClick={() => navigateCallFilter("done_today")}>
                  <span className="sidebar-filter-icon">◷</span><span className="sidebar-filter-label">Calls Done Today</span>
                </button>
                <button type="button" className={itemClass(activeCallFilter === "required")} onClick={() => navigateCallFilter("required")}>
                  <span className="sidebar-filter-icon">○</span><span className="sidebar-filter-label">Calls Required</span>
                </button>
              </div>
            </>
          )}

          <div className="sidebar-section-label sidebar-today-label">TODAY</div>
          <div className="sidebar-filter-group">
            <button type="button" className={itemClass(location.pathname === "/" && activeScope === "today" && !activeIntent)} onClick={() => navigateFilter("today")}>
              <span className="sidebar-filter-icon">◷</span><span className="sidebar-filter-label">Today</span>
            </button>
            <button type="button" className={itemClass(location.pathname === "/" && activeScope === "today" && activeIntent === "hot")} onClick={() => navigateFilter("today", "hot")}>
              <span className="sidebar-filter-icon">🔥</span><span className="sidebar-filter-label">Hot Today</span>
            </button>
            <button type="button" className={itemClass(location.pathname === "/" && activeScope === "today" && activeIntent === "warm")} onClick={() => navigateFilter("today", "warm")}>
              <span className="sidebar-filter-icon">◉</span><span className="sidebar-filter-label">Warm Today</span>
            </button>
            <button type="button" className={itemClass(location.pathname === "/" && activeScope === "today" && activeIntent === "low")} onClick={() => navigateFilter("today", "low")}>
              <span className="sidebar-filter-icon">○</span><span className="sidebar-filter-label">Low Intent Today</span>
            </button>
          </div>
        </nav>

        <div className="sidebar-footer">
          <div className="sidebar-user"><div className="sidebar-user-avatar">{username.charAt(0).toUpperCase()}</div><div className="sidebar-user-info"><strong>{username}</strong><span>Administrator</span></div></div>
          <button type="button" className="sidebar-logout" onClick={handleLogout}><span>↪</span><span>Sign out</span></button>
          <div className="sidebar-security"><span className="sidebar-security-dot" /><span>Secure admin session</span></div>
        </div>
      </aside>
    </>
  );
}

export default Sidebar;
