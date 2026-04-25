import React, { useEffect, useRef } from "react";
import { useNavigate } from "react-router-dom";
import { setAuthToken } from "../api";
import { useAuth } from "../context/AuthContext";

export default function AuthCallback() {
  const navigate = useNavigate();
  const { refresh } = useAuth();
  const hasProcessed = useRef(false);

  useEffect(() => {
    if (hasProcessed.current) return;
    hasProcessed.current = true;

    const hash = window.location.hash || "";
    const params = new URLSearchParams(hash.replace(/^#/, ""));
    const session_token = params.get("session_token");
    const onboarded = params.get("onboarded") === "true";

    if (!session_token) {
      navigate("/", { replace: true });
      return;
    }

    setAuthToken(session_token);
    window.history.replaceState({}, "", "/");

    refresh().then((user) => {
      if (!user || !user.onboarded) {
        navigate("/onboarding", { replace: true });
      } else {
        navigate("/dashboard", { replace: true });
      }
    });
  }, [navigate, refresh]);

  return (
    <div className="min-h-screen flex items-center justify-center" style={{ background: "#0a0a0a" }}>
      <div className="text-[#666] text-sm">Signing you in…</div>
    </div>
  );
}
