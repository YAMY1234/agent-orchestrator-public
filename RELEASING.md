# Release process

The private repository is the canonical source tree. Public releases should
contain the same tracked files, without maintaining a separately sanitized
code fork.

## Before a release

1. Keep machine-specific settings, credentials, logs, and task data in ignored
   local files or outside the repository.
2. Run the test suite and syntax checks documented in `CONTRIBUTING.md`.
3. Scan the complete tracked tree for credentials, private paths, internal
   hostnames, and unexpectedly large files.
4. Review the diff since the previous public release.

## Publish the tree, not the private history

Create the public release commit from the exact tree object approved in the
private repository. Give that commit a clean public parent, or no parent for a
new public baseline. Do not copy or merge the private commit graph into the
public repository.

Before publishing, compare the recursive tree listings in both repositories.
Every mode, path, and blob ID must match. A public-only source edit is a release
error and should be made in the canonical repository first.

Publishing, rewriting a public branch, and changing repository visibility each
require an explicit maintainer review.
