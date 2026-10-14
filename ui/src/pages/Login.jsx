import { useEffect, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { getCurrentUser, login } from "../api";

function Login() {
  const navigate = useNavigate();
  const location = useLocation();

  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");

  const [showPassword, setShowPassword] = useState(false);

  const [loading, setLoading] = useState(false);
  const [checkingSession, setCheckingSession] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    let mounted = true;

    const checkSession = async () => {
      try {
        const user = await getCurrentUser();

        if (!mounted) return;

        if (user?.authenticated) {
          sessionStorage.setItem(
            "mania_authenticated",
            "true"
          );

          sessionStorage.setItem(
            "mania_username",
            user.username || ""
          );

          const destination =
            location.state?.from || "/";

          navigate(destination, {
            replace: true,
          });

          return;
        }
      } catch {
        // User is not authenticated.
      } finally {
        if (mounted) {
          setCheckingSession(false);
        }
      }
    };

    checkSession();

    return () => {
      mounted = false;
    };
  }, [navigate, location.state]);

  const handleSubmit = async (event) => {
    event.preventDefault();

    setError("");

    if (!username.trim() || !password) {
      setError(
        "Enter your username and password."
      );
      return;
    }

    setLoading(true);

    try {
      const result = await login(
        username.trim(),
        password
      );

      if (!result?.authenticated) {
        throw new Error(
          "Authentication failed."
        );
      }

      sessionStorage.setItem(
        "mania_authenticated",
        "true"
      );

      sessionStorage.setItem(
        "mania_username",
        result.username || username.trim()
      );

      const destination =
        location.state?.from || "/";

      navigate(destination, {
        replace: true,
      });
    } catch (err) {
      setError(
        err?.data?.detail ||
          err?.message ||
          "Invalid username or password."
      );
    } finally {
      setLoading(false);
    }
  };

  /* =====================================================
     SESSION CHECKING
  ===================================================== */

  if (checkingSession) {
    return (
      <div className="auth-loading">
        <div className="auth-loading-spinner"></div>
        <span>Checking session...</span>
      </div>
    );
  }

  /* =====================================================
     LOGIN PAGE
  ===================================================== */

  return (
    <div className="login-page">

      <div className="login-shell">

        {/* =================================================
            LEFT VISUAL PANEL
        ================================================= */}

        <section className="login-visual">

          <div className="login-visual-grid"></div>

          <div className="login-visual-content">

            {/* Brand */}

            <div className="brand-mark">

              <div className="brand-mark-icon">
                M
              </div>

              <div>
                <div className="brand-name">
                  Mania Mobiles
                </div>

                <div className="brand-subtitle">
                  LEAD INTELLIGENCE
                </div>
              </div>

            </div>


            {/* Main heading */}

            <h2 className="visual-heading">
              <span>
                Turn conversations
              </span>

              <strong>
                into opportunities.
              </strong>
            </h2>


            {/* Description */}

            <p className="visual-description">
              Manage WhatsApp leads, understand
              customer intent, and help your sales
              team focus on the leads that matter.
            </p>


            {/* Stats */}

            <div className="visual-stats">

              <div className="visual-stat">

                <span className="visual-stat-dot blue"></span>

                <div>
                  <strong>
                    Lead Intelligence
                  </strong>

                  <span>
                    Understand customer intent
                  </span>
                </div>

              </div>


              <div className="visual-stat">

                <span className="visual-stat-dot green"></span>

                <div>
                  <strong>
                    Real-time Qualification
                  </strong>

                  <span>
                    Automatically identify serious buyers
                  </span>
                </div>

              </div>


              <div className="visual-stat">

                <span className="visual-stat-dot purple"></span>

                <div>
                  <strong>
                    Centralized Dashboard
                  </strong>

                  <span>
                    Keep every lead organized
                  </span>
                </div>

              </div>

            </div>

          </div>


          {/* Visual footer */}

          <div className="login-visual-footer">

            <span>
              Mania Mobiles
            </span>

            <span>•</span>

            <span>
              Lead Management System
            </span>

          </div>

        </section>


        {/* =================================================
            RIGHT LOGIN PANEL
        ================================================= */}

        <section className="login-form-panel">

          <div className="login-form-container">

            {/* =================================================
                MOBILE BRAND
            ================================================= */}

            <div className="mobile-brand">

              <div className="brand-mark-icon">
                M
              </div>

              <div>
                <div className="brand-name">
                  Mania Mobiles
                </div>

                <div className="brand-subtitle">
                  LEAD INTELLIGENCE
                </div>
              </div>

            </div>


            {/* =================================================
                LOGIN HEADING
            ================================================= */}

            <div className="login-eyebrow">

              <span className="online-dot"></span>

              <span>
                Administrator Portal
              </span>

            </div>


            <div className="login-heading">

              <h1>
                Welcome back
              </h1>

              <p>
                Sign in to access your lead
                management dashboard.
              </p>

            </div>


            {/* =================================================
                ERROR
            ================================================= */}

            {error && (
              <div
                className="login-error"
                role="alert"
              >

                <div className="login-error-icon">
                  !
                </div>

                <div>

                  <strong>
                    Sign in failed
                  </strong>

                  <span>
                    {error}
                  </span>

                </div>

              </div>
            )}


            {/* =================================================
                FORM
            ================================================= */}

            <form
              className="login-form"
              onSubmit={handleSubmit}
            >

              {/* USERNAME */}

              <div className="login-field">

                <label htmlFor="username">
                  Username
                </label>

                <div className="input-wrapper">

                  <span
                    className="input-icon"
                    aria-hidden="true"
                  >

                    <svg
                      viewBox="0 0 24 24"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="1.8"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                    >
                      <path d="M20 21a8 8 0 0 0-16 0" />
                      <circle cx="12" cy="7" r="4" />
                    </svg>

                  </span>


                  <input
                    id="username"
                    type="text"
                    value={username}
                    onChange={(event) => {
                      setUsername(event.target.value);
                      if (error) {
                        setError("");
                      }
                    }}
                    placeholder="Enter your username"
                    autoComplete="username"
                    disabled={loading}
                    autoFocus
                  />

                </div>

              </div>


              {/* PASSWORD */}

              <div className="login-field">

                <label htmlFor="password">
                  Password
                </label>

                <div className="input-wrapper">

                  <span
                    className="input-icon"
                    aria-hidden="true"
                  >

                    <svg
                      viewBox="0 0 24 24"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="1.8"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                    >
                      <rect
                        x="4"
                        y="10"
                        width="16"
                        height="11"
                        rx="2"
                      />

                      <path d="M8 10V7a4 4 0 0 1 8 0v3" />

                    </svg>

                  </span>


                  <input
                    id="password"
                    type={
                      showPassword
                        ? "text"
                        : "password"
                    }
                    value={password}
                    onChange={(event) => {
                      setPassword(event.target.value);
                      if (error) {
                        setError("");
                      }
                    }}
                    placeholder="Enter your password"
                    autoComplete="current-password"
                    disabled={loading}
                  />


                  {/* Password visibility */}

                  <button
                    type="button"
                    className="password-toggle"
                    onClick={() =>
                      setShowPassword(
                        (previous) => !previous
                      )
                    }
                    aria-label={
                      showPassword
                        ? "Hide password"
                        : "Show password"
                    }
                    title={
                      showPassword
                        ? "Hide password"
                        : "Show password"
                    }
                    disabled={loading}
                  >

                    {showPassword ? (

                      /* Eye off */

                      <svg
                        viewBox="0 0 24 24"
                        fill="none"
                        stroke="currentColor"
                        strokeWidth="1.8"
                        strokeLinecap="round"
                        strokeLinejoin="round"
                      >

                        <path d="M3 3l18 18" />

                        <path d="M10.6 10.6a2 2 0 0 0 2.8 2.8" />

                        <path d="M9.9 4.2A10.8 10.8 0 0 1 12 4c5.5 0 9 5 9 5s-1.1 1.6-3.1 3.1" />

                        <path d="M6.6 6.6C3.8 8.3 3 9 3 9s3.5 5 9 5c1 0 2-.2 2.9-.5" />

                      </svg>

                    ) : (

                      /* Eye */

                      <svg
                        viewBox="0 0 24 24"
                        fill="none"
                        stroke="currentColor"
                        strokeWidth="1.8"
                        strokeLinecap="round"
                        strokeLinejoin="round"
                      >

                        <path d="M2.5 12s3.5-6 9.5-6 9.5 6 9.5 6-3.5 6-9.5 6-9.5-6-9.5-6Z" />

                        <circle
                          cx="12"
                          cy="12"
                          r="2.5"
                        />

                      </svg>

                    )}

                  </button>

                </div>

              </div>


              {/* =================================================
                  SUBMIT
              ================================================= */}

              <button
                type="submit"
                className="login-submit"
                disabled={loading}
              >

                {loading ? (
                  <>
                    <span className="button-spinner"></span>

                    <span>
                      Signing in...
                    </span>
                  </>
                ) : (
                  <>
                    <span>
                      Sign in
                    </span>

                    <span
                      className="button-arrow"
                      aria-hidden="true"
                    >
                      →
                    </span>
                  </>
                )}

              </button>

            </form>


            {/* =================================================
                SECURITY
            ================================================= */}

            <div className="login-security">

              <svg
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.8"
                strokeLinecap="round"
                strokeLinejoin="round"
                aria-hidden="true"
              >

                <rect
                  x="4"
                  y="10"
                  width="16"
                  height="11"
                  rx="2"
                />

                <path d="M8 10V7a4 4 0 0 1 8 0v3" />

              </svg>

              <span>
                Secure administrator access
              </span>

            </div>


            {/* Copyright */}

            <div className="login-copyright">
              © {new Date().getFullYear()} Mania Mobiles
            </div>

          </div>

        </section>

      </div>

    </div>
  );
}

export default Login;