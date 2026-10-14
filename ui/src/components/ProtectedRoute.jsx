import { useEffect, useState } from "react";
import { Navigate, useLocation } from "react-router-dom";
import { getCurrentUser } from "../api";

function ProtectedRoute({ children }) {
  const location = useLocation();
  const [state, setState] = useState("checking");

  useEffect(() => {
    let active = true;

    getCurrentUser()
      .then((user) => {
        if (!active) return;
        if (user?.authenticated) {
          sessionStorage.setItem("mania_authenticated", "true");
          sessionStorage.setItem("mania_username", user.username || "");
          setState("authenticated");
        } else {
          sessionStorage.removeItem("mania_authenticated");
          sessionStorage.removeItem("mania_username");
          setState("unauthenticated");
        }
      })
      .catch(() => {
        if (!active) return;
        sessionStorage.removeItem("mania_authenticated");
        sessionStorage.removeItem("mania_username");
        setState("unauthenticated");
      });

    return () => {
      active = false;
    };
  }, []);

  if (state === "checking") {
    return (
      <div className="auth-loading">
        <div className="auth-loading-spinner" />
        <span>Checking session...</span>
      </div>
    );
  }

  if (state !== "authenticated") {
    return (
      <Navigate
        to="/login"
        replace
        state={{ from: location.pathname }}
      />
    );
  }

  return children;
}

export default ProtectedRoute;
