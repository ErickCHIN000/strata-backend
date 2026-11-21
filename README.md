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

The **Use SC117 Flash-Next IQ3_S** shortcut fills the model repository and quantization. For custom Qwen Flash-Next GGUFs, the first launch builds a separate text-only pack using the model’s own tokenizer. Custom packs use the profile’s Engine and Server arguments. Setting the run config’s `drafting` to `mtp` uses the already-prepared shared Flash-Next MTP layer.

**Model profiles** save common memory and context settings, raw Strata engine, launch, setup and server arguments, environment variables, and run config overrides. The option reference comes from the installed build. The Model picker lists completed downloads, and **Any model** creates a reusable preset. Fresh wipe preserves profiles and the shared Hugging Face cache.

For files downloaded with `hf download --local-dir`, click **Add Model** under **Models**, enter the full folder or GGUF path, then **Scan folder**. Choose the complete shard set, optionally attach an `mmproj` GGUF found in that folder, and click **Add Model**. For example, scan `F:\Models\Huihui-Flash-Next-v2` and choose the two files under `swift-IQ3_S`. The entry uses the existing files and appears in the profile’s Model dropdown. Complete downloads are required. Add the same model again to update its display name or projector.

The profile form displays the attached projector path. Choose **GPU** or **CPU** under **Image input**, save the profile, and load it to prepare the vision reader. Its first build uses Strata’s existing compiler setup and can take several minutes. GPU image input uses additional VRAM. **Off** serves text only.

Use **Delete model** under **Models** to remove a managed download. Locally added files have **Remove model**, which removes their library registration and keeps the original files in place. Use **Delete profile** under **Model profiles** to remove the selected preset. These actions ask for confirmation. Stop a running profile and switch the Hermes default away from it before deleting it or its download.

## Use a profile in Hermes

1. Save a profile linked to a downloaded model. Strata Backend registers eligible profiles in the current Hermes model picker automatically.
2. Select the profile in the model picker. Strata prepares its pack from the downloaded GGUFs and starts the local server. A restored picker value stays cold until you send a prompt to it or make a new selection. The first prompt waits for launch. For the baseline, vision and the optional draft download are off. On Windows, Strata uses resident low-RAM mode when available commit is smaller than the expert arena unless the profile specifies a low-RAM mode.
3. The plugin follows confirmed switches in live sessions. Selecting another Strata profile unloads the previous one. Choosing a cloud model unloads the model and keeps the lightweight provider available for later selection. Closing the final Desktop window releases both. If the close signal is interrupted, the backend cleans up after the last heartbeat expires.

**Strata processes** lists managed instances and detected external Strata engines or servers. Running managed profiles show live timing when available, the three most recent requests, and current system RAM and GPU VRAM usage. **Stop process** unloads the selected managed model. Send a new message to the same profile or select it again to load it with its latest saved settings. Automatic unloading applies to plugin-managed instances. Separate-engine and ready-pack formats need their own serving setup.

## Share Strata with other bots

Open **Profile access** in the Strata tab of the profile that owns the installation. Check each local Hermes bot/profile that should use it. Hermes enables a small connector in that profile and registers the same Strata provider and saved model presets. The common installation supplies the Python environment, model files, native packs, and model profiles. New and edited presets refresh the enabled profiles’ provider lists.

Other bots can select a Strata preset in their model pickers and send messages to load it. Shared bot requests run in order on the same engine, including when another bot is focused. The model stays loaded between replies. Explicit Stop, switching models, and closing the last Desktop window still release the selected model. The lightweight shared provider remains available to enabled bots. Remove a bot’s access with its checkbox after choosing a different default model for that bot.

On upgrading from v0.2.2 or earlier, use the **Restart gateway** button in the Strata tab to activate the new Python router once. Updates to the plugin's runtime handlers are then picked up on the next request through that router.

## Project layout

- `plugin.yaml` and `__init__.py` define the Hermes Python plugin.
- `dashboard/plugin_api.py` mounts stable Hermes routes. `dashboard/runtime_api.py` handles setup, updates, and model catalog requests.
- `desktop/plugin.js` registers the workspace page, sidebar entry, and status bar pill.
- `.codex/` and `.agents/` are reserved for project-specific agent configuration.
