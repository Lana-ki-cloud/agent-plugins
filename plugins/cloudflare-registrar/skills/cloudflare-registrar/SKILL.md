---
name: cloudflare-registrar
description: "Cloudflare Registrar for Lana KI — read-only by default. Search domain names, check real-time availability and pricing, check transfer eligibility, list and read registrar domains and registrations, and check registration workflow status. Domain registration and registrar configuration changes (auto-renew, lock, privacy) are separately gated, never automatic, and must be run manually by a human after an explicit purchase confirmation. Triggers on: Cloudflare domain, Cloudflare Registrar, is this domain available, domain price, domain renewal price, register domain on Cloudflare, buy domain, transfer domain to Cloudflare, list my Cloudflare domains, registration status."
license: Apache-2.0
metadata:
  tags: cloudflare, registrar, domains, domain-search, availability, pricing, transfer, audit, least-privilege, read-only, lana-ki
---

# Cloudflare Registrar (Lana KI)

Secret-safe, audit-first, least-privilege access to the Cloudflare Registrar API through the bundled stdlib-only CLI [cf_registrar.py](../../scripts/cf_registrar.py). The CLI runs **server-side only** and reads tokens from its environment.

## Hard rules

1. **Read-only default.** Use only read operations unless the user explicitly asks to register or change a domain.
2. **Never run `register` or `update-domain` yourself.** They are blocked without a human at an interactive terminal. Prepare the command and hand it to the user to run manually.
3. **No automatic purchases.** Cloudflare domain registrations are **non-refundable**. Always show the full purchase gate summary first.
4. **Never handle secrets.** Do not ask for, print, echo, store or pass API tokens or transfer auth/EPP codes. Never put a token on the command line. If a token is missing, tell the user which environment variable the operator must configure on the backend.
5. **No blind retries.** If a registration failed or its outcome is unknown, check `registration-status` and report. Do not resubmit.

## Invocation

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/cf_registrar.py" <operation> [args]
```

Output is JSON on stdout. Errors are JSON on stderr. Exit codes: `0` success, `1` failure, `2` blocked by a safety gate.

## Read-only operations (read token)

| Goal                                     | Operation                                         |
| ---------------------------------------- | ------------------------------------------------- |
| Domain ideas (cached, non-authoritative) | `search "<keywords>" [--extension dev]`           |
| Real-time availability + pricing (≤20)   | `check <domain> [<domain> ...]`                   |
| Purchase gate summary for one domain     | `quote <domain> [--years N]`                      |
| Transfer eligibility (no auth codes)     | `transfer-check <domain> [...]`                   |
| Account domains / one domain             | `list-domains`, `get-domain <domain>`             |
| Registrations / one registration         | `list-registrations`, `get-registration <domain>` |
| Registration workflow status             | `registration-status <domain>`                    |
| Configuration-change workflow status     | `update-status <domain>`                          |
| Supported extensions / one extension     | `list-extensions`, `get-extension <ext>`          |

`search` results are cached. Always confirm with `check` or `quote` before presenting a domain as available.

## Purchase workflow (gated)

1. Run `quote <domain> --years N` (read-only).
2. Show the user **all** purchase gate fields: domain, years, currency, first-year price, renewal price, estimated total and the irreversible-purchase warning.
3. Ask the user for explicit confirmation. Without it, stop.
4. Give the user the exact command to run **manually in their own terminal on the backend host**:
   `register <domain> --years N --confirm "<confirmation_phrase from quote>"`
5. After the user reports back, run `registration-status <domain>` and report the workflow state.

Load [purchase-gate.md](references/purchase-gate.md) before any purchase or configuration-change request.

## Configuration changes (gated)

`update-domain <domain> [--auto-renew true|false] [--locked true|false] [--privacy true|false]` follows the same rules. Show the change, warn that `auto-renew=true` authorizes future charges and that `locked=false` allows a transfer away, then hand the command to the user.

## Secrets, auditing and idempotency

Load [security-and-audit.md](references/security-and-audit.md) when configuring the plugin, investigating a blocked or failed operation, or reviewing the audit log.
