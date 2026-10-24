import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import "./ProductsCampaigns.css";

const API = "/api/products-campaigns";

async function apiRequest(path, options = {}) {
  const response = await fetch(`${API}/${String(path).replace(/^\/+/, "")}`, {
    ...options,
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
  });

  const data = await response.json().catch(() => ({}));

  if (!response.ok) {
    const message =
      response.status === 401
        ? "Your session expired. Please sign in again."
        : response.status === 403
          ? "You don't have permission to perform this action."
          : data.detail || data.message || `Request failed (${response.status}).`;

    throw new Error(message);
  }

  return data;
}

function getProductName(product) {
  return product?.title || product?.model || "Unnamed product";
}

function getErrorMessage(error) {
  return error instanceof Error
    ? error.message
    : "Something went wrong. Please try again.";
}

function formatPrice(value) {
  if (value === null || value === undefined || value === "") {
    return "Not set";
  }

  const number = Number(value);
  if (!Number.isFinite(number)) return "Not set";

  return new Intl.NumberFormat("en-IN", {
    style: "currency",
    currency: "INR",
    maximumFractionDigits: 0,
  }).format(number);
}

function createCampaignId() {
  if (globalThis.crypto?.randomUUID) {
    return globalThis.crypto.randomUUID();
  }

  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (char) => {
    const random = Math.floor(Math.random() * 16);
    return (char === "x" ? random : (random & 3) | 8).toString(16);
  });
}

function maskPhone(value) {
  const digits = String(value || "").replace(/\D/g, "");
  if (digits.length < 5) return "Hidden";
  return `${"•".repeat(Math.max(0, digits.length - 4))}${digits.slice(-4)}`;
}

function formatDate(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString();
}

function Stat({ label, value }) {
  return (
    <div>
      <span>{label}</span>
      <strong>{value ?? 0}</strong>
    </div>
  );
}

export default function ProductsCampaigns() {
  const [products, setProducts] = useState([]);
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(true);
  const [savingId, setSavingId] = useState(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const [scope, setScope] = useState("brand");
  const [preview, setPreview] = useState(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [sending, setSending] = useState(false);
  const [campaignResult, setCampaignResult] = useState(null);

  const [responses, setResponses] = useState([]);
  const [responsesLoading, setResponsesLoading] = useState(true);
  const [responsesError, setResponsesError] = useState("");

  // Reuse the same ID when retrying a request whose outcome was uncertain.
  const campaignIdRef = useRef(null);

  const loadProducts = useCallback(async ({ quiet = false } = {}) => {
    if (!quiet) setLoading(true);
    setError("");

    try {
      const data = await apiRequest("products");
      setProducts(Array.isArray(data.products) ? data.products : []);
    } catch (err) {
      setError(getErrorMessage(err));
    } finally {
      if (!quiet) setLoading(false);
    }
  }, []);

  const loadResponses = useCallback(async () => {
    setResponsesLoading(true);
    setResponsesError("");

    try {
      const data = await apiRequest("campaign-responses?limit=100");
      setResponses(Array.isArray(data.responses) ? data.responses : []);
    } catch (err) {
      setResponsesError(getErrorMessage(err));
    } finally {
      setResponsesLoading(false);
    }
  }, []);

  useEffect(() => {
    loadProducts();
    loadResponses();
  }, [loadProducts, loadResponses]);

  const filteredProducts = useMemo(() => {
    const needle = search.trim().toLowerCase();
    if (!needle) return products;

    return products.filter((product) =>
      [
        product.title,
        product.model,
        product.brand,
        product.sku,
      ].some((value) => String(value || "").toLowerCase().includes(needle))
    );
  }, [products, search]);

  const enabledCampaignCount = useMemo(
    () => products.filter((product) => Boolean(product.campaign_enabled)).length,
    [products]
  );

  const totalBrandInterest = useMemo(
    () =>
      products.reduce(
        (total, product) =>
          total + (Number(product.brand_interest_count) || 0),
        0
      ),
    [products]
  );

  const editLocal = useCallback((id, field, value) => {
    setProducts((current) =>
      current.map((product) =>
        product.id === id ? { ...product, [field]: value } : product
      )
    );
  }, []);

  async function saveProduct(product) {
    setSavingId(product.id);
    setError("");
    setNotice("");

    const numericPrice =
      product.price === "" || product.price == null
        ? null
        : Number(product.price);

    if (
      numericPrice !== null &&
      (!Number.isFinite(numericPrice) || numericPrice < 0)
    ) {
      setError("Enter a valid, non-negative product price.");
      setSavingId(null);
      return;
    }

    try {
      await apiRequest(`products/${encodeURIComponent(product.id)}`, {
        method: "PATCH",
        body: JSON.stringify({
          price: numericPrice,
          battery_health: product.battery_health ?? "",
          campaign_enabled: Boolean(product.campaign_enabled),
        }),
      });

      setNotice(
        `Saved ${getProductName(product)}. If its price changed, verify it again before running a campaign.`
      );
      await loadProducts({ quiet: true });
    } catch (err) {
      setError(getErrorMessage(err));
    } finally {
      setSavingId(null);
    }
  }

  async function previewCampaign(product) {
    setPreviewLoading(true);
    setError("");
    setNotice("");
    setPreview(null);
    setCampaignResult(null);
    campaignIdRef.current = null;

    try {
      const data = await apiRequest("campaign-preview", {
        method: "POST",
        body: JSON.stringify({
          product_id: product.id,
          audience_scope: scope,
        }),
      });

      setPreview(data);
      campaignIdRef.current = createCampaignId();
    } catch (err) {
      setError(getErrorMessage(err));
    } finally {
      setPreviewLoading(false);
    }
  }

  async function sendCampaign() {
    if (
      sending ||
      !preview?.send_enabled ||
      !preview?.product?.id ||
      Number(preview.freeform_window_count) < 1
    ) {
      return;
    }

    const eligibleCount = Number(preview.freeform_window_count) || 0;
    const outsideWindowCount =
      Number(preview.approved_template_required_count) || 0;

    const details = outsideWindowCount
      ? ` ${outsideWindowCount} opted-in customer(s) outside the 24-hour window will be skipped.`
      : "";

    const confirmed = window.confirm(
      `Send this product offer to up to ${eligibleCount} opted-in customer(s) with an open 24-hour WhatsApp window?${details} This action sends real WhatsApp messages.`
    );

    if (!confirmed) return;

    setSending(true);
    setError("");
    setNotice("");

    try {
      const campaignId = campaignIdRef.current || createCampaignId();
      campaignIdRef.current = campaignId;

      const result = await apiRequest("campaign-send", {
        method: "POST",
        body: JSON.stringify({
          product_id: preview.product.id,
          audience_scope: scope,
          campaign_id: campaignId,
        }),
      });

      setCampaignResult(result);
      setNotice(
        `Campaign processing finished. ${Number(result.sent_count) || 0} message(s) sent.`
      );

      await Promise.all([
        loadProducts({ quiet: true }),
        loadResponses(),
      ]);
    } catch (err) {
      // Keep the campaign ID so a retry can use the same idempotency key.
      setError(getErrorMessage(err));
    } finally {
      setSending(false);
    }
  }

  function closePreview() {
    if (sending) return;
    setPreview(null);
    setCampaignResult(null);
    campaignIdRef.current = null;
  }

  return (
    <main className="pc-page">
      <header className="pc-header">
        <div>
          <p className="pc-eyebrow">MANIA MOBILES · GROWTH</p>
          <h1>Products &amp; Campaigns</h1>
          <p className="pc-subtitle">
            Manage product details, understand demand, and preview a compliant
            WhatsApp audience before sending a campaign.
          </p>
        </div>

        <button
          className="pc-secondary"
          type="button"
          onClick={() => loadProducts()}
          disabled={loading}
        >
          {loading ? "Refreshing…" : "Refresh catalog"}
        </button>
      </header>

      <section className="pc-summary" aria-label="Catalog summary">
        <Stat label="Products" value={products.length} />
        <Stat label="Campaign enabled" value={enabledCampaignCount} />
        <div>
          <span>Brand interest signals</span>
          <strong>{totalBrandInterest}</strong>
          <small>Counts are per product and may overlap.</small>
        </div>
      </section>

      {error && (
        <div className="pc-alert pc-alert-error" role="alert">
          {error}
        </div>
      )}

      {notice && (
        <div className="pc-alert pc-alert-success" role="status">
          {notice}
        </div>
      )}

      <div className="pc-toolbar">
        <input
          type="search"
          aria-label="Search products"
          placeholder="Search brand, model, SKU…"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
        />
        <span>
          {filteredProducts.length}{" "}
          {filteredProducts.length === 1 ? "product" : "products"}
        </span>
      </div>

      {loading ? (
        <div className="pc-empty">Loading products from Supabase…</div>
      ) : filteredProducts.length === 0 ? (
        <div className="pc-empty">
          {search.trim()
            ? "No products match your search."
            : "No products found in the catalog."}
        </div>
      ) : (
        <div className="pc-product-grid">
          {filteredProducts.map((product) => (
            <article className="pc-card" key={product.id}>
              <div className="pc-card-top">
                {product.image_url ? (
                  <img
                    src={product.image_url}
                    alt={getProductName(product)}
                    loading="lazy"
                    onError={(event) => {
                      event.currentTarget.style.visibility = "hidden";
                    }}
                  />
                ) : (
                  <div className="pc-image-placeholder" aria-hidden="true">
                    MM
                  </div>
                )}

                <div className="pc-product-heading">
                  <h2>{getProductName(product)}</h2>
                  <p>
                    {[product.brand, product.storage, product.color]
                      .filter(Boolean)
                      .join(" · ")}
                  </p>
                  <span
                    className={`pc-stock pc-stock-${String(
                      product.stock_status || "unknown"
                    )
                      .toLowerCase()
                      .replace(/[^a-z]/g, "")}`}
                  >
                    {product.stock_status || "Stock unknown"}
                  </span>
                </div>
              </div>

              <div className="pc-fields">
                <label>
                  Current selling price (₹)
                  <input
                    type="number"
                    min="0"
                    step="1"
                    inputMode="numeric"
                    value={product.price ?? ""}
                    placeholder="Enter price"
                    onChange={(event) =>
                      editLocal(product.id, "price", event.target.value)
                    }
                  />
                  <small>Displayed price: {formatPrice(product.price)}</small>
                </label>

                <label>
                  Battery health
                  <input
                    type="text"
                    maxLength={40}
                    placeholder="e.g. 92% / N/A"
                    value={product.battery_health ?? ""}
                    onChange={(event) =>
                      editLocal(
                        product.id,
                        "battery_health",
                        event.target.value
                      )
                    }
                  />
                </label>
              </div>

              <div className="pc-interest">
                <div>
                  <span>Brand interest</span>
                  <strong>{Number(product.brand_interest_count) || 0}</strong>
                </div>
                <div>
                  <span>Product interest</span>
                  <strong>
                    {Number(product.product_interest_count) || 0}
                  </strong>
                </div>
              </div>

              <label className="pc-toggle">
                <span>
                  <strong>Campaign enabled</strong>
                  <small>Enable this product for campaign planning.</small>
                </span>
                <input
                  type="checkbox"
                  checked={Boolean(product.campaign_enabled)}
                  onChange={(event) =>
                    editLocal(
                      product.id,
                      "campaign_enabled",
                      event.target.checked
                    )
                  }
                />
              </label>

              <div className="pc-actions">
                <button
                  type="button"
                  onClick={() => saveProduct(product)}
                  disabled={savingId !== null || previewLoading}
                >
                  {savingId === product.id ? "Saving…" : "Save changes"}
                </button>

                <button
                  className="pc-secondary"
                  type="button"
                  onClick={() => previewCampaign(product)}
                  disabled={
                    previewLoading ||
                    savingId !== null ||
                    !product.campaign_enabled
                  }
                >
                  {previewLoading ? "Preparing…" : "Preview campaign"}
                </button>
              </div>

              {product.price_verified !== true && (
                <p className="pc-warning">
                  Price verification is not confirmed. Verify the price with
                  the store before promoting this product.
                </p>
              )}
            </article>
          ))}
        </div>
      )}

      <section className="pc-responses" aria-labelledby="pc-responses-title">
        <div className="pc-responses-header">
          <div>
            <p className="pc-eyebrow">CAMPAIGN ACTIVITY</p>
            <h2 id="pc-responses-title">Customer responses</h2>
            <p className="pc-subtitle">
              Recent campaign replies. Phone numbers are masked in this view.
            </p>
          </div>

          <button
            className="pc-secondary"
            type="button"
            onClick={loadResponses}
            disabled={responsesLoading}
          >
            {responsesLoading ? "Refreshing…" : "Refresh responses"}
          </button>
        </div>

        {responsesError && (
          <div className="pc-alert pc-alert-error" role="alert">
            {responsesError}
          </div>
        )}

        {responsesLoading ? (
          <div className="pc-empty">Loading campaign responses…</div>
        ) : responses.length === 0 ? (
          <div className="pc-empty">No campaign responses recorded yet.</div>
        ) : (
          <div className="pc-table-wrap">
            <table className="pc-table">
              <thead>
                <tr>
                  <th>Product ID</th>
                  <th>Customer</th>
                  <th>Response</th>
                  <th>Received</th>
                </tr>
              </thead>
              <tbody>
                {responses.map((item) => (
                  <tr
                    key={
                      item.id ||
                      `${item.campaign_id}-${item.phone_number}-${item.response}`
                    }
                  >
                    <td>{item.product_id ?? "—"}</td>
                    <td>{maskPhone(item.phone_number)}</td>
                    <td>
                      <span
                        className={`pc-response-tag ${
                          item.response === "interested"
                            ? "pc-response-positive"
                            : "pc-response-neutral"
                        }`}
                      >
                        {String(item.response || "unknown").replace(/_/g, " ")}
                      </span>
                    </td>
                    <td>{formatDate(item.responded_at || item.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {preview && (
        <div
          className="pc-modal-backdrop"
          role="presentation"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) closePreview();
          }}
        >
          <section
            className="pc-modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="pc-preview-title"
          >
            <button
              className="pc-close"
              type="button"
              onClick={closePreview}
              disabled={sending}
              aria-label="Close preview"
            >
              ×
            </button>

            <p className="pc-eyebrow">CAMPAIGN PREVIEW</p>
            <h2 id="pc-preview-title">
              {preview.product?.title || "Campaign audience"}
            </h2>

            <label className="pc-scope">
              Audience
              <select
                value={scope}
                disabled={sending}
                onChange={(event) => {
                  setScope(event.target.value);
                  setPreview(null);
                  setCampaignResult(null);
                  campaignIdRef.current = null;
                }}
              >
                <option value="brand">People interested in this brand</option>
                <option value="product">People interested in this product</option>
              </select>
            </label>

            <div className="pc-preview-stats">
              <Stat
                label="Matching interest"
                value={preview.matched_interest_count}
              />
              <Stat
                label="Marketing opt-in"
                value={preview.marketing_opted_in_count}
              />
              <Stat
                label="24-hour window open"
                value={preview.freeform_window_count}
              />
              <Stat
                label="Approved template required"
                value={preview.approved_template_required_count}
              />
            </div>

            <div
              className={`pc-alert ${
                preview.send_enabled ? "pc-alert-info" : "pc-alert-error"
              }`}
            >
              {preview.send_block_reason ||
                (Number(preview.freeform_window_count) > 0
                  ? "Ready to send to opted-in customers with an open 24-hour WhatsApp window. Out-of-window recipients will be skipped."
                  : "No eligible recipients currently have an open 24-hour WhatsApp window. Configure an approved Meta marketing template before contacting out-of-window recipients.")}
            </div>

            {campaignResult ? (
              <div className="pc-result" role="status">
                <h3>Campaign result</h3>
                <div className="pc-preview-stats">
                  <Stat label="Matched" value={campaignResult.matched_count} />
                  <Stat label="Eligible" value={campaignResult.eligible_count} />
                  <Stat label="Sent" value={campaignResult.sent_count} />
                  <Stat label="Skipped" value={campaignResult.skipped_count} />
                </div>

                {Number(campaignResult.failed_count) > 0 && (
                  <p className="pc-warning">
                    {campaignResult.failed_count} recipient(s) failed or were
                    skipped. Check dispatch status and server logs before
                    retrying.
                  </p>
                )}

                <p className="pc-footnote">
                  Campaign ID: {campaignResult.campaign_id || "—"}
                </p>
              </div>
            ) : (
              <button
                className="pc-send-button"
                type="button"
                onClick={sendCampaign}
                disabled={
                  sending ||
                  !preview.send_enabled ||
                  Number(preview.freeform_window_count) < 1
                }
              >
                {sending
                  ? "Sending campaign…"
                  : `Send to ${
                      Number(preview.freeform_window_count) || 0
                    } eligible customer(s)`}
              </button>
            )}

            <p className="pc-footnote">
              Sending is manual. The backend must verify the current price,
              campaign status, explicit consent, opt-out status, and WhatsApp
              window again at send time. Free-form messages are sent only inside
              the 24-hour window; out-of-window recipients are skipped until an
              approved Meta marketing template is integrated.
            </p>
          </section>
        </div>
      )}
    </main>
  );
}