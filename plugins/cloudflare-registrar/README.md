# Cloudflare Registrar – Plugin for Lana KI

Secret-safe, audit-first, least-privilege Cloudflare Registrar access for the Lana KI control plane.

> **Cloudflare domain registrations are non-refundable.** This plugin never purchases automatically. Every registration stays a manual, explicit, human decision behind a confirmation gate.

## What it does

The plugin bundles one skill (`cloudflare-registrar`) and one standard-library-only Python CLI, [`scripts/cf_registrar.py`](scripts/cf_registrar.py). It has no third-party dependencies and no MCP server. The CLI runs server-side on the Lana KI backend.

| Capability                      | Operation(s)                                                                                               | Mode             |
| ------------------------------- | ---------------------------------------------------------------------------------------------------------- | ---------------- |
| Domain search (suggestions)     | `search`                                                                                                   | read             |
| Availability and pricing        | `check`, `quote`                                                                                           | read             |
| Transferability                 | `transfer-check`                                                                                           | read             |
| Registrar domain/account reads  | `list-domains`, `get-domain`, `list-registrations`, `get-registration`, `list-extensions`, `get-extension` | read             |
| Workflow status                 | `registration-status`, `update-status`                                                                     | read             |
| Domain registration             | `register`                                                                                                 | **write, gated** |
| Registrar configuration changes | `update-domain` (auto-renew, lock, privacy)                                                                | **write, gated** |

Transfer-in and transfer auth/EPP codes are intentionally out of scope.

## Read-only default

- Without `CF_REGISTRAR_WRITE_ENABLED=true`, every write operation is blocked (exit code `2`) before any network call.
- Read operations use a dedicated read-only token (`CF_REGISTRAR_READ_TOKEN`). Write operations require a **separate** token (`CF_REGISTRAR_WRITE_TOKEN`).
- Read and write operations are declared in two disjoint sets (`READ_OPERATIONS` and `WRITE_OPERATIONS`) in the CLI.

## Explicit purchase gate

`quote <domain> --years N` (read-only) returns the gate summary:

- domain name
- currency
- first-year price
- renewal price
- estimated total for the term
- irreversible-purchase warning
- a `confirmation_phrase` that encodes domain, years, currency and both prices

`register` only submits when **all** of these are true:

1. Write mode is enabled.
2. The write token is present.
3. The audit log is writable.
4. No local or remote registration exists for the domain.
5. A fresh real-time check still shows the domain as registrable and explicitly `standard` tier (premium, missing or unknown tiers are refused).
6. `--confirm` matches the fresh quote exactly.
7. A human at an **interactive terminal** retypes the domain name after seeing the summary.

Agents have no TTY, so they cannot complete a purchase. They prepare the command and the user runs it. See [purchase-gate.md](skills/cloudflare-registrar/references/purchase-gate.md).

## Idempotency

The normalized domain name is the unique registration key. A write-ahead state record is claimed atomically before submission.

- In-flight, succeeded and unknown-outcome records block resubmission.
- Failed records require an explicit `--retry-after-failure` decision.
- Network errors and 5xx responses are never retried automatically.

## Auditing

Every operation appends a JSON line to the audit log with the following fields:

- `timestamp`
- `operation`
- `mode`
- `status`, one of `started`, `success`, `failure` or `blocked`
- `failure_reason`
- `domain`
- `http_status`

Writes are audit-first: if the log cannot be written, nothing is sent. Details are in [security-and-audit.md](skills/cloudflare-registrar/references/security-and-audit.md).

## Secret handling

- API tokens live **only** in the backend process environment, loaded from the backend secret store. Never put them in client-facing code, mobile/web apps, prompts, repository files, tests, docs or logs.
- There is no command-line option for tokens.
- Token values, `Authorization` headers and token-like strings are redacted from all output, audit entries and state files.
- Use two account-scoped tokens with the least privilege: registrar read for reads, and registrar edit for writes. Provision the write token only when a write is planned. Never use the Global API Key.

## Configuration

| Variable                     | Purpose                                             |
| ---------------------------- | --------------------------------------------------- |
| `CF_ACCOUNT_ID`              | Cloudflare account ID (32 hex characters)           |
| `CF_REGISTRAR_READ_TOKEN`    | Read-only token                                     |
| `CF_REGISTRAR_WRITE_TOKEN`   | Separate write token (only when a write is planned) |
| `CF_REGISTRAR_WRITE_ENABLED` | `true` to allow gated writes. Unset by default.     |
| `CF_REGISTRAR_STATE_DIR`     | Idempotency state directory (optional)              |
| `CF_REGISTRAR_AUDIT_LOG`     | Audit log path (optional)                           |

## Usage

```bash
python3 scripts/cf_registrar.py check example.dev example.app
python3 scripts/cf_registrar.py quote example.dev --years 1
python3 scripts/cf_registrar.py registration-status example.dev
```

## Tests

Unit tests use a fake transport and never touch the network:

```bash
mise run test:python
# or
pytest tools/evals/cloudflare-registrar/scripts/test_cf_registrar.py
```

## Status

The endpoint map follows the official Cloudflare Python SDK. Live verification against a real Cloudflare account has not been performed (`NEEDS_TEST`). Check the endpoints against the current Cloudflare API reference with a read token before the first write.
