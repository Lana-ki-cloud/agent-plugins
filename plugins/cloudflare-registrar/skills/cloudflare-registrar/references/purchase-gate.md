# Purchase and Configuration-Change Gate

Cloudflare domain registrations are **non-refundable**. The account's default payment method is charged immediately. Every registration must stay a manual, explicit, human decision.

## Gates enforced by the CLI

A `register` request reaches Cloudflare only when **all** of the following are true. Each failed gate exits with code `2` (`blocked`) and nothing is submitted.

| #   | Gate                 | Enforcement                                                                                                     |
| --- | -------------------- | --------------------------------------------------------------------------------------------------------------- |
| 1   | Write mode enabled   | Operator set `CF_REGISTRAR_WRITE_ENABLED=true` on the backend. Default is unset, which means read-only.         |
| 2   | Separate write token | `CF_REGISTRAR_WRITE_TOKEN` is present. The read token is never used for writes.                                 |
| 3   | Audit log writable   | A `started` audit entry is written before anything else. If that fails, the command aborts.                     |
| 4   | Idempotency          | No local record for the domain, and no existing registration in the Cloudflare account.                         |
| 5   | Fresh quote          | `domain-check` runs again. Must be `registrable: true` and tier `standard` (premium/missing/unknown refused).   |
| 6   | Confirmation phrase  | `--confirm` must equal the current quote's `confirmation_phrase`. Any price, currency or term change breaks it. |
| 7   | Human at a terminal  | The CLI shows the purchase summary on an interactive TTY and the human must retype the domain name.             |

Gate 7 is checked twice: TTY presence right after gates 1–2, before any request with the write token, and again at confirmation. An agent therefore cannot complete a purchase, and it cannot make any write-token request either, because agent shells have no interactive terminal.

`update-domain` uses gates 1, 2, 3 and 7. Its gate 7 shows the requested changes, but the human only retypes the domain name, so the human must read the change summary before confirming. It has no idempotency or quote checks.

The gates assume the CLI is run as a command. `main()` accepts a `confirm_fn` hook for tests only; backend code must not inject one.

## Purchase summary (what the user must see)

`quote` returns these fields. Show all of them to the user before asking for confirmation:

```json
{
  "domain": "example.dev",
  "years": 1,
  "currency": "USD",
  "first_year_price": "10.18",
  "renewal_price": "12.18",
  "estimated_total_for_term": "10.18",
  "tier": "standard",
  "auto_renew": false,
  "warning": "WARNING: Domain registration is an irreversible purchase. ...",
  "confirmation_phrase": "REGISTER example.dev YEARS=1 USD FIRST_YEAR=10.18 RENEWAL=12.18"
}
```

The values above are illustrative and are not real prices.

- `first_year_price` applies to the first year. `renewal_price` applies to each further year and to renewals.
- Registry prices can change. Pricing is re-checked at submission, and a stale `--confirm` phrase is rejected.
- Registrations are submitted with `auto_renew=false` and WHOIS `privacy_mode=redaction`. Enable auto-renew later with `update-domain` if the user wants it.
- Some extensions require a minimum term (for example, 2 years). If the registry rejects the term, the registration fails and is recorded. It is not retried automatically.

## Agent script

1. Run `quote <domain> --years N`.
2. Show the summary and the warning verbatim. Ask: "Do you want to buy this domain at these prices? This is non-refundable."
3. If the user agrees, give them this command to run themselves on the backend host:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/cf_registrar.py" register <domain> --years N --confirm "<confirmation_phrase>"
   ```

4. Do not run it. Do not set `CF_REGISTRAR_WRITE_ENABLED` or any token yourself.
5. When the user reports back, run `registration-status <domain>`.

## Workflow states

| State             | Meaning                                             | Agent action                                           |
| ----------------- | --------------------------------------------------- | ------------------------------------------------------ |
| `pending`         | Accepted, awaiting processing                       | Poll `registration-status` later                       |
| `in_progress`     | Processing                                          | Poll `registration-status` later                       |
| `action_required` | Waiting for user action (see `context.action`)      | Stop polling, tell the user what is needed             |
| `blocked`         | A third party (registry) is blocking progress       | Poll later, inform the user                            |
| `succeeded`       | Terminal: domain registered                         | Report success                                         |
| `failed`          | Terminal: see `error.code` / `error.message`        | Report the reason. **Do not retry.** The user decides. |
| `unknown` (local) | Network error or 5xx: the outcome was not confirmed | Run `registration-status`. Never resubmit.             |

## Retrying after a failure

A `failed` record blocks every new `register` for that domain. Only after the user has reviewed the failure reason may they add `--retry-after-failure` to their manual command. All gates still apply. An `unknown` or in-flight record cannot be overridden with this flag. Check `registration-status` first.
