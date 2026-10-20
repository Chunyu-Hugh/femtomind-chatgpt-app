# Published-analysis layout and deferred Ceph relocation

## Source layout

`analyses/<analysis>/catalog/` holds the three scientific catalog documents.
`analyses/<analysis>/results/` holds the index, precomputed compact statistics,
documentation and archive support scripts. Large immutable arrays belong only
in its ignored `data/<archive-id>/` directory on Ceph.

Python modules, MCP tool names and schema names are unchanged. Loaders use the
analysis folder (`JAM24`), not the subfolder (`catalog`/`results`), as the logical
identity. Moving the unchanged catalog bytes therefore preserves catalog hashes.
Archive identifiers and `results_ref` revisions do not depend on physical paths.

Tests live in a sibling `femtomind-tests/` directory, with an explicit
`FEMTOMIND_APP_ROOT`. They are not application or deployment files. Their prior
Git history remains available at the source commits recorded by that suite.

## Current storage: leave unchanged until separately authorized

Primary, existing directory:

`/w/ceph24/JAM/treyand/femtomind-chatgpt-app/reference_results/JAM24/data/97a675d1e7880ece/`

Alternate, prepared destination (not yet populated):

`/w/ceph24/JAM/treyand/femtomind-chatgpt-app/analyses/JAM24/results/data/97a675d1e7880ece/`

The index retains the primary and adds the tightly validated alternate address.
Only these paired layouts in the same project are accepted. The reader falls
back only if the primary directory is absent. Corruption, inaccessible files or
symlinks are errors. It never searches other projects or accepts client paths.

Both locations remain ignored during transition. Never use `git add -f`, reset,
clean, recursive synchronization with deletion, or an archive rebuild to migrate
this data. Ordinary Git renames/synchronization do not move ignored arrays.

## Ordered procedure after separate approval

1. Promote the reorganized source and safely fast-forward the JLab application
   checkout, inventorying its untracked/ignored files first. Preserve the old
   archive and transitional ignore rules. Handle the existing identical legacy
   ignore file without touching its arrays. Do not synchronize scientific data.
2. The owner deploys this compatibility-capable code and new `analyses/` metadata
   to **jam-gateway-treyand** using its established deployment mechanism. Both
   packagers allowlist only the three catalog documents and results index; never
   recursively bundle `analyses/` or any `data/` tree. Check staged imports and
   rollback before activation. Verify all six catalog/results tools, retrieving
   reference results successfully from the still-existing primary directory.
3. Quiesce reference-result retrieval requests for the short relocation window.
   Inventory all 297 archive files and checksums (109,517,099 bytes). Confirm the
   source's real path/ownership, that the destination leaf does not exist, and
   that both parents are on the same Ceph filesystem. Create only the approved
   destination parent if needed; do not merge directories or replace a target.
4. Rename the single `97a675d1e7880ece` directory from the old `data/` parent to
   the new `data/` parent on Ceph. Do not copy, regenerate or edit its contents.
   Do not move the original JAM24 source files or change FitPack/campaign paths.
5. Verify all file counts, byte counts and checksums again. The archive manifest
   must still hash to
   `d5f4f6b6ecc5e0229dbe0202df24c1c4ea531f64d03ee3e4afa5140e0cf9a895`.
   Ensure Git still ignores every numerical file. Existing deployed readers now
   use the registered alternate automatically; no second backend reload is
   required for this address transition. Verify all 96 datasets, 5,853 points,
   normalization/statistics values and representative ordered predictions.
6. If verification fails, quiesce retrieval again, verify the old leaf is absent,
   and rename the same directory back. Keep the compatibility deployment and
   recoverable previous snapshot. Do not overwrite either location. Resume
   retrieval only after integrity checks pass. An in-flight request crossing the
   rename can fail explicitly and should be retried; no data are substituted.
7. After successful review, a separate metadata change may make the new location
   primary and retire the alternate. That changes the locator-manifest checksum,
   not archive hashes or scientific values. No such cleanup is part of the
   current uncommitted change.

No storage relocation, deployment, FitPack modification, scientific calculation,
campaign creation or job submission is needed to validate this source change.
Historical builder hashes in immutable provenance stay unchanged even though
development scripts now understand the new layout.
