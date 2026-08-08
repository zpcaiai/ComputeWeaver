# Incident response

## Severity

- SEV-1: unsafe external action, tenant isolation failure, evidence/signing compromise, total
  control-plane outage or loss beyond RPO.
- SEV-2: sustained SLO breach, connector/solver degradation without unsafe action, delayed jobs.
- SEV-3: contained defect with no safety, isolation or SLO impact.

## Procedure

1. The incident commander declares severity, opens an immutable timeline and assigns operations,
   security and communications owners.
2. For SEV-1, set guarded execution off, scale workers to zero and revoke the active release
   certificate. Do not delete queues, evidence, logs or affected objects.
3. Preserve correlation IDs, audit-chain heads, image digests, configuration versions, database
   timeline and object version IDs. Store secrets separately from evidence.
4. Recover through a tested connector fallback, last-safe plan, isolated restore or prior signed
   release. Validate observed external state before workers resume.
5. Require security and operations approval, a new B20 run and a newly signed certificate before
   re-enabling external writes.
6. Close only after customer impact, root cause, corrective actions, evidence hashes and owners are
   recorded. Complete a blameless review within five business days.
