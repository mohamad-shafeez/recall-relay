import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";

import { useSession } from "../context/SessionContext";
import { recallRelayApi } from "../services/api";
import type {
  EvidenceItem,
  IdentityVerificationStatus,
  OrganizationRecord,
} from "../types/api";
import type { ConnectionStatus, OrganizationRole } from "../types/domain";
import {
  EmptyState,
  FeedbackBanner,
  formatTimestamp,
  PanelCard,
  StatusBadge,
} from "../components/ui/DesignSystem";

export function OrganizationsPage() {
  const { credentials, sessionVersion } = useSession();
  const [orgs, setOrgs] = useState<OrganizationRecord[]>([]);
  const [evidenceList, setEvidenceList] = useState<EvidenceItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [feedback, setFeedback] = useState<{ kind: "error" | "success"; message: string } | null>(
    null
  );
  const [issuedOneTimeNotice, setIssuedOneTimeNotice] = useState<string | null>(null);

  // Admin register form
  const [name, setName] = useState("");
  const [role, setRole] = useState<OrganizationRole>("MANUFACTURER");
  const [connStatus, setConnStatus] = useState<ConnectionStatus>("NOT_CONNECTED");
  const [sourceOrg, setSourceOrg] = useState("");
  const [sourceDoc, setSourceDoc] = useState("");
  const [sourceUrl, setSourceUrl] = useState("");

  // Admin identity review state
  const [reviewOrgId, setReviewOrgId] = useState<string | null>(null);
  const [targetStatus, setTargetStatus] =
    useState<IdentityVerificationStatus>("PENDING_VERIFICATION");
  const [reviewerName, setReviewerName] = useState("");
  const [supportingEvId, setSupportingEvId] = useState("");

  const loadData = useCallback(
    async (signal?: AbortSignal) => {
      setLoading(true);
      try {
        const list = await recallRelayApi.listOrganizations(credentials, signal);
        setOrgs(list);
        if (credentials.mode === "admin") {
          const evs = await recallRelayApi.listEvidence(credentials, signal);
          setEvidenceList(evs);
        }
      } catch (err: unknown) {
        if (err instanceof DOMException && err.name === "AbortError") return;
        setFeedback({
          kind: "error",
          message: err instanceof Error ? err.message : "Failed to load organizations.",
        });
      } finally {
        if (!signal?.aborted) setLoading(false);
      }
    },
    [credentials]
  );

  useEffect(() => {
    const controller = new AbortController();
    void loadData(controller.signal);
    return () => controller.abort();
  }, [loadData, sessionVersion]);

  async function handleRegisterOrg(e: FormEvent) {
    e.preventDefault();
    setFeedback(null);
    setIssuedOneTimeNotice(null);
    try {
      const res = await recallRelayApi.registerOrganization(credentials, {
        name: name.trim(),
        role,
        connection_status: connStatus,
        source_organization: sourceOrg.trim(),
        source_document: sourceDoc.trim() || null,
        source_url: sourceUrl.trim() || null,
      });
      setName("");
      setSourceOrg("");
      setSourceDoc("");
      setSourceUrl("");
      setIssuedOneTimeNotice(
        `Organization '${res.organization.name}' registered as UNVERIFIED. Deliver its one-time bearer token securely out-of-band (never stored in browser): ${res.access_token}`
      );
      await loadData();
    } catch (err: unknown) {
      setFeedback({
        kind: "error",
        message: err instanceof Error ? err.message : "Failed to register organization.",
      });
    }
  }

  async function handleIdentityReview(e: FormEvent) {
    e.preventDefault();
    if (!reviewOrgId) return;
    setFeedback(null);
    try {
      const updated = await recallRelayApi.reviewOrganizationIdentity(
        credentials,
        reviewOrgId,
        {
          target_status: targetStatus,
          reviewer_name: reviewerName.trim(),
          supporting_evidence_id: supportingEvId.trim() || null,
        }
      );
      setReviewOrgId(null);
      setReviewerName("");
      setSupportingEvId("");
      setFeedback({
        kind: "success",
        message: `Organization '${updated.name}' identity status updated to ${updated.identity_verification_status}.`,
      });
      await loadData();
    } catch (err: unknown) {
      setFeedback({
        kind: "error",
        message: err instanceof Error ? err.message : "Identity review failed.",
      });
    }
  }

  return (
    <div className="space-y-6">
      {feedback && <FeedbackBanner kind={feedback.kind} message={feedback.message} />}
      {issuedOneTimeNotice && (
        <div className="rounded-xl border border-status-pending-border bg-status-pending-bg p-4 text-xs text-charcoal">
          <div className="flex items-start justify-between gap-2">
            <p className="break-all font-mono">{issuedOneTimeNotice}</p>
            <button
              type="button"
              onClick={() => setIssuedOneTimeNotice(null)}
              className="shrink-0 rounded border border-peach-border bg-white px-2 py-1 text-[11px] font-semibold"
            >
              Dismiss
            </button>
          </div>
        </div>
      )}

      {credentials.mode === "admin" && (
        <PanelCard
          title="Register Organization (Admin)"
          subtitle="Registration creates an UNVERIFIED organization boundary; token possession never implies verified legal identity"
        >
          <form onSubmit={(e) => void handleRegisterOrg(e)} className="grid grid-cols-1 gap-3 sm:grid-cols-3">
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Organization legal name *"
              required
              className="rounded-lg border border-peach-border bg-cream/50 px-3 py-2 text-xs"
            />
            <select
              value={role}
              onChange={(e) => setRole(e.target.value as OrganizationRole)}
              className="rounded-lg border border-peach-border bg-cream/50 px-3 py-2 text-xs"
            >
              <option value="MANUFACTURER">MANUFACTURER</option>
              <option value="DISTRIBUTOR">DISTRIBUTOR</option>
              <option value="RETAILER">RETAILER</option>
              <option value="REGULATOR">REGULATOR</option>
              <option value="COORDINATOR">COORDINATOR</option>
            </select>
            <select
              value={connStatus}
              onChange={(e) => setConnStatus(e.target.value as ConnectionStatus)}
              className="rounded-lg border border-peach-border bg-cream/50 px-3 py-2 text-xs"
            >
              <option value="NOT_CONNECTED">NOT_CONNECTED</option>
              <option value="AWAITING_AUTHORIZATION">AWAITING_AUTHORIZATION</option>
              <option value="CONNECTED">CONNECTED</option>
            </select>
            <input
              value={sourceOrg}
              onChange={(e) => setSourceOrg(e.target.value)}
              placeholder="Source registry / authority *"
              required
              className="rounded-lg border border-peach-border bg-cream/50 px-3 py-2 text-xs"
            />
            <input
              value={sourceUrl}
              onChange={(e) => setSourceUrl(e.target.value)}
              placeholder="Source URL (https://...)"
              className="rounded-lg border border-peach-border bg-cream/50 px-3 py-2 text-xs"
            />
            <input
              value={sourceDoc}
              onChange={(e) => setSourceDoc(e.target.value)}
              placeholder="Source document reference"
              className="rounded-lg border border-peach-border bg-cream/50 px-3 py-2 text-xs"
            />
            <div className="sm:col-span-3">
              <button
                type="submit"
                className="rounded-lg bg-burgundy px-4 py-2 text-xs font-semibold text-white hover:bg-burgundy-deep"
              >
                Register Organization
              </button>
            </div>
          </form>
        </PanelCard>
      )}

      <PanelCard
        title={`Organization Directory (${orgs.length})`}
        subtitle="Credentials and hashes are never exposed; identity verification requires human admin review"
      >
        {loading ? (
          <p className="text-xs text-mauve">Loading organizations...</p>
        ) : orgs.length === 0 ? (
          <EmptyState
            title="No organizations registered"
            description="No manufacturer, distributor, or retailer organizations have been registered in the backend yet."
          />
        ) : (
          <div className="overflow-x-auto rounded-xl border border-peach-border">
            <table className="w-full text-left text-xs">
              <thead className="bg-peach/40 text-burgundy-deep">
                <tr>
                  <th className="px-3 py-2.5 font-semibold">Organization</th>
                  <th className="px-3 py-2.5 font-semibold">Role</th>
                  <th className="px-3 py-2.5 font-semibold">Identity Verification</th>
                  <th className="px-3 py-2.5 font-semibold">Connection</th>
                  <th className="px-3 py-2.5 font-semibold">Registered</th>
                  {credentials.mode === "admin" && (
                    <th className="px-3 py-2.5 font-semibold">Review</th>
                  )}
                </tr>
              </thead>
              <tbody className="divide-y divide-peach-border bg-white">
                {orgs.map((org) => (
                  <tr key={org.id}>
                    <td className="px-3 py-2.5">
                      <p className="font-semibold text-charcoal">{org.name}</p>
                      <p className="font-mono text-[11px] text-mauve">{org.id}</p>
                    </td>
                    <td className="px-3 py-2.5">
                      <StatusBadge value={org.role} />
                    </td>
                    <td className="px-3 py-2.5">
                      <StatusBadge value={org.identity_verification_status} />
                    </td>
                    <td className="px-3 py-2.5">
                      <StatusBadge value={org.connection_status} />
                    </td>
                    <td className="px-3 py-2.5 text-mauve">
                      {formatTimestamp(org.created_at)}
                    </td>
                    {credentials.mode === "admin" && (
                      <td className="px-3 py-2.5">
                        <button
                          type="button"
                          onClick={() => setReviewOrgId(org.id)}
                          className="rounded-lg border border-peach-border bg-cream px-2.5 py-1 text-xs font-semibold text-burgundy-deep hover:bg-peach/60"
                        >
                          Review Identity
                        </button>
                      </td>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </PanelCard>

      {credentials.mode === "admin" && reviewOrgId && (
        <PanelCard
          title={`Human Identity Verification Review (${reviewOrgId})`}
          subtitle="Setting VERIFIED strictly requires an authoritative supporting_evidence_id"
          action={
            <button
              type="button"
              onClick={() => setReviewOrgId(null)}
              className="rounded-lg border border-peach-border px-3 py-1 text-xs font-semibold text-mauve"
            >
              Cancel
            </button>
          }
        >
          <form onSubmit={(e) => void handleIdentityReview(e)} className="grid grid-cols-1 gap-3 sm:grid-cols-3">
            <select
              value={targetStatus}
              onChange={(e) => setTargetStatus(e.target.value as IdentityVerificationStatus)}
              className="rounded-lg border border-peach-border bg-cream/50 px-3 py-2 text-xs"
            >
              <option value="PENDING_VERIFICATION">PENDING_VERIFICATION</option>
              <option value="VERIFIED">VERIFIED (Requires Evidence)</option>
              <option value="REJECTED">REJECTED</option>
              <option value="UNVERIFIED">UNVERIFIED</option>
            </select>
            <input
              value={reviewerName}
              onChange={(e) => setReviewerName(e.target.value)}
              placeholder="Authorized human reviewer name *"
              required
              className="rounded-lg border border-peach-border bg-cream/50 px-3 py-2 text-xs"
            />
            <select
              value={supportingEvId}
              onChange={(e) => setSupportingEvId(e.target.value)}
              required={targetStatus === "VERIFIED"}
              className="rounded-lg border border-peach-border bg-cream/50 px-3 py-2 text-xs"
            >
              <option value="">Supporting evidence_id (required for VERIFIED)</option>
              {evidenceList.map((ev) => (
                <option key={ev.id} value={ev.id}>
                  {ev.id} — {ev.record_reference}
                </option>
              ))}
            </select>
            <div className="sm:col-span-3">
              <button
                type="submit"
                className="rounded-lg bg-burgundy px-4 py-2 text-xs font-semibold text-white hover:bg-burgundy-deep"
              >
                Submit Identity Decision
              </button>
            </div>
          </form>
        </PanelCard>
      )}
    </div>
  );
}
