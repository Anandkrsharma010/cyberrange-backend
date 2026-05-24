# Operations feed — trigger matrix (Phase 2)

Approved map: **event_type** → **severity** → **event_key pattern** → **deep_link**.

All rows include `metadata.schema_version = 1`. Most rows set `metadata.emitter` to the producing module (see column).

| event_type | Default severity | event_key (pattern) | deep_link | Emitter (`metadata.emitter`) |
|------------|------------------|---------------------|-----------|------------------|
| `cohort.run_requested` | info | `cohort-run-requested:{workshop_id}:{deployment_id}` | `/admin/ops/workshop/{workshop_id}` | `course.router` |
| `course.deploy_queued` | info | `course-deploy-queued:{content_id}:{deployment_id}` | `/admin/ops/individual/deployment/{id}` | `course.router` |
| `cohort.invite_created` | info / warning | `cohort-invite-created:{workshop_id}:{invite_id}` | `/admin/ops/workshop/{id}?tab=roster` | `course_invites.router` (warning if email not dispatched) |
| `cohort.invite_resent` | info / warning | `cohort-invite-resent:{workshop_id}:{invite_id}` | same | same |
| `cohort.invite_revoked` | warning | `cohort-invite-revoked:{workshop_id}:{invite_id}` | same | same |
| `workshop.created` | info | `workshop-created:{workshop_id}` | `/admin/ops/workshop/{id}?tab=overview` | `workshops.router` |
| `workshop.updated` | info / warning | `workshop-patch:{workshop_id}:{digest}` | `/admin/ops/workshop/{id}?tab=…` | `workshops.router` |
| `cohort.seat_granted` | info | `cohort-seat-granted:{workshop_id}:{user_id}:{token}` | `/admin/ops/workshop/{id}?tab=roster` | `workshops.router` |
| `workshop.operator_assigned` | info | `workshop-operator-assigned:{workshop_id}:{user_id}` | `/admin/ops/workshop/{id}?tab=assignments` | `workshops.router` |
| `workshop.operator_removed` | info | `workshop-operator-removed:{workshop_id}:{user_id}` | same | same |
| `billing.order_created` | info | `billing-order-created:{internal_payment_id}` | `/admin/billing/payments?user_id=…&status=pending` | `billing.router` |
| `billing.workshop_order_created` | info | `billing-workshop-order-created:{internal_payment_id}` | same | `billing.router` |
| `billing.payment_failed` | warning | `billing-payment-failed:{payment_id}` | `/admin/billing/payments?user_id=…&status=failed` | `razorpay_billing` |
| `billing.workshop_capture_rejected` | warning | `billing-workshop-capture-rejected:{payment_id}` | `/admin/ops/workshop/{id}?tab=billing` | `razorpay_billing` |
| `billing.workshop_payment_captured` | info | `billing-workshop-payment-captured:{payment_id}` | `/admin/ops/workshop/{id}?tab=billing` | `razorpay_billing` |
| `billing.payment_captured` | info | `billing-payment-captured:{payment_id}` | `/admin/billing/payments?user_id=…&status=pending` | `razorpay_billing` |
| `lab.deployment_provisioning` | info | `lab-deploy:{deployment_id}:provisioning` | `/admin/ops/individual/deployment/{id}` | `lab_provisioning_worker` |
| `lab.deployment_running` | info | `lab-deploy:{deployment_id}:running` | same | `lab_provisioning_worker` |
| `lab.deployment_failed` | warning | `lab-deploy:{deployment_id}:failed` | same | `lab_provisioning_worker` |
| `lab.deployment_terminating` | info | `lab-deploy:{deployment_id}:terminating` | same | `lab_cleanup_worker` |
| `lab.deployment_expired` | info | `lab-deploy:{deployment_id}:expired` | same | `lab_cleanup_worker` |
| `lab.deployment_cleanup_failed` | warning | `lab-deploy:{deployment_id}:cleanup_failed` | same | `lab_cleanup_worker` |
| `lab.sysadmin_deploy_self` | info | `sysadmin-lab-queued-self:{deployment_id}` | `/admin/ops/individual/deployment/{id}` | `labs.router` |
| `lab.sysadmin_deploy_for_user` | info | `sysadmin-lab-queued-for-user:{deployment_id}` | same | `labs.router` |

## Idempotency

- All keys above are stable for the same logical fact, or intentionally unique (e.g. seat grant token) to avoid collapsing distinct actions.
- `emit_ops_event` uses `ON CONFLICT (event_key) DO UPDATE` for upserts without resetting `is_read`.

## Validation (Phase 2.4)

**Automated (DB):** from `Backend` in WSL with `.venv_wsl` active:  
`PYTHONPATH=. python scripts/verify_ops_feed_db.py`  
confirms `operations_feed_read_state_chk` exists and there are no inconsistent read rows.

Run in staging with a sys_admin account:

1. For each **event_type** row in the table, perform one action that produces it (or use Razorpay test / worker logs).
2. Open **Admin → Operations feed**; confirm **one** row (or upsert update, not duplicate keys for idempotent rows).
3. Check **severity** matches the table intent.
4. Click **deep_link** → correct page loads.
5. **Mark read** on one row; **Mark all read**; unread count matches list filter.
6. Call **`POST /admin/ops-feed/repair-read-state`** once after upgrades; expect `{"ok": true}` and no DB CHECK violations.

## Email (Phase 2.3)

Implement `OpsFeedEmailAdapter` in `backend/services/ops_feed_email.py` when a worker exists; wire it from that worker only (not from request handlers by default).

## Phase 3 — workflow API (ack / assign / escalate)

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/admin/ops-feed/{id}/acknowledge` | Set `acknowledged_at` / `acknowledged_by` (current sys_admin). |
| `PATCH` | `/admin/ops-feed/{id}/workflow` | Body: `assigned_to_user_id` (UUID or `null` to clear), `escalation` (`none` \| `watch` \| `urgent`). At least one field required. |

Deep links opened from the feed append `?fromFeed=1` (or `&fromFeed=1`) so **Workshop ops**, **Deployment detail**, and **Billing payments** show a **Back to operations feed** banner.
