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

## Residual risks (accepted)

| Risk | Why unfixable here | Mitigation we do provide |
|---|---|---|
| Payment identity | Keys are billed accounts; KYC ties them to a real identity | Documented; ops choice to source keys accordingly |
| Content/stylometric correlation | Prompts must reach the model; repo names and writing style survive any scrubbing | `PERSONA_HYGIENE` strips identifier fields (#49); PII vault-scrubbing is a follow-up |
| Canonicalized-body hashing | Serialization jitter raises the bar; hashing parsed payloads defeats it | Per-attempt serialization jitter lands with #46 |
| Host-clock timing | All personas share one machine's clock and cadence | None at this layer |

Fingerprint *values* are validated only for blankness in this ticket;
semantic validation (known stack or well-formed JA3) belongs to #47, which
consumes them.

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
