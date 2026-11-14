# Strata Backend

This repository is a unified Hermes plugin. `plugin.yaml` lives at the repository root so Hermes can install the package directly from GitHub.

<!-- codebase-memory-mcp:start -->
For structural codebase exploration, use the installed `codebase-memory` skill.
<!-- codebase-memory-mcp:end -->

- Put Python logic in the plugin backend. Keep `desktop/plugin.js` as uncompiled ESM for the Hermes Desktop SDK.
- Verify changes through the Hermes plugin entry points. Use a disposable checkout to test updates.
