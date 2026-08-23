# Persona-based unlinkability for rotated egress

The rotator exists to spread load across N (egress IP, API key) pairs — but
everything except the IP and key linked those pairs back to one operator:
the client's `User-Agent` and SDK telemetry headers rode upstream verbatim,
all egresses shared one Python TLS stack, credential headers could leak the
caller's own key, and byte-identical failover replays tied personas together
within seconds. We decided to generalize the existing `Node` into a
**persona**: every identity attribute a provider can observe — egress, key,
User-Agent, transport fingerprint — is pinned per node and moves together,
never mixed across nodes.

## Threat model

Adversary: the LLM provider's anti-abuse systems, with full server-side view
(key, IP, TLS fingerprint, headers, body, timing). Goal: the N personas are
mutually unlinkable and none is identifiable as "one automation stack".
Explicit non-goals (unachievable at this layer):

- **Payment identity** — keys are billed accounts; KYC links them to a real
  identity regardless of what this proxy sends.
- **Content/stylometric correlation** — prompts must reach the model; repo
  names and writing style survive any header hygiene.
- **Canonicalized-body hashing** — serialization jitter raises the bar but a
  provider hashing parsed payloads defeats it.
- **Host-clock timing** — all personas share one machine's clock and cadence.

## Decisions

- **Persona = coherent client stack**, curated from realistic *API* clients
  (httpx, undici, okhttp, reqwest, python-requests). Browser stacks were
  rejected: browser TLS fingerprints on API endpoints stand out more than
  they hide.
- **Assignment is a stable hash of node_id**, so a node keeps its persona
  across restarts; `PERSONA_N_USER_AGENT` / `PERSONA_N_FINGERPRINT`
  override it. Blank overrides fail loudly at startup.
- **Credential/organization header drops are unconditional** (`x-api-key`,
  `api-key`, `openai-organization`, `openai-project`): bug fixes, not
  features. Client telemetry stripping (`x-stainless-*`, `x-app`, `x-title`,
  `http-referer`) and payload-field removal (`user`, `metadata`,
  `prompt_cache_key`, `safety_identifier`) ship behind `PERSONA_HYGIENE`
  (default false) while the behavior change soaks.
- **The persona User-Agent always replaces the client's**: identity
  coherence — a client UA under a persona fingerprint would be incoherent.

## Consequences

- With the default `requests` transport only the User-Agent half of a
  persona is observable (Python h1.1 stack throughout); the fingerprint half
  activates with the flag-gated curl_cffi transport (issue #47). Mixed-mode
  degradation is documented, not hidden.
- Failover still replays request bytes across personas by default; closing
  that linkage oracle is ticket #46 (same-persona Retry-After ladder +
  serialization jitter).
- `/health` exposes each node's persona labels (never keys) for operators.
- Residual risks above are accepted and re-stated in README's security notes.

Design record: epic #45 (grill session 2026-08-23), tickets #46–#50.
