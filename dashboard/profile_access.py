"""Share one local Strata installation with selected Hermes profiles."""

import json
import tempfile
from contextlib import contextmanager
from pathlib import Path

from hermes_cli.profiles import get_profile_dir, list_profiles
from hermes_constants import get_hermes_home


def read_state(path):
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("The Strata profile access settings are invalid.")
    return value


def known_profiles():
    return list_profiles(lazy_skill_count=True)


def profile_name(home):
    home = Path(home).resolve()
    return next((profile.name for profile in known_profiles() if profile.path.resolve() == home), None)


def data_directory(home):
    path = Path(home).resolve() / "plugin-data" / "strata-backend"
    if path.resolve() != path:
        raise ValueError("The Strata profile data folder moved outside its Hermes profile.")
    return path


def owner_home(root):
    return root.parent.parent.parent


def runtime_directory(local_data):
    link = read_state(local_data / "shared-runtime.json")
    if not link:
        return local_data / "strata-runtime"
    owner = link.get("owner")
    available = {profile.name for profile in known_profiles()}
    current = profile_name(get_hermes_home())
    if owner not in available or owner == current:
        raise ValueError("The shared Strata installation owner is unavailable.")
    data = data_directory(get_profile_dir(owner))
    if current not in read_state(data / "profile-access.json").get("profiles", []):
        raise ValueError("This Hermes profile no longer has access to the shared Strata installation.")
    return data / "strata-runtime"


def provider_profiles(root):
    owner = owner_home(root)
    targets = read_state(root.parent / "profile-access.json").get("profiles", [])
    profiles = [profile for profile in known_profiles()
                if profile.path.resolve() == owner or (
                    profile.name in targets and read_state(data_directory(profile.path) / "shared-runtime.json").get("owner") == profile_name(owner))]
    if not any(profile.path.resolve() == owner for profile in profiles):
        raise ValueError("The Strata owner profile could not be found.")
    return profiles


@contextmanager
def profile_scope(name):
    from hermes_cli.web_server_profiles import _config_profile_scope

    with _config_profile_scope(name):
        yield


def access_status(root):
    owner = profile_name(owner_home(root))
    current = profile_name(get_hermes_home())
    enabled = {profile.name for profile in provider_profiles(root)}
    return {"owner": owner, "current": current, "can_manage": current == owner,
            "profiles": [{"name": profile.name, "label": profile.bot_title or profile.display_name or profile.name,
                          "enabled": profile.name in enabled, "owner": profile.name == owner}
                         for profile in known_profiles()]}


def defaults_using(root, aliases):
    from hermes_cli.config import load_config_readonly

    users = []
    for profile in provider_profiles(root):
        with profile_scope(profile.name):
            model = load_config_readonly().get("model")
            if (model.get("default") if isinstance(model, dict) else model) in aliases:
                users.append(profile.name)
    return users


def ensure_connector(home, owner):
    from ruamel.yaml import YAML

    directory = Path(home).resolve() / "plugins"
    target = directory / "strata-backend"
    if target.resolve() != target:
        raise ValueError("The target profile's plugin folder moved outside its Hermes home.")
    marker = target / "shared-strata.json"
    if target.exists():
        if marker.is_file():
            if read_state(marker).get("owner") != owner:
                raise ValueError("This profile is already linked to another Strata installation.")
            return
        manifest = YAML(typ="safe").load((target / "plugin.yaml").read_text(encoding="utf-8"))
        if isinstance(manifest, dict) and manifest.get("name") == "strata-backend" and (target / "dashboard" / "profile_access.py").is_file():
            return
        raise ValueError("Update this profile's Strata plugin to a version with profile sharing, then enable access.")
    code = Path(__file__).resolve().parents[1]
    manifest = YAML(typ="safe").load((code / "plugin.yaml").read_text(encoding="utf-8"))
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".strata-link-", dir=directory) as staging:
        package = Path(staging) / "strata-backend"
        dashboard = package / "dashboard"
        dashboard.mkdir(parents=True)
        (package / "plugin.yaml").write_text(
            f"name: strata-backend\nversion: {manifest['version']}\ndescription: Shared Strata installation\n", encoding="utf-8")
        (package / "__init__.py").write_text("def register(ctx):\n    pass\n", encoding="utf-8")
        (dashboard / "manifest.json").write_text(json.dumps({"name": "strata-backend", "api": "plugin_api.py"}), encoding="utf-8")
        (dashboard / "plugin_api.py").write_text(
            f"import runpy\nglobals().update(runpy.run_path({str(code / 'dashboard' / 'plugin_api.py')!r}))\n", encoding="utf-8")
        marker_name = package / marker.name
        marker_name.write_text(json.dumps({"owner": owner}), encoding="utf-8")
        package.rename(target)


def change_access(root, payload, tools, roster):
    from hermes_cli.config import require_readable_config_before_write
    from hermes_cli.plugins_cmd import dashboard_set_agent_plugin_enabled

    status = access_status(root)
    if not status["can_manage"]:
        raise ValueError("Manage shared profile access from the Strata owner profile.")
    name = (payload or {}).get("target_profile", (payload or {}).get("profile"))
    enabled = (payload or {}).get("enabled")
    target = next((profile for profile in known_profiles() if profile.name == name), None)
    if target is None or name == status["owner"] or not isinstance(enabled, bool):
        raise ValueError("Choose another local Hermes profile and whether to enable access.")
    data = data_directory(target.path)
    link_path = data / "shared-runtime.json"
    access_path = root.parent / "profile-access.json"
    previous_link, previous_access = read_state(link_path), read_state(access_path)
    if previous_link and previous_link.get("owner") != status["owner"]:
        raise ValueError("That profile is already using another shared Strata installation.")
    if not previous_link and ((data / "strata-runtime" / "prepared-commit").exists()
                              or any((data / "strata-runtime" / "models").glob("*/model.json"))
                              or any((data / "model-profiles").glob("*.json"))):
        raise ValueError("That profile already has its own Strata data. Use a profile without a separate installation.")
    profiles = set(previous_access.get("profiles", []))
    provider = tools["read_json"](tools["service_root"](root) / "provider.json")
    if not provider.get("key"):
        raise ValueError("Save a Strata model profile and set up its provider before sharing access.")
    with profile_scope(name):
        config = require_readable_config_before_write()
        existing = (config.get("providers") or {}).get("strata-backend")
        if existing and (not isinstance(existing, dict) or existing.get("api_key") != provider["key"]):
            raise ValueError("A different Hermes provider already uses the Strata Backend name.")
        model = config.get("model")
        if not enabled and (model.get("default") if isinstance(model, dict) else model) in roster:
            raise ValueError("Choose a different default model for this bot before removing its Strata access.")
    if enabled:
        profiles.add(name)
        ensure_connector(target.path, status["owner"])
        tools["write_json"](access_path, {**previous_access, "profiles": sorted(profiles)})
        tools["write_json"](link_path, {"owner": status["owner"]})
    else:
        profiles.discard(name)
    try:
        with profile_scope(name):
            result = dashboard_set_agent_plugin_enabled("strata-backend", enabled=enabled)
            if not result.get("ok"):
                raise ValueError(result.get("error") or "Hermes could not update the plugin for this profile.")
            tools["write_provider_config"](provider, roster if enabled else {})
    except BaseException:
        tools["write_json"](access_path, previous_access)
        if previous_link:
            tools["write_json"](link_path, previous_link)
        else:
            link_path.unlink(missing_ok=True)
        raise
    if not enabled:
        link_path.unlink(missing_ok=True)
        tools["write_json"](access_path, {**previous_access, "profiles": sorted(profiles)})
    if not enabled:
        for window in (tools["service_root"](root) / "windows").glob("*.json"):
            state = tools["read_json"](window)
            if state.get("profile") == name:
                tools["write_json"](window, {**state, "closed": True})
    return {"ok": True}
