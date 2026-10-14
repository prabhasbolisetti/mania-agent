import { Link } from "react-router-dom";


function formatCustomerName(customer) {
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


function formatCustomerPhone(customer, lead = null) {
  return (
    customer?.phone_number ||
    customer?.phone ||
    customer?.mobile ||
    lead?.phone_number ||
    ""
  );
}


function formatProduct(customer) {
  return (
    customer?.product_interest ||
    customer?.product ||
    customer?.model ||
    "—"
  );
}


function formatBudget(budget) {
  if (
    budget === null ||
    budget === undefined ||
    budget === ""
  ) {
    return "—";
  }


  if (
    typeof budget === "string"
  ) {
    try {
      budget = JSON.parse(budget);
    } catch {
      return budget;
    }
  }


  if (
    typeof budget === "object" &&
    budget.min !== undefined &&
    budget.max !== undefined
  ) {
    return (
      `₹${Number(
        budget.min
      ).toLocaleString("en-IN")}` +
      ` – ₹${Number(
        budget.max
      ).toLocaleString("en-IN")}`
    );
  }


  if (
    typeof budget === "object"
  ) {
    return Object.entries(
      budget
    )
      .map(
        ([key, value]) =>
          `${key}: ${value}`
      )
      .join(", ");
  }


  return String(budget);
}


function getIntentStatus(lead) {
  const status =
    lead?.intent?.status ||
    lead?.qualification?.status ||
    "low";

  return String(
    status
  ).toLowerCase();
}


function getIntentScore(lead) {
  return Number(
    lead?.intent?.score ??
      lead?.qualification?.score ??
      0
  );
}


function formatDate(value) {
  if (!value) {
    return "—";
  }


  const date =
    new Date(value);


  if (
    Number.isNaN(
      date.getTime()
    )
  ) {
    return value;
  }


  return date.toLocaleString(
    "en-IN",
    {
      day: "2-digit",
      month: "short",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    }
  );
}


function LeadTable({
  leads = [],
}) {
  if (!leads.length) {
    return (
      <div className="empty-state">

        <div className="empty-icon">
          ○
        </div>

        <h3>
          No leads found
        </h3>

        <p>
          Leads created by the
          WhatsApp agent will
          appear here.
        </p>

      </div>
    );
  }


  return (
    <div className="table-wrapper">

      <table className="lead-table">

        <thead>

          <tr>

            <th>
              Customer
            </th>

            <th>
              Product
            </th>

            <th>
              Budget
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

            <th />

          </tr>

        </thead>


        <tbody>

          {leads.map(
            (lead) => {

              const customer =
                lead?.customer ||
                lead?.customer_data ||
                {};

              const status =
                getIntentStatus(
                  lead
                );

              const score =
                getIntentScore(
                  lead
                );

              const name =
                formatCustomerName(
                  customer
                );

              const phone =
                formatCustomerPhone(
                  customer,
                  lead
                );

              const product =
                formatProduct(
                  customer
                );

              const budget =
                formatBudget(
                  customer?.budget
                );


              return (
                <tr
                  key={
                    lead.lead_id
                  }
                >

                  <td>

                    <div className="customer-cell">

                      <div className="table-avatar">

                        {name
                          .charAt(0)
                          .toUpperCase()}

                      </div>


                      <div>

                        <strong>
                          {name}
                        </strong>


                        {phone && (
                          <span>
                            {phone}
                          </span>
                        )}

                      </div>

                    </div>

                  </td>


                  <td>

                    <div className="product-cell">

                      {product}

                    </div>


                    {customer?.storage && (
                      <div className="table-secondary">

                        {customer.storage}

                      </div>
                    )}

                  </td>


                  <td>

                    <span className="table-budget">

                      {budget}

                    </span>

                  </td>


                  <td>

                    <span
                      className={
                        `status-pill ${status}`
                      }
                    >

                      {status}

                    </span>

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

                    {formatDate(
                      lead.updated_at ||
                        lead.created_at
                    )}

                  </td>


                  <td>

                    <Link
                      className="view-link"
                      to={
                        `/leads/${encodeURIComponent(
                          lead.lead_id
                        )}`
                      }
                    >

                      View

                    </Link>

                  </td>

                </tr>
              );
            }
          )}

        </tbody>

      </table>

    </div>
  );
}


export default LeadTable;