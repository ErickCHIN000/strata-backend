# Strata Backend

Strata Backend is a self-contained Hermes Desktop plugin. Its sidebar entry opens the **Strata Backend** workspace page. The version pill in the bottom-right status bar checks the installed checkout's tracked GitHub branch every two minutes while Hermes is open. Click the current version to check immediately, or click the update badge to apply an available update.

## Install

```sh
hermes plugins install https://github.com/ErickCHIN000/strata-backend
hermes plugins enable strata-backend
```

In Hermes Desktop, open **Capabilities → Plugins**, rescan, and enable the Desktop contribution if needed. The Python backend provides the version check and updater. The Desktop module loads directly in Hermes without a build step.

## Set up Strata

Open **Strata Backend** in the sidebar and choose **Download and set up**. The plugin tracks [`Niko1221/Strata` on `main`](https://github.com/Niko1221/Strata/tree/main), builds the checked-out engine source, and installs the Python dependencies in a private `.venv`. Model selection is separate. Engine setup needs the toolchain for the selected GPU. The page shows the setup log while it runs.

Use **Update Strata** to fast-forward the managed checkout and run setup again. **Fresh wipe** removes only the managed Strata directory in this Hermes profile, including its venv, downloaded models, build products, and setup files.

Once the engine is ready, **Suggested models** shows Strata's hardware-based recommendation and its catalog. Choose **Download** on a specific entry to fetch its weights. **Open models folder** opens the local profile's managed downloads in the file manager, including when the folder is empty. The page lists download size separately from the memory estimate. Downloads do not start a model or change the Hermes provider.

The transfer panel shows bytes received, the current shard, verification, and completion. **Pause download** keeps the partial file. **Resume download** requests the remaining bytes and verifies the finished file before adding it to the library. A transfer started by v0.3.4 or earlier uses the old worker and needs to be started again to gain these controls.

Under **Hugging Face models**, enter an `owner/repo` and optionally an exact GGUF filename or quantization. For example, enter `unsloth/Qwen3.8-27B-GGUF` and `Qwen3.8-27B-UD-Q4_K_M.gguf`. The picker groups complete shard sets. The Hub client runs in the managed venv and uses the existing Hugging Face login and cache. External GGUF downloads are listed with compatibility unverified.

The DavidAU Flash-Next shortcut points to two raw `IQ3_S` shards and a separate vision projector. The downloaded model needs a Strata pack prepared separately before a compatibility test.

**Model profiles** save common memory and context settings, raw Strata engine, launch, setup and server arguments, environment variables, and run config overrides. The option reference comes from the installed build. The Model picker lists completed downloads, and **Any model** creates a reusable preset. Fresh wipe preserves profiles and the shared Hugging Face cache.

## Use a profile in Hermes

1. Save a profile linked to a downloaded model. Strata Backend registers eligible profiles in the current Hermes model picker automatically.
2. Select the profile in the model picker. Strata prepares its pack from the downloaded GGUFs and starts the local server. A restored picker value stays cold until you send a prompt to it or make a new selection. The first prompt waits for launch. For the baseline, vision and the optional draft download are off. On Windows, Strata uses resident low-RAM mode when available commit is smaller than the expert arena unless the profile specifies a low-RAM mode.
3. The plugin follows confirmed switches in live sessions. Selecting another Strata profile unloads the previous one. Choosing a cloud model unloads the model and keeps the lightweight provider available for later selection. Closing the final Desktop window releases both. If the close signal is interrupted, the backend cleans up after the last heartbeat expires.

**Strata processes** lists managed instances and detected external Strata engines or servers. **Stop process** unloads the selected managed model until you choose a different model and select it again. Automatic unloading applies to plugin-managed instances. Profile edits take effect the next time the instance starts. Managed source serving currently supports GGUF weights that Strata’s own-files setup recognizes. Separate-engine and ready-pack formats need their own serving setup.

On upgrading from v0.2.2 or earlier, use the **Restart gateway** button in the Strata tab to activate the new Python router once. Updates to the plugin's runtime handlers are then picked up on the next request through that router.

## Project layout

- `plugin.yaml` and `__init__.py` define the Hermes Python plugin.
- `dashboard/plugin_api.py` mounts stable Hermes routes. `dashboard/runtime_api.py` handles setup, updates, and model catalog requests.
- `desktop/plugin.js` registers the workspace page, sidebar entry, and status bar pill.
- `.codex/` and `.agents/` are reserved for project-specific agent configuration.
