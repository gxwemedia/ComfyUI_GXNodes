import importlib

# Settings and request security must exist before any feature route is registered.
try:
    from . import gxsecurity

    gxsecurity.register_security_routes()
except Exception as _security_error:
    print("[ComfyUI_GXNodes] request security unavailable: {}".format(_security_error))
    gxsecurity = None

try:
    from . import gxsettings

    gxsettings.register_settings_routes()
    _settings = gxsettings.load_settings()
except Exception as _settings_error:
    print("[ComfyUI_GXNodes] settings unavailable, using defaults: {}".format(_settings_error))
    gxsettings = None
    _settings = {
        "enable_comfypilot": True,
        "enable_shared_compute": True,
    }

# Routes stay registered and check the feature flags per request, so toggling a
# feature in the settings panel takes effect without restarting ComfyUI.
try:
    from .gxshared import register_shared_routes

    register_shared_routes()
except Exception as _shared_error:
    print("[ComfyUI_GXNodes] shared compute disabled: {}".format(_shared_error))

node_list = [
    # Add list of .py files containing nodes here
    "gxsaveimageplus",
    "gxpadoutpaintingsize",
    "gxstringliteral",
    "gxsavepathcombiner",
    "gxsavepathappend",
    "gxsaveimagesbyname",
    "gxhighpasssharpening",
    "gxvideoreverse",
    "gxshowtextandunescapeunicode",
    "gxcomfypilot",
    "gxllm",
    "gxsavevideo",
    "gxsharedloadmedia",
    "gx_topaz_nodes",
]

ImportSuccessfulList = []
AllNodeClasses = []

try:
    from .v3_compat import GXNodesExtension
    _V3_AVAILABLE = True
except ImportError as _v3_error:
    # Keep the package importable in lightweight test/inspection environments.
    print("[ComfyUI_GXNodes] V3 API unavailable, using legacy registration: {}".format(_v3_error))
    GXNodesExtension = None
    _V3_AVAILABLE = False

NODE_CLASS_MAPPINGS = None if _V3_AVAILABLE else {}
NODE_DISPLAY_NAME_MAPPINGS = None if _V3_AVAILABLE else {}

for module_name in node_list:

    try:
        imported_module = importlib.import_module(".nodes.{}".format(module_name), __name__)
        ImportSuccessfulList.append(module_name)
        if _V3_AVAILABLE:
            if not hasattr(imported_module, "V3_NODE_CLASSES"):
                raise RuntimeError("{} 未声明 V3_NODE_CLASSES".format(module_name))
            AllNodeClasses.extend(imported_module.V3_NODE_CLASSES)
        elif hasattr(imported_module, "NODE_CLASS_MAPPINGS"):
            NODE_CLASS_MAPPINGS.update(imported_module.NODE_CLASS_MAPPINGS)
            NODE_DISPLAY_NAME_MAPPINGS.update(imported_module.NODE_DISPLAY_NAME_MAPPINGS)
        else:
            raise RuntimeError("{} 不支持当前 ComfyUI 节点注册方式".format(module_name))
        register_routes = getattr(imported_module, "register_routes", None)
        if callable(register_routes):
            register_routes()
    except Exception as e:
        print("\033[91m Failed to import {}: {}\033[0m".format(module_name, str(e)))

if _V3_AVAILABLE and gxsettings is not None:
    try:
        gxsettings.register_node_classes(AllNodeClasses)
    except Exception as _registry_error:
        print("[ComfyUI_GXNodes] node registry unavailable: {}".format(_registry_error))

_disabled_nodes = gxsettings.disabled_node_ids(_settings) if gxsettings is not None else set()
if _V3_AVAILABLE:
    V3_NODE_CLASSES = [
        node_class
        for node_class in AllNodeClasses
        if gxsettings is None or gxsettings.node_id_of(node_class) not in _disabled_nodes
    ]
    LoadedNodes = V3_NODE_CLASSES
else:
    V3_NODE_CLASSES = []
    LoadedNodes = list(NODE_CLASS_MAPPINGS)

WEB_DIRECTORY = "./js"


async def comfy_entrypoint() -> GXNodesExtension:
    """Official ComfyUI V3 extension entrypoint."""
    if not _V3_AVAILABLE:
        raise RuntimeError("ComfyUI V3 API dependencies are unavailable")
    return GXNodesExtension(V3_NODE_CLASSES)


__all__ = ["WEB_DIRECTORY", "comfy_entrypoint", "NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]


print("--------------------------------------------------------")
print("\033[36m ComfyUI_GXNodes : \033[92m  loaded {} nodes from {} modules \033[0m".format(len(LoadedNodes), len(ImportSuccessfulList)))
print("--------------------------------------------------------")
