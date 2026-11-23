import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";

import { recallRelayApi } from "../services/api";
import type { SessionCredentials } from "../types/api";

const PUBLIC_SESSION: SessionCredentials = {
  mode: "public",
  csrfToken: null,
  organizationId: null,
  organizationRole: null,
  dataBoundaryId: null,
  expiresAt: null,
  label: "Public Read-Only",
};

interface SessionContextValue {
  credentials: SessionCredentials;
  sessionVersion: number;
  signInAdmin: (adminKey: string) => Promise<void>;
  signInOrganization: (orgToken: string) => Promise<void>;
  signOut: () => Promise<void>;
}

const SessionContext = createContext<SessionContextValue | null>(null);

export function SessionProvider({ children }: { children: ReactNode }) {
  // Zero browser secret retention: only server-issued CSRF token and non-secret session metadata.
  const [credentials, setCredentials] = useState<SessionCredentials>(PUBLIC_SESSION);
  const [sessionVersion, setSessionVersion] = useState<number>(0);

  useEffect(() => {
    const controller = new AbortController();
    recallRelayApi
      .getSessionMe(controller.signal)
      .then((res) => {
        if (!res.authenticated) {
          setCredentials(PUBLIC_SESSION);
          return;
        }
        setCredentials({
          mode: res.mode === "admin" ? "admin" : "organization",
          csrfToken: res.csrf_token,
          organizationId: res.organization_id,
          organizationRole: res.organization_role,
          dataBoundaryId: res.data_boundary_id,
          expiresAt: res.expires_at,
          label:
            res.mode === "admin"
              ? "Administrator (HttpOnly Session)"
              : `Organization ${res.organization_role ?? ""} (HttpOnly Session)`.trim(),
        });
      })
      .catch(() => {
        setCredentials(PUBLIC_SESSION);
      });
    return () => controller.abort();
  }, []);

  const signInAdmin = useCallback(async (adminKey: string) => {
    const trimmed = adminKey.trim();
    if (!trimmed) {
      throw new Error("Administrative key is required.");
    }
    const res = await recallRelayApi.loginAdmin(trimmed);
    setCredentials({
      mode: "admin",
      csrfToken: res.csrf_token,
      organizationId: null,
      organizationRole: null,
      dataBoundaryId: null,
      expiresAt: res.expires_at,
      label: "Administrator (HttpOnly Session)",
    });
    setSessionVersion((v) => v + 1);
  }, []);

  const signInOrganization = useCallback(async (orgToken: string) => {
    const trimmed = orgToken.trim();
    if (!trimmed) {
      throw new Error("Organization bearer token is required.");
    }
    const res = await recallRelayApi.loginOrganization(trimmed);
    setCredentials({
      mode: "organization",
      csrfToken: res.csrf_token,
      organizationId: res.organization_id,
      organizationRole: res.organization_role,
      dataBoundaryId: res.data_boundary_id,
      expiresAt: res.expires_at,
      label: `Organization ${res.organization_role ?? ""} (HttpOnly Session)`.trim(),
    });
    setSessionVersion((v) => v + 1);
  }, []);

  const signOut = useCallback(async () => {
    try {
      await recallRelayApi.logoutSession(credentials);
    } catch {
      // Clear local session state even if network fails
    } finally {
      setCredentials(PUBLIC_SESSION);
      setSessionVersion((v) => v + 1);
    }
  }, [credentials]);

  const value = useMemo(
    () => ({
      credentials,
      sessionVersion,
      signInAdmin,
      signInOrganization,
      signOut,
    }),
    [credentials, sessionVersion, signInAdmin, signInOrganization, signOut]
  );

  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

export function useSession(): SessionContextValue {
  const ctx = useContext(SessionContext);
  if (!ctx) {
    throw new Error("useSession must be used inside SessionProvider.");
  }
  return ctx;
}
