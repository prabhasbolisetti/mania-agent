
import { useEffect, useMemo, useState, useCallback } from "react";
import "./ProductsCampaigns.css";

const API = "/api/products-campaigns";

/**
 * Normalize appended paths so "/products" and "products"
 * both produce a single-slash API URL.
 */
function normalizeApiPath(path = "") {
  const cleanPath = String(path).replace(/^\/+/, "");
  return cleanPath ? `/${cleanPath}` : "";
}

async function apiRequest(path = "", options = {}) {
  const url = `${API}${normalizeApiPath(path)}`;

  const response = await fetch(url, {
    ...options,
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
  });

  const body = await response.json().catch(() => ({}));

  if (!response.ok) {
    if (response.status === 401) {
      throw new Error("Your session expired. Please sign in again.");
    }

    if (response.status === 403) {
      throw new Error("You don't have permission to perform this action.");
    }

    throw new Error(
      body.detail ||
        body.message ||
        `Request failed with status ${response.status}.`
    );
  }

  return body;
}

function formatPrice(value) {
  if (value === null || value === undefined || value === "") {
    return "Not set";
  }

  const number = Number(value);

  if (!Number.isFinite(number)) {
    return "Not set";
  }

  return new Intl.NumberFormat("en-IN", {
    style: "currency",
    currency: "INR",
    maximumFractionDigits: 0,
  }).format(number);
}

function getProductName(product) {
  return product.title || product.model || "Unnamed product";
}

function getErrorMessage(error) {
  return error instanceof Error
    ? error.message
    : "Something went wrong. Please try again.";
}

export default function ProductsCampaigns() {
  const [products, setProducts] = useState([]);
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(true);
  const [savingId, setSavingId] = useState(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [preview, setPreview] = useState(null);
  const [scope, setScope] = useState("brand");
  const [previewLoading, setPreviewLoading] = useState(false);

  const loadProducts = useCallback(async () => {
    setLoading(true);
    setError("");

    try {
      const data = await apiRequest("/products");
      setProducts(Array.isArray(data.products) ? data.products : []);
    } catch (err) {
      setError(getErrorMessage(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadProducts();
  }, [loadProducts]);

  const filteredProducts = useMemo(() => {
    const needle = search.trim().toLowerCase();

    if (!needle) {
      return products;
    }

    return products.filter((product) =>
      [
        product.title,
        product.model,
        product.brand,
        product.sku,
      ].some((value) =>
        String(value || "").toLowerCase().includes(needle)
      )
    );
  }, [products, search]);

  const editLocal = useCallback((id, field, value) => {
    setProducts((current) =>
      current.map((product) =>
        product.id === id
          ? { ...product, [field]: value }
          : product
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
      const body = {
        price: numericPrice,
        battery_health: product.battery_health ?? "",
        campaign_enabled: Boolean(product.campaign_enabled),
      };

      await apiRequest(`/products/${encodeURIComponent(product.id)}`, {
        method: "PATCH",
        body: JSON.stringify(body),
      });

      setNotice(`Saved ${getProductName(product)}.`);
      await loadProducts();
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

    try {
      const data = await apiRequest("/campaign-preview", {
        method: "POST",
        body: JSON.stringify({
          product_id: product.id,
          audience_scope: scope,
        }),
      });

      setPreview(data);
    } catch (err) {
      setError(getErrorMessage(err));
    } finally {
      setPreviewLoading(false);
    }
  }

  const enabledCampaignCount = products.filter(
    (product) => Boolean(product.campaign_enabled)
  ).length;

  const totalBrandInterest = products.reduce(
    (sum, product) =>
      sum + (Number(product.brand_interest_count) || 0),
    0
  );

  return (
    <main className="pc-page">
      <header className="pc-header">
        <div>
          <p className="pc-eyebrow">MANIA MOBILES · GROWTH</p>
          <h1>Products &amp; Campaigns</h1>
          <p className="pc-subtitle">
            Manage product details, understand demand, and preview a
            compliant WhatsApp audience.
          </p>
        </div>

        <button
          className="pc-secondary"
          type="button"
          onClick={loadProducts}
          disabled={loading}
        >
          {loading ? "Refreshing…" : "Refresh catalog"}
        </button>
      </header>

      <section className="pc-summary">
        <div>
          <span>Products</span>
          <strong>{products.length}</strong>
        </div>

        <div>
          <span>Campaign enabled</span>
          <strong>{enabledCampaignCount}</strong>
        </div>

        <div>
          <span>Brand interest signals</span>
          <strong>{totalBrandInterest}</strong>
          <small>Counts are per product and can overlap.</small>
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
                      event.currentTarget.style.display = "none";
                    }}
                  />
                ) : (
                  <div className="pc-image-placeholder">MM</div>
                )}

                <div className="pc-product-heading">
                  <h2>{getProductName(product)}</h2>

                  <p>
                    {[
                      product.brand,
                      product.storage,
                      product.color,
                    ]
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
                    onChange={(event) =>
                      editLocal(
                        product.id,
                        "price",
                        event.target.value
                      )
                    }
                    placeholder="Enter price"
                  />
                  <small>
                    Displayed price: {formatPrice(product.price)}
                  </small>
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
                  <strong>
                    {Number(product.brand_interest_count) || 0}
                  </strong>
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
                  <small>
                    Enable this product for campaign planning.
                  </small>
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
                  disabled={savingId === product.id || previewLoading}
                >
                  {savingId === product.id
                    ? "Saving…"
                    : "Save changes"}
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
                  {previewLoading
                    ? "Preparing…"
                    : "Preview campaign"}
                </button>
              </div>

              {product.price_verified !== true && (
                <p className="pc-warning">
                  Price verification is not confirmed. Verify the price
                  with the store before promoting this product.
                </p>
              )}
            </article>
          ))}
        </div>
      )}

      {preview && (
        <div
          className="pc-modal-backdrop"
          role="presentation"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) {
              setPreview(null);
            }
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
              onClick={() => setPreview(null)}
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
                onChange={(event) => {
                  setScope(event.target.value);
                  setPreview(null);
                }}
              >
                <option value="brand">
                  People interested in this brand
                </option>
                <option value="product">
                  People interested in this product
                </option>
              </select>
            </label>

            <div className="pc-preview-stats">
              <div>
                <span>Matching interest</span>
                <strong>
                  {preview.matched_interest_count ?? 0}
                </strong>
              </div>

              <div>
                <span>Marketing opt-in</span>
                <strong>
                  {preview.marketing_opted_in_count ?? 0}
                </strong>
              </div>

              <div>
                <span>24-hour window open</span>
                <strong>
                  {preview.freeform_window_count ?? 0}
                </strong>
              </div>

              <div>
                <span>Approved template needed</span>
                <strong>
                  {preview.approved_template_required_count ?? 0}
                </strong>
              </div>
            </div>

            <div className="pc-alert pc-alert-info">
              {preview.send_block_reason ||
                "This is a preview only. Campaign sending is disabled."}
            </div>

            <button
              className="pc-send-disabled"
              type="button"
              disabled
              title="Sending is intentionally disabled in Phase 1"
            >
              Send campaign — not enabled yet
            </button>

            <p className="pc-footnote">
              No messages have been sent. The preview contains counts
              only and does not expose customer phone numbers.
            </p>
          </section>
        </div>
      )}
    </main>
  );
}
