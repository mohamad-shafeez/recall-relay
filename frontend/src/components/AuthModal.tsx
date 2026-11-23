import { useState } from "react";
import type { FormEvent } from "react";

import { useSession } from "../context/SessionContext";
import { FeedbackBanner } from "./ui/DesignSystem";

interface AuthModalProps {
  isOpen: boolean;
  onClose: () => void;
}

export function AuthModal({ isOpen, onClose }: AuthModalProps) {
  const { signInAdmin, signInOrganization } = useSession();
  const [tab, setTab] = useState<"admin" | "organization">("admin");
  const [secretInput, setSecretInput] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!isOpen) {
    return null;
  }

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setIsSubmitting(true);
    try {
      if (tab === "admin") {
        await signInAdmin(secretInput);
      } else {
        await signInOrganization(secretInput);
      }
      setSecretInput("");
      onClose();
    } catch (err: unknown) {
      setError(
        err instanceof Error
          ? err.message
          : "Credential verification failed against the backend."
      );
    } finally {
      setIsSubmitting(false);
    }
  }

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="auth-modal-title"
      className="fixed inset-0 z-50 flex items-center justify-center bg-charcoal/40 p-4 backdrop-blur-xs"
    >
      <div className="w-full max-w-md rounded-2xl border border-peach-border bg-white p-6 shadow-xl">
        <div className="flex items-start justify-between gap-4">
          <div>
            <p className="text-xs font-semibold uppercase tracking-wider text-burgundy">
              Session Authentication
            </p>
            <h2 id="auth-modal-title" className="mt-1 text-lg font-semibold text-charcoal">
              Authenticate with RecallRelay Backend
            </h2>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg border border-peach-border px-2.5 py-1 text-xs font-semibold text-mauve hover:bg-cream"
          >
            Close
          </button>
        </div>

        <p className="mt-2 text-xs leading-relaxed text-mauve">
          Credentials are verified once by the FastAPI backend and exchanged for a server-side
          HttpOnly SameSite=Strict session cookie. Raw keys are never retained in browser memory or storage.
        </p>

        <div className="mt-4 grid grid-cols-2 gap-2 rounded-xl bg-cream p-1">
          <button
            type="button"
            onClick={() => {
              setTab("admin");
              setError(null);
            }}
            className={`rounded-lg px-3 py-2 text-xs font-semibold transition ${
              tab === "admin"
                ? "bg-burgundy text-white shadow-xs"
                : "text-charcoal hover:bg-peach/50"
            }`}
          >
            Administrator (X-Admin-Key)
          </button>
          <button
            type="button"
            onClick={() => {
              setTab("organization");
              setError(null);
            }}
            className={`rounded-lg px-3 py-2 text-xs font-semibold transition ${
              tab === "organization"
                ? "bg-burgundy text-white shadow-xs"
                : "text-charcoal hover:bg-peach/50"
            }`}
          >
            Organization (Bearer Token)
          </button>
        </div>

        <form onSubmit={(e) => void handleSubmit(e)} className="mt-4 space-y-4">
          <div>
            <label
              htmlFor="credential-input"
              className="block text-xs font-semibold text-charcoal"
            >
              {tab === "admin" ? "Server X-Admin-Key" : "Organization Bearer Token (rr_org_...)"}
            </label>
            <input
              id="credential-input"
              type="password"
              autoComplete="off"
              value={secretInput}
              onChange={(e) => setSecretInput(e.target.value)}
              placeholder={tab === "admin" ? "Enter configured admin key" : "Enter issued rr_org_ token"}
              className="mt-1.5 w-full rounded-lg border border-peach-border bg-cream/60 px-3 py-2 text-sm text-charcoal focus:border-burgundy focus:outline-none"
              required
            />
          </div>

          {error && <FeedbackBanner kind="error" message={error} />}

          <div className="flex justify-end gap-2">
            <button
              type="button"
              onClick={onClose}
              className="rounded-lg border border-peach-border bg-white px-4 py-2 text-xs font-semibold text-charcoal hover:bg-cream"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={isSubmitting}
              className="rounded-lg bg-burgundy px-4 py-2 text-xs font-semibold text-white hover:bg-burgundy-deep disabled:opacity-60"
            >
              {isSubmitting ? "Verifying..." : "Verify & Sign In"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
