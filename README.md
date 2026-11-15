# Strata Backend

Strata Backend is a self-contained Hermes Desktop plugin. Its sidebar entry opens the **Strata Backend** workspace page. The version pill in the bottom-right status bar checks the installed checkout's tracked GitHub branch and offers a one-click update when the branch can fast-forward.

## Install

```sh
hermes plugins install https://github.com/ErickCHIN000/strata-backend
hermes plugins enable strata-backend
```

In Hermes Desktop, open **Capabilities → Plugins**, rescan, and enable the Desktop contribution if needed. The Python backend provides the version check and updater. The Desktop module loads directly in Hermes without a build step.

## Set up Strata

Open **Strata Backend** in the sidebar and choose **Download and set up**. The plugin tracks [`Niko1221/Strata` on `main`](https://github.com/Niko1221/Strata/tree/main), builds the checked-out engine source, and installs the Python dependencies in a private `.venv`. Model selection is separate. Engine setup needs the toolchain for the selected GPU. The page shows the setup log while it runs.

Use **Update Strata** to fast-forward the managed checkout and run setup again. **Fresh wipe** removes only the managed Strata directory in this Hermes profile, including its venv, downloaded models, build products, and setup files.

Once the engine is ready, **Suggested models** shows Strata's hardware-based recommendation and its catalog. Choose **Download** on a specific entry to fetch its weights. The page lists download size separately from the memory estimate. Downloads do not start a model or change the Hermes provider.

Under **Hugging Face models**, enter an `owner/repo` and optionally an exact GGUF filename or quantization. For example, enter `unsloth/Qwen3.8-27B-GGUF` and `Qwen3.8-27B-UD-Q4_K_M.gguf`. The picker groups complete shard sets. The Hub client runs in the managed venv and uses the existing Hugging Face login and cache. External GGUF downloads are listed with compatibility unverified.

The DavidAU Flash-Next shortcut points to two raw `IQ3_S` shards and a separate vision projector. The downloaded model needs a Strata pack prepared separately before a compatibility test.

**Model profiles** save common memory and context settings, raw Strata engine, launch, setup and server arguments, environment variables, and run config overrides. The option reference comes from the installed build. Profiles can be associated with a model or saved as reusable presets. Fresh wipe preserves profiles and the shared Hugging Face cache.

On upgrading from v0.2.2 or earlier, use the **Restart gateway** button in the Strata tab to activate the new Python router once. Updates to the plugin's runtime handlers are then picked up on the next request through that router.

## Project layout

- `plugin.yaml` and `__init__.py` define the Hermes Python plugin.
- `dashboard/plugin_api.py` mounts stable Hermes routes. `dashboard/runtime_api.py` handles setup, updates, and model catalog requests.
- `desktop/plugin.js` registers the workspace page, sidebar entry, and status bar pill.
- `.codex/` and `.agents/` are reserved for project-specific agent configuration.
