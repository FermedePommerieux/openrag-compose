# Pommerieux personal ingestion and archive migration

Status: **prepared, not executed**. The target account is `eloiprimaux`, role
`admin`, with mandatory replacement of the operator-provided temporary password
before workspace access. No password belongs in this file, a plan, command-line
arguments, shell history, logs, or GitOps.

The existing first-run onboarding and local-user administration remain available.
Pommerieux is an existing installation, so its closed browser onboarding must
not be reopened to create this administrator; use the operator bootstrap.

## Observed storage and destination

The read-only filesystem/schema inventory on 2026-09-05 found:

| Item | Existing | Planned destination |
|---|---|---|
| Files awaiting ingestion and legacy ancillary files | `/shared/openrag-documents/`, excluding the archive | `/shared/openrag-documents/eloiprimaux/ingestion/` |
| Archived originals | `/shared/openrag-documents/.openrag-indexed/` | `/shared/openrag-documents/eloiprimaux/archives/` |
| Application identity authority | `/data/data/openrag.db` | Same database |

There are 406 regular files totaling 817,494,400 bytes, including 282 archived
files totaling 583,253,907 bytes. This count includes ancillary files such as
hidden metadata; it is not a count of indexed documents. The inventory found no
symlinks and no existing local account named `eloiprimaux`. Existing anonymous
and API-key identities must be preserved. Paths below each source root and
source download IDs are retained. No Unix account or blanket `chown` is proposed.

These observations are preparation evidence, not a quiesced migration manifest.
Checksums, exact chunk ACL journals and metadata-generation consistency must be
captured again before cutover. Do not move files just by changing a path setting:
ownerless indexed documents and metadata must cross the same authorization boundary.

## Bounded read-only planner

`scripts/plan_local_storage_migration.py` creates a JSON plan with file hashes,
size/mtime/mode information, a reserved target UUID, proposed chunk ACL changes,
source locators, metadata identity changes and rollback steps. It never creates
an account, moves a file, updates an index/alias, or changes authentication.

Run it **outside the serving backend pod**, in a separately limited maintenance
process with read-only access to the application database and source volumes.
Use the backend configuration and the same absolute paths. For example, inside
that prepared maintenance environment:

```sh
python scripts/plan_local_storage_migration.py \
  --login eloiprimaux \
  --documents /shared/openrag-documents \
  --archive /shared/openrag-documents/.openrag-indexed \
  --database /data/data/openrag.db \
  --include-index \
  --output /tmp/eloiprimaux-migration-plan.json
```

Review and retain the generated UUID; pass `--user-id` on subsequent captures
so the reviewed identity does not change. The tool limits Linux process address
space to 384 MiB, HTTP responses to 2 MiB, and scroll pages to 25 compact rows.
A count preflight declines to materialize more than 50,000 chunk changes and
marks the plan incomplete. Larger corpora require separate bounded batch
journals before execution; do not treat an incomplete plan as applicable or
raise its limits inside a serving pod. Original full metadata payloads remain
in the immutable source generation; the plan only records compact identities,
ACLs and integrity digests.

Foreign owners, explicit sharing, mixed ownership for one document, pre-existing
destinations, symlinks, unexpected archive layouts and hash mismatches require
review. The planner does not silently grant their contents to the new admin.
It is a preparation tool, not an automatic migration executor.

## Coordinated cutover

1. Resolve every plan blocker and all original multi-user production gates.
   Coverage for cross-user provenance and the deployed Agent metadata-tool
   contract are still blockers from the earlier auth validation. Keep identity
   occurrence/generation v1 and the unrelated 652k-chunk identity migration off.
2. Put ingress behind maintenance access. Pause uploads, connectors, reindexing
   and every other writer. Take a consistent application SQL backup and an
   OpenSearch snapshot; record the current metadata alias target and code images.
3. Upgrade with the compatible candidate while maintenance access is retained.
   Create the reviewed administrator with the reserved UUID in the existing DB:

   ```sh
   OPENRAG_AUTH_MODE=local OPENRAG_RBAC_ENFORCE=true PYTHONPATH=src \
     python -m auth.local_admin bootstrap eloiprimaux \
       --user-id <reviewed-random-uuid-v4> --require-password-change
   ```

   Enter the user's approved temporary password at the masked prompts. It is
   deliberately not written here. Verify the created UUID and admin role.
   If bootstrap was already consumed, stop and use reviewed existing-admin or
   recovery operations; never remove the bootstrap marker to bypass it.
4. Re-capture the complete file and ACL journals under the write pause. Abort on
   any size, timestamp, hash, sequence-number or primary-term drift. Insert the
   `user_storage` binding for this UUID and `eloiprimaux` before copying legacy
   data, using the existing application DB and logging only identifiers.
5. Copy into the two personal directories without overwrite. Preserve filenames
   and source IDs, verify every SHA-256, and register `source_archive_locations`
   against the same user ID. Preserve originals in a maintenance-only backup
   outside every ingestion root. Verify service/host deposit permissions.
6. Transfer only reviewed legacy local ACLs to this UUID using per-document
   sequence-number/primary-term guards. Record returned CAS values for rollback.
   Preserve all content, `document_id`, chunk IDs, source URLs, provenance,
   profiles, citations and existing foreign/external ownership.
7. Clone the immutable metadata generation, retaining unrelated rows unchanged.
   Rebuild affected projection IDs with the existing canonical formula, using
   the new owner and original source document/entity IDs. Copy the full `filter`
   unchanged; validate its existing digests. Do not mutate the old immutable
   generation. Verify counts/DLS, then atomically switch the alias.
8. Check actual local user A/B isolation, archived downloads and citations,
   metadata counts, provenance coverage, Agent/streaming and RuntimeBehavior.
   Verify the temporary password only permits password replacement. Configure
   the validated local mode, remove legacy duplicates from searchable ingestion
   roots, then reopen traffic only when every gate passes.

## Rollback

Keep writes paused. Verify the destination hashes and journaled post-write CAS
values before undoing anything; stop if data changed. Restore the original
metadata alias and recorded ACL fields, including removing fields absent before
migration. Restore verified original files and SQL locator/mapping state from
the same maintenance snapshot. Delete only this run's verified copies after
restoration. Keep both metadata generations and evidence until acceptance.

Do not switch a live authenticated installation to public no-auth for recovery.
Downgrade guards reject remaining archive locators and temporary credentials;
forward repair or a controlled restore behind maintenance access is preferred.

## Inventory incident and limit of evidence

The first full-index preparation attempt was run inside the serving backend pod
and exceeded memory. Kubernetes reported OOMKilled at 2026-09-05 11:20:36 UTC;
the backend restarted at 11:20:37 UTC. The cause was an unbounded in-memory
inventory. This was an operational incident despite the command being read-only.

Subsequent checks showed the backend Ready/Running, one restart, legacy no-auth
still selected and RuntimeBehavior MATCH. No account, file move, index ACL,
metadata alias or deployment activation was applied by this attempt. The full
migration manifest was not produced. Bulk production inventory was suspended;
the replacement planner's response, page, process and row bounds were tested
locally. Complete the remaining journal capture in the separate maintenance
environment described above before any migration.
