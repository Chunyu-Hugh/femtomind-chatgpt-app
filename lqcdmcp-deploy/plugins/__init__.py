"""Physics-task plugins for the gateway.

Each plugin is a folder plugins/<name>/ with a plugin.py that defines:

  NAME          the folder name; every tool it adds is named <NAME>_<something>
  SHIP          paths (relative to the folder, glob patterns allowed) that the
                job script and hot_redeploy.py carry to the gateway node
  INSTRUCTIONS  text appended to the gateway's MCP instructions
  load()        called once at gateway start; raises if the plugin cannot run
  tools()       {tool name: spec}, spec as in jam_gateway.TOOLS
                (desc, schema, fn, optional meta and annotations)
  resources()   {uri: {"name", "mimeType", "description", "text": callable, "meta"}}

The gateway (JAM) itself is not a plugin and is unchanged by them. A plugin
that fails to load is left out as a whole and the gateway runs without it.
JAM_GW_PLUGINS chooses plugins: unset loads every folder found, "none" or ""
loads none, "a,b" loads those. The gateway, the job script and
hot_redeploy.py load this file by path, as module jam_plugins, so the
generic folder name never shadows another package.
"""

import glob
import importlib.util
import os
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
TOOL_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")


def _available():
    return sorted(n for n in os.listdir(ROOT)
                  if os.path.isfile(os.path.join(ROOT, n, "plugin.py")) and re.fullmatch(r"[a-z][a-z0-9]*", n))


def selected():
    """The plugin names JAM_GW_PLUGINS asks for (default: all found)."""
    asked = os.environ.get("JAM_GW_PLUGINS")
    if asked is None:
        return _available()
    if asked.strip().lower() in ("", "none"):
        return []
    return [n.strip() for n in asked.split(",") if n.strip()]


def _module(name):
    path = os.path.join(ROOT, name, "plugin.py")
    if not os.path.isfile(path):
        raise FileNotFoundError(f"no plugin folder {name!r} with a plugin.py")
    spec = importlib.util.spec_from_file_location("jam_plugin_" + name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    if getattr(module, "NAME", None) != name:
        raise ValueError(f"plugin {name}: NAME must be {name!r}")
    return module


def ship_files():
    """({path relative to the app folder: local path}, {plugin: why it is left out})
    of the plugin files the gateway node needs. Only the selected plugins whose
    every SHIP pattern matches a file are shipped (a submodule that is not
    checked out is left out, with the reason)."""
    files = {"plugins/__init__.py": os.path.join(ROOT, "__init__.py")}
    skipped = {}
    for name in selected():
        try:
            module = _module(name)
            mine = {}
            for pattern in module.SHIP:
                matches = sorted(glob.glob(os.path.join(ROOT, name, pattern)))
                if not matches:
                    raise FileNotFoundError(f"{pattern} matches no file (is the submodule checked out?"
                                            " git submodule update --init)")
                for path in matches:
                    if os.path.islink(path) or not os.path.isfile(path):
                        raise ValueError(f"refusing {path}: not a regular file")
                    mine["plugins/" + os.path.relpath(path, ROOT).replace(os.sep, "/")] = path
            files.update(mine)
        except Exception as e:
            skipped[name] = f"{type(e).__name__}: {e}"
    return files, skipped


def attach(tools, reserved_uris):
    """Load the selected plugins and add their tools to `tools` (the gateway's
    TOOLS). Returns (resources, instructions, status). A plugin is added whole
    or not at all; a tool name or resource URI that is taken is an error."""
    resources, texts = {}, []
    status = {"loaded": [], "errors": {}}
    for name in selected():
        try:
            module = _module(name)
            module.load()
            new_tools = module.tools()
            for tool, spec in new_tools.items():
                if not TOOL_NAME.fullmatch(tool) or not tool.startswith(name + "_"):
                    raise ValueError(f"tool {tool!r} must be named {name}_<lowercase>")
                if tool in tools:
                    raise ValueError(f"tool {tool!r} already exists")
                if not (isinstance(spec.get("desc"), str) and isinstance(spec.get("schema"), dict)
                        and callable(spec.get("fn"))):
                    raise ValueError(f"tool {tool!r} needs desc, schema and fn")
            new_resources = module.resources()
            for uri, res in new_resources.items():
                if not uri.startswith("ui://") or uri in reserved_uris or uri in resources:
                    raise ValueError(f"resource {uri!r} is not a free ui:// URI")
                if not (isinstance(res.get("mimeType"), str) and callable(res.get("text"))):
                    raise ValueError(f"resource {uri!r} needs mimeType and text")
        except Exception as e:
            status["errors"][name] = f"{type(e).__name__}: {e}"
            continue
        tools.update(new_tools)
        resources.update(new_resources)
        texts.append(module.INSTRUCTIONS.strip())
        status["loaded"].append(name)
    return resources, "".join("\n\n" + t for t in texts), status
