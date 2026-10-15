# Secrets, Least Privilege, Auditing and Idempotency

## Secret handling

- Tokens are read **only** from environment variables on the backend host that runs the CLI. There is no command-line flag for tokens.
- Never put tokens in client-facing code, browser/mobile apps, prompts, chat, repository files, tests, docs or logs.
- Load tokens from the backend secret store (for example, a secrets manager) into the CLI's process environment only. Do not write them into shell history or `.env` files that are committed.
- The CLI redacts known token values, `Authorization` header values and long token-like strings from every printed error, audit entry and state record.
- Transfer auth/EPP codes are secrets too. `transfer-check` deliberately does not accept them, and transfer-in is not supported by this plugin. Do transfers in the Cloudflare dashboard.

## Configuration (backend environment)

| Variable                     | Required for | Purpose                                                                   |
| ---------------------------- | ------------ | ------------------------------------------------------------------------- |
| `CF_ACCOUNT_ID`              | all          | 32-character hex Cloudflare account ID (not a secret, but keep it scoped) |
| `CF_REGISTRAR_READ_TOKEN`    | reads        | Read-only API token                                                       |
| `CF_REGISTRAR_WRITE_TOKEN`   | writes       | Separate write token. Provision it only when a write is planned.          |
| `CF_REGISTRAR_WRITE_ENABLED` | writes       | Must be `true` to allow writes. Unset (default) means read-only.          |
| `CF_REGISTRAR_STATE_DIR`     | optional     | Idempotency state directory (default `~/.local/state/cf-registrar`)       |
| `CF_REGISTRAR_AUDIT_LOG`     | optional     | Audit log path (default `<state dir>/audit.jsonl`)                        |

## Least privilege

- Create **two** account-scoped Cloudflare API tokens restricted to the single Registrar account:
  - a read token with registrar **read** permission only,
  - a write token with registrar **edit** permission only, created just before a planned write and revoked or rotated afterwards.
- Do not use the Global API Key. Do not add zone, DNS, Workers, R2 or other permissions.
- Restrict token client IPs to the backend host where Cloudflare supports it, and set a token expiry.
- Leave `CF_REGISTRAR_WRITE_ENABLED` unset in normal operation.

## Audit log

Every invocation appends JSON lines to the audit log (file mode `0600`, directory `0700`). Each entry has exactly these fields:

| Field            | Description                                                        |
| ---------------- | ------------------------------------------------------------------ |
| `timestamp`      | UTC ISO-8601 with milliseconds, e.g. `2026-10-15T09:00:00.000Z`    |
| `operation`      | CLI operation, e.g. `check`, `register`, `register.submit`         |
| `mode`           | `read` or `write`                                                  |
| `status`         | `started`, `success`, `failure` or `blocked`                       |
| `failure_reason` | Redacted reason for `failure`/`blocked`, otherwise `null`          |
| `domain`         | Normalized domain when the operation targets one, otherwise `null` |
| `http_status`    | HTTP status of a failed API call, otherwise `null`                 |

Write operations are **audit-first**: a `started` entry is written before any gate that talks to Cloudflare. If it cannot be written, the command aborts without contacting Cloudflare. A successful registration produces `register/started`, `register.submit/started` and `register/success`.

Example (illustrative):

```json
{"domain": "example.dev", "failure_reason": "write operations are disabled (read-only default); set CF_REGISTRAR_WRITE_ENABLED=true to enable", "http_status": null, "mode": "write", "operation": "register", "status": "blocked", "timestamp": "2026-10-15T09:00:00.000Z"}
```

Ship the audit log to the Lana KI control plane's central log store with an append-only collector. Do not edit it in place.

## Idempotency

- The normalized domain name (lowercase, punycode, no trailing dot) is the unique registration key, matching Cloudflare's own one-registration-per-domain rule.
- Before submission, the CLI atomically creates `<state dir>/<domain>.json` with state `submitted` (write-ahead), under an exclusive `<domain>.lock` file. A second or concurrent submission for the same domain is blocked while any record or lock exists.
- Every local state write (including the `registration-status` sync and the remote readback) takes the same lock. A remote readback only records a registration when no local record exists, so it never overwrites a submission or a failure reason.
- A stale lock (no process running) is never removed automatically. Check `registration-status` before deleting it manually.
- On `--retry-after-failure`, the failed record is moved to `<state dir>/archive/` (never deleted), and the new record references it under `previous_failure`.
- It also reads back `GET /registrations/<domain>`. If the domain is already registered in the account, it is blocked and recorded as `succeeded`.
- Outcomes:
  - HTTP 4xx → `failed` with the reason. Requires a human decision (`--retry-after-failure`).
  - Network error or HTTP 5xx → `unknown`. Never retried. Run `registration-status`.
  - Accepted → the workflow `state` (`pending`, `in_progress`, ...). `registration-status` keeps it in sync.
- A corrupt state file is treated as `unknown`, never as "no record".
- If the state file cannot be updated after a successful submission, the command reports a failure, keeps the `submitted` record (domain stays locked), and asks for `registration-status`.

## Endpoints used

All requests go to `https://api.cloudflare.com/client/v4/accounts/{account_id}/registrar/...`. The CLI refuses any other host.

| Operation             | Method and path                                  | Mode  |
| --------------------- | ------------------------------------------------ | ----- |
| `search`              | `GET domain-search`                              | read  |
| `check`, `quote`      | `POST domain-check` (no side effects)            | read  |
| `transfer-check`      | `POST domain-transfer-check` (no side effects)   | read  |
| `list-domains`        | `GET domains`                                    | read  |
| `get-domain`          | `GET domains/{domain}`                           | read  |
| `list-registrations`  | `GET registrations`                              | read  |
| `get-registration`    | `GET registrations/{domain}`                     | read  |
| `registration-status` | `GET registrations/{domain}/registration-status` | read  |
| `update-status`       | `GET registrations/{domain}/update-status`       | read  |
| `list-extensions`     | `GET extensions`                                 | read  |
| `get-extension`       | `GET extensions/{extension}`                     | read  |
| `register`            | `POST registrations` (`Prefer: respond-async`)   | write |
| `update-domain`       | `PUT domains/{domain}`                           | write |

The endpoint map follows the official Cloudflare Python SDK (`cloudflare.resources.registrar`). Check it against the current Cloudflare API reference before the first live use.
