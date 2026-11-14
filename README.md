# Strata Backend

Strata Backend is a self-contained Hermes Desktop plugin. Its sidebar entry opens the **Strata Backend** workspace page. The version pill in the bottom-right status bar checks the installed checkout's tracked GitHub branch and offers a one-click update when the branch can fast-forward.

## Install

```sh
hermes plugins install https://github.com/ErickCHIN000/strata-backend
hermes plugins enable strata-backend
```

In Hermes Desktop, open **Capabilities → Plugins**, rescan, and enable the Desktop contribution if needed. The Python backend provides the version check and updater. The Desktop module loads directly in Hermes without a build step.

## Project layout

- `plugin.yaml` and `__init__.py` define the Hermes Python plugin.
- `dashboard/` exposes the version check and update endpoints.
- `desktop/plugin.js` registers the workspace page, sidebar entry, and status bar pill.
- `.codex/` and `.agents/` are reserved for project-specific agent configuration.
