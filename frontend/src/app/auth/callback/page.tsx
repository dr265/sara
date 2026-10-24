"use client";

import { useEffect, useRef, useState, Suspense } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { SARA_API_URL, saraFetch } from "@/lib/api";

function AuthCallbackContent() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [status, setStatus] = useState("Authenticating with Google...");
  const [detail, setDetail] = useState("");
  // React 18 StrictMode mounts effects twice in dev — that would exchange the
  // same one-time auth code twice and the second call always fails with
  // invalid_grant. Guard so the exchange happens exactly once.
  const exchangeStarted = useRef(false);

  useEffect(() => {
    if (exchangeStarted.current) return;
    exchangeStarted.current = true;

    const code = searchParams.get("code");
    // The sender panel sends the page it came from ("/hub/email-automation")
    // as OAuth state; Google round-trips it verbatim. Only allow internal
    // paths so a crafted state can never redirect somewhere external.
    const rawState = searchParams.get("state") ?? "";
    const next =
      rawState.startsWith("/") && !rawState.startsWith("//")
        ? rawState
        : "/workspace";
    
    if (!code) {
      setStatus("Error: No authorization code found.");
      return;
    }

    const exchangeToken = async () => {
      try {
        const response = await saraFetch(`${SARA_API_URL}/api/auth/google`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ 
            code: code,
            // Hardcoding a dummy user_id for now, assuming single-user local testing
            user_id: "user_123" 
          }),
        });

        const data = await response.json().catch(() => ({}));

        if (response.ok) {
          setStatus("Successfully connected! Redirecting back...");
          setTimeout(() => router.push(next), 2000);
        } else {
          setStatus("Failed to save credentials in the backend.");
          const googleDetail = data?.detail ?? "";
          if (/unauthorized_client/i.test(googleDetail)) {
            setDetail(
              "Google rejected the app credentials (unauthorized_client). The GOOGLE_CLIENT_SECRET in backend/.env is stale or the OAuth client is not a 'Web application' type. Fix it in Google Cloud Console → APIs & Services → Credentials, then click Connect Gmail again."
            );
          } else if (/invalid_grant/i.test(googleDetail)) {
            setDetail(
              "The authorization code was already used or expired. Click Connect Gmail once more and complete the consent again."
            );
          } else {
            setDetail(googleDetail || "Unknown error from backend.");
          }
        }
      } catch (error) {
        setStatus("Connection to backend failed.");
        setDetail("Is the backend running on port 8000?");
      }
    };

    exchangeToken();
  }, [searchParams, router]);

  return (
    <div style={{ display: 'flex', height: '100vh', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', backgroundColor: '#04050c', color: '#fff', padding: '0 2rem', textAlign: 'center' }}>
      <h2 style={{ marginBottom: '1rem' }}>{status}</h2>
      {detail && <p style={{ maxWidth: 640, color: '#f87171', fontSize: 14, lineHeight: 1.6 }}>{detail}</p>}
    </div>
  );
}

export default function AuthCallbackPage() {
  return (
    <Suspense fallback={<div style={{ display: 'flex', height: '100vh', alignItems: 'center', justifyContent: 'center', backgroundColor: '#04050c', color: '#fff' }}><h2>Loading...</h2></div>}>
      <AuthCallbackContent />
    </Suspense>
  );
}