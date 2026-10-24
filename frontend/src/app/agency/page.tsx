import { redirect } from "next/navigation";

/**
 * /agency has no index page of its own — only /agency/[agentId].
 *
 * The SARA Desktop "open agency workspace" action and external links
 * occasionally land on the bare /agency URL (no agent id), which used to
 * 404 with "Hmmm... can't reach this page". Send those visitors to the
 * Agent Hub, which lists both the 15 hub agents and the agency
 * specialists, instead of dead-ending.
 */
export default function AgencyIndex() {
  redirect("/hub");
}
