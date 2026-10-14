import { useEffect, useState } from "react";

import {
  Link,
  useParams,
} from "react-router-dom";

import Sidebar from "../components/Sidebar";

import { getLead } from "../api";


function prettyKey(key) {
  return String(key)
    .replace(/_/g, " ")
    .replace(/\b\w/g, (char) =>
      char.toUpperCase()
    );
}


function formatValue(value) {
  if (
    value === null ||
    value === undefined ||
    value === ""
  ) {
    return "—";
  }


  if (
    typeof value === "object"
  ) {
    return JSON.stringify(
      value,
      null,
      2
    );
  }


  return String(value);
}


function formatBudget(value) {
  if (
    value === null ||
    value === undefined ||
    value === ""
  ) {
    return "—";
  }


  let budget =
    value;


  if (
    typeof budget === "string"
  ) {
    try {
      budget =
        JSON.parse(
          budget
        );
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


  return formatValue(
    budget
  );
}


function getCustomerName(customer) {
  return (
    customer?.name ||
    customer?.customer_name ||
    customer?.full_name ||
    customer?.customerName ||
    customer?.phone ||
    customer?.phone_number ||
    "Unknown Customer"
  );
}


function getCustomerPhone(customer) {
  return (
    customer?.phone ||
    customer?.phone_number ||
    customer?.mobile ||
    ""
  );
}


function getIntentClass(status) {
  const value =
    String(
      status || "low"
    ).toLowerCase();


  if (
    value === "hot"
  ) {
    return "hot";
  }


  if (
    value === "warm"
  ) {
    return "warm";
  }


  if (
    value === "high"
  ) {
    return "high";
  }


  return "low";
}


function getIntentEmoji(status) {
  const value =
    String(
      status || "low"
    ).toLowerCase();


  if (
    value === "hot"
  ) {
    return "🔥";
  }


  if (
    value === "warm"
  ) {
    return "🟠";
  }


  if (
    value === "high"
  ) {
    return "🟢";
  }


  return "⚪";
}


function getSenderType(turn) {
  const sender =
    String(
      turn?.sender_type ||
        turn?.sender ||
        turn?.role ||
        ""
    ).toLowerCase();


  if (
    sender.includes(
      "customer"
    ) ||
    sender.includes(
      "user"
    ) ||
    sender === "incoming"
  ) {
    return "customer";
  }


  if (
    sender.includes(
      "agent"
    ) ||
    sender.includes(
      "assistant"
    ) ||
    sender === "outgoing"
  ) {
    return "agent";
  }


  return "customer";
}


function getMessageText(turn) {
  return (
    turn?.message ||
    turn?.message_text ||
    turn?.content ||
    turn?.text ||
    JSON.stringify(
      turn
    )
  );
}


function formatMessageTime(
  value
) {
  if (!value) {
    return "";
  }


  const date =
    new Date(value);


  if (
    Number.isNaN(
      date.getTime()
    )
  ) {
    return "";
  }


  return date.toLocaleString(
    "en-IN",
    {
      day: "2-digit",
      month: "short",
      hour: "2-digit",
      minute: "2-digit",
    }
  );
}


function LeadDetails() {

  const {
    leadId,
  } = useParams();


  const [
    lead,
    setLead,
  ] = useState(null);


  const [
    loading,
    setLoading,
  ] = useState(true);


  const [
    error,
    setError,
  ] = useState("");


  useEffect(() => {

    const loadLead =
      async () => {

        setLoading(true);

        setError("");


        try {

          const data =
            await getLead(
              leadId
            );


          setLead(
            data?.lead ||
              data
          );

        } catch (err) {

          setError(
            err?.data?.detail ||
              err?.message ||
              "Unable to load lead."
          );

        } finally {

          setLoading(false);

        }
      };


    loadLead();

  }, [leadId]);


  /*
   * LOADING
   */

  if (loading) {

    return (
      <div className="lead-page">

        <Sidebar />

        <main className="lead-page-main">

          <div className="lead-page-container">

            <div className="lead-loading-card">

              <div className="lead-loading-spinner" />

              <span>
                Loading lead...
              </span>

            </div>

          </div>

        </main>

      </div>
    );
  }


  /*
   * ERROR
   */

  if (
    error ||
    !lead
  ) {

    return (
      <div className="lead-page">

        <Sidebar />

        <main className="lead-page-main">

          <div className="lead-page-container">

            <Link
              to="/"
              className="lead-back-button"
            >
              <span>
                ←
              </span>

              Back to leads
            </Link>


            <div className="lead-error-card">

              <div className="lead-error-icon">
                !
              </div>


              <h1>
                Unable to load lead
              </h1>


              <p>
                {error ||
                  "Lead not found."}
              </p>


              <Link
                to="/"
                className="lead-error-link"
              >
                Return to all leads
              </Link>

            </div>

          </div>

        </main>

      </div>
    );
  }


  const customer =
    lead.customer ||
    {};


  const intent =
    lead.intent ||
    {};


  const qualification =
    lead.qualification ||
    {};


  const history =
    Array.isArray(
      lead.conversation_history
    )
      ? lead.conversation_history
      : [];


  const customerName =
    getCustomerName(
      customer
    );


  const customerPhone =
    getCustomerPhone(
      customer
    );


  const intentStatus =
    String(
      intent.status ||
        qualification.status ||
        "low"
    ).toLowerCase();


  const intentClass =
    getIntentClass(
      intentStatus
    );


  const intentEmoji =
    getIntentEmoji(
      intentStatus
    );


  const intentScore =
    Number(
      intent.score ??
        qualification.score ??
        0
    );


  /*
   * CUSTOMER FIELDS
   */

  const customerEntries =
    Object.entries(
      customer
    );


  return (
    <div className="lead-page">

      <Sidebar />


      <main className="lead-page-main">

        <div className="lead-page-container">


          {/* BACK */}

          <div className="lead-topbar">

            <Link
              to="/"
              className="lead-back-button"
            >

              <span>
                ←
              </span>

              Back to leads

            </Link>

          </div>


          {/* HEADER */}

          <header className="lead-hero">

            <div className="lead-hero-left">

              <div className="lead-avatar">

                {customerName
                  .charAt(0)
                  .toUpperCase()}

              </div>


              <div className="lead-hero-info">

                <div className="lead-eyebrow">

                  LEAD DETAILS

                </div>


                <h1>

                  {customerName}

                </h1>


                {customerPhone && (

                  <div className="lead-phone">

                    {customerPhone}

                  </div>

                )}


                <div className="lead-id">

                  Lead ID:

                  <span>

                    {lead.lead_id}

                  </span>

                </div>

              </div>

            </div>


            <div
              className={
                `lead-intent-badge ${intentClass}`
              }
            >

              <span className="lead-intent-emoji">

                {intentEmoji}

              </span>


              <div>

                <strong>

                  {intentStatus
                    .charAt(0)
                    .toUpperCase() +
                    intentStatus.slice(
                      1
                    )}

                </strong>


                <span>

                  Intent ·{" "}

                  {intentScore}

                </span>

              </div>

            </div>

          </header>


          {/* CUSTOMER + INTENT */}

          <section className="lead-two-column">


            {/* CUSTOMER */}

            <article className="lead-card">

              <div className="lead-card-header">

                <div>

                  <div className="lead-card-kicker">

                    CUSTOMER

                  </div>


                  <h2>

                    Customer Information

                  </h2>

                </div>


                <div className="lead-card-icon">

                  👤

                </div>

              </div>


              <div className="lead-fields">

                {customerEntries.map(
                  ([key, value]) => (

                    <div
                      className="lead-field"
                      key={key}
                    >

                      <span className="lead-field-label">

                        {prettyKey(
                          key
                        )}

                      </span>


                      <strong className="lead-field-value">

                        {key ===
                        "budget"
                          ? formatBudget(
                              value
                            )
                          : formatValue(
                              value
                            )}

                      </strong>

                    </div>

                  )
                )}

              </div>

            </article>


            {/* INTENT */}

            <article className="lead-card">

              <div className="lead-card-header">

                <div>

                  <div className="lead-card-kicker">

                    LEAD INTENT

                  </div>


                  <h2>

                    Intent Analysis

                  </h2>

                </div>


                <div
                  className={
                    `lead-card-icon ${intentClass}`
                  }
                >

                  {intentEmoji}

                </div>

              </div>


              <div className="lead-score-panel">

                <div
                  className={
                    `lead-score-circle ${intentClass}`
                  }
                >

                  <strong>

                    {intentScore}

                  </strong>


                  <span>

                    SCORE

                  </span>

                </div>


                <div className="lead-score-copy">

                  <span>

                    Current intent

                  </span>


                  <strong>

                    {intentEmoji}{" "}

                    {intentStatus
                      .charAt(0)
                      .toUpperCase() +
                      intentStatus.slice(
                        1
                      )}

                  </strong>

                </div>

              </div>


              <div className="lead-section-label">

                Intent Signals

              </div>


              {Array.isArray(
                intent.signals
              ) &&
              intent.signals.length ? (

                <div className="lead-signals">

                  {intent.signals.map(
                    (
                      signal,
                      index
                    ) => (

                      <div
                        className="lead-signal"
                        key={index}
                      >

                        <span className="lead-signal-dot">

                          ✓

                        </span>


                        <span>

                          {typeof signal ===
                          "object"
                            ? JSON.stringify(
                                signal
                              )
                            : signal}

                        </span>

                      </div>

                    )
                  )}

                </div>

              ) : (

                <div className="lead-empty-small">

                  No intent signals
                  recorded.

                </div>

              )}

            </article>

          </section>


          {/* QUALIFICATION */}

          <section className="lead-card">

            <div className="lead-card-header">

              <div>

                <div className="lead-card-kicker">

                  QUALIFICATION

                </div>


                <h2>

                  Lead Qualification

                </h2>

              </div>


              <div className="lead-card-icon">

                ✓

              </div>

            </div>


            <div className="lead-fields lead-fields-wide">

              {Object.entries(
                qualification
              ).map(
                ([
                  key,
                  value,
                ]) => (

                  <div
                    className="lead-field"
                    key={key}
                  >

                    <span className="lead-field-label">

                      {prettyKey(
                        key
                      )}

                    </span>


                    <strong className="lead-field-value">

                      {formatValue(
                        value
                      )}

                    </strong>

                  </div>

                )
              )}

            </div>

          </section>


          {/* CONVERSATION */}

          <section className="lead-card lead-conversation-card">

            <div className="lead-card-header">

              <div>

                <div className="lead-card-kicker">

                  ACTIVITY

                </div>


                <h2>

                  Conversation History

                </h2>

              </div>


              <div className="lead-turn-count">

                {history.length}{" "}

                {history.length === 1
                  ? "message"
                  : "messages"}

              </div>

            </div>


            {history.length ? (

              <div className="lead-conversation">

                {history.map(
                  (
                    turn,
                    index
                  ) => {

                    const senderType =
                      getSenderType(
                        turn
                      );


                    const isCustomer =
                      senderType ===
                      "customer";


                    const message =
                      getMessageText(
                        turn
                      );


                    return (

                      <div
                        className={
                          `lead-conversation-row ${
                            isCustomer
                              ? "customer"
                              : "agent"
                          }`
                        }
                        key={
                          turn.id ||
                          turn.whatsapp_message_id ||
                          index
                        }
                      >

                        <div className="lead-message-meta">

                          <span className="lead-message-role">

                            {isCustomer
                              ? "Customer"
                              : "Agent"}

                          </span>


                          {turn.language && (

                            <span className="lead-message-language">

                              {turn.language}

                            </span>

                          )}


                          {turn.created_at && (

                            <span className="lead-message-time">

                              {formatMessageTime(
                                turn.created_at
                              )}

                            </span>

                          )}

                        </div>


                        <div className="lead-message-bubble">

                          {message}

                        </div>

                      </div>

                    );
                  }
                )}

              </div>

            ) : (

              <div className="lead-empty-conversation">

                <div>
                  💬
                </div>


                <strong>

                  No conversation history

                </strong>


                <span>

                  No messages have been
                  recorded for this lead.

                </span>

              </div>

            )}

          </section>


          {/* COMPLETE DATABASE RECORD */}

          <section className="lead-card">

            <div className="lead-card-header">

              <div>

                <div className="lead-card-kicker">

                  DATABASE

                </div>


                <h2>

                  Complete Lead Record

                </h2>

              </div>


              <div className="lead-card-icon">

                #

              </div>

            </div>


            <div className="lead-fields lead-fields-wide">

              <div className="lead-field">

                <span className="lead-field-label">

                  Lead ID

                </span>


                <strong className="lead-field-value">

                  {lead.lead_id}

                </strong>

              </div>


              <div className="lead-field">

                <span className="lead-field-label">

                  Created At

                </span>


                <strong className="lead-field-value">

                  {formatValue(
                    lead.created_at
                  )}

                </strong>

              </div>


              <div className="lead-field">

                <span className="lead-field-label">

                  Updated At

                </span>


                <strong className="lead-field-value">

                  {formatValue(
                    lead.updated_at
                  )}

                </strong>

              </div>


              <div className="lead-field">

                <span className="lead-field-label">

                  Conversation ID

                </span>


                <strong className="lead-field-value">

                  {formatValue(
                    lead.conversation_id
                  )}

                </strong>

              </div>

            </div>


            <details className="lead-raw-data">

              <summary>

                View complete JSON

              </summary>


              <pre>

                {JSON.stringify(
                  lead,
                  null,
                  2
                )}

              </pre>

            </details>

          </section>


        </div>

      </main>

    </div>
  );
}


export default LeadDetails;
