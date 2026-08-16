# CI ownership policy

GitHub Actions is a verification boundary. It does not implement, repair, commit, push, materialize, or delete tracked production source.

The canonical permanent workflow inventory is `tools/architecture_checks/workflows.toml`. Each registered workflow must declare top-level `permissions.contents: read`. Controller chains, remote workflow dispatch, write permissions, job-level permission escalation, unregistered YAML workflows, and mutating Git commands are rejected by `tools/architecture_checks/check_workflows.py`.

A new deployable or owner proof adds its permanent workflow and registry entry in the same owner change. Temporary apply, repair, capture, reconciliation, and self-deleting workflows are not valid repository architecture.
