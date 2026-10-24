"""Sara Bots — the real-agent workspace service.

Aggregates the platform's ACTUAL agents (backend hub agents, the agency
specialist fleet, and approved local Python actions discovered statically
from sara_desktop/actions) into one registry with honest states, and executes
them through a bounded, audited runner.

Nothing here fabricates a status, an output, or a capability:
  * hub agent liveness comes from the same real HTTP probes the Ultron
    control center uses (app.agents.sara_ultron_agent.core.fleet);
  * local actions are discovered by ``ast`` ONLY — discovery never imports
    or executes discovered code — and run only after explicit approval,
    in a separate process with a timeout and a scrubbed environment.
"""
