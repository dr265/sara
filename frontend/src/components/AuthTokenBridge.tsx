"use client";

import { useEffect } from "react";
import { useAuth } from "@clerk/nextjs";
import { setAuthTokenGetter } from "@/lib/api";

/**
 * Keeps lib/api's saraFetch aware of the Clerk session.
 *
 * Signed in  -> every backend call carries the session token as a Bearer
 *               credential; the backend scopes bots/tasks/memory to THAT user
 *               (see tenancy.py) instead of the shared local operator.
 * Signed out -> no token; the configured API key keeps resolving to the
 *               local tenant exactly as before.
 */
export default function AuthTokenBridge() {
  const { isLoaded, isSignedIn, getToken } = useAuth();

  useEffect(() => {
    if (!isLoaded) return;
    if (isSignedIn) {
      setAuthTokenGetter(() => getToken());
    } else {
      setAuthTokenGetter(null);
    }
    return () => setAuthTokenGetter(null);
  }, [isLoaded, isSignedIn, getToken]);

  return null;
}
