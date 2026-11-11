# GXNodes

ComfyUI custom nodes for **LAN discovery and shared compute**, **local Topaz Video AI**, LLM workflows, and named media exports.

[中文指南](README.zh-CN.md) · [Comfy Registry](https://registry.comfy.org/nodes/gxnodes) · [GitHub v0.1.2](https://github.com/gxwemedia/ComfyUI_GXNodes/releases/tag/v0.1.2)

| Release | Registry package | ComfyUI | Python |
| --- | --- | --- | --- |
| `0.1.2` | `gxnodes` | `0.44.2+` | `3.11+` |

## Install and start

**ComfyUI Manager** — Search for **GXNodes** or `gxnodes`, install it, then restart ComfyUI and refresh the browser. The package page shows the current Registry status.

**Git** — Run these commands from the ComfyUI directory. Use the Python interpreter that launches your ComfyUI instance to install the dependencies.

```bash
git clone --branch v0.1.2 https://github.com/gxwemedia/ComfyUI_GXNodes.git custom_nodes/gxnodes
python -m pip install -r custom_nodes/gxnodes/requirements.txt
```

Restart ComfyUI. Search the node menu for **GXNodes**. The save and utility nodes work with the built-in defaults. Topaz, model API calls, and ComfyPilot each use their own local installation or service configuration.

## Configure with JSON

Use **Settings → GXNodes** or edit the same settings file directly. The directory below is relative to the ComfyUI installation. If you pass `--user-directory`, use that directory in place of `user/`.

| Template in the node package | Destination in ComfyUI | Purpose |
| --- | --- | --- |
| [settings.json.example](settings.json.example) | `user/GXNodes/settings.json` | Topaz, shared compute, retention, and ComfyPilot |
| [save-folders.json.example](save-folders.json.example) | `user/GXNodes/save-folders.json` | Folder presets for Save Image Plus and the Save Path nodes |

Create `user/GXNodes/`, copy each template to the destination name without `.example`, and edit the UTF-8 JSON. Keep existing files and merge only the fields you want to change. Restart ComfyUI after hand-editing configuration so node availability and the settings panel reload together. The templates ship with an empty shared token and an empty executable override.

| Key in `settings.json` | Default | How to use it |
| --- | --- | --- |
| `enable_shared_compute` | `true` | Enables the shared-compute sidebar and discovery. |
| `allow_incoming_shared` | `true` | Allows new shared tasks. Set `false` on a machine that should only submit tasks. |
| `shared_token` | `""` | Set the same value on participating instances when access validation is needed. `GX_SHARED_TOKEN` takes precedence. |
| `transfer_retention_hours` | `24` | Hours to retain transferred copies. `0` keeps them indefinitely. |
| `result_retention_days` | `0` | Days to retain shared results. `0` keeps them indefinitely. |
| `topaz_ffmpeg_path` | `""` | Absolute local path to Topaz `ffmpeg.exe`. Empty uses detection. |
| `enable_comfypilot` | `true` | Shows the optional enterprise integration nodes. |
| `comfypilot_url` | `http://127.0.0.1:39112` | Address of a separately installed ComfyPilot service. |
| `allow_remote_comfypilot` | `false` | Enable only when you intend to use a non-loopback service address. |

For Windows paths in JSON, use forward slashes or escape each backslash. For example, a typical value is `C:/Program Files/Topaz Labs LLC/Topaz Video AI/ffmpeg.exe`. Use the actual location on your machine. To customize save presets, edit the `subfolder_names` list in `save-folders.json`, using one folder name per entry. If you previously customized `nodes/config.json`, copy its `subfolder_names` list to the new user configuration before upgrading.

## Use the nodes

### LAN discovery and shared compute

1. On each trusted LAN machine, run ComfyUI on a LAN-reachable address and enable **Shared Compute**. On the worker, allow incoming shared tasks.
2. Allow the worker's ComfyUI TCP port and UDP `8765` for discovery on the trusted LAN. Use instances on the same broadcast network.
3. On the submitting machine, open the **Shared Compute** sidebar, select the worker, and choose **Shared Run**. Review the target, requirements and transferable assets before confirming.
4. Open the task list to monitor the run and load completed outputs. Start with a simple image workflow to confirm the worker has the required nodes and models.

Discovery uses local UDP broadcast and does not authenticate devices. Workflow and file transfers use HTTP. Use a trusted network and check the target address before submitting. The shared token protects GX endpoints. Manage access to native ComfyUI endpoints separately.

### Local Topaz Video AI

Use a licensed **Topaz Video AI 6.2.1** installation, the required model files, and an NVIDIA GPU with NVENC support. Set `topaz_ffmpeg_path` or configure the executable in **Settings → GXNodes → Topaz Video AI**. The path must point to Topaz's bundled executable.

| Node | Use |
| --- | --- |
| `GX Topaz Video AI Frame Interpolation` | Change frame rate or apply slow motion with Chronos, Chronos Fast, or Apollo. |
| `GX Topaz Video AI Enhancement` | Resize and enhance with Proteus or Rhea. |
| `GX Topaz Video AI Interpolation + Enhancement` | Apply interpolation and enhancement in one pass. |

On the canvas, connect an image sequence from a video loader to `images`. API workflows may supply a local `input_path`. The outputs are `images` and `video path`. Processing runs on the machine hosting ComfyUI. Start with a short clip to confirm model availability, output frame rate and dimensions. Add Noise follows the Topaz UI scale, with `1` corresponding to `prenoise=0.01`.

For a source audio track in the output MP4, use `input_path` and the default audio copy setting. An `images` input contains only frames. To match Topaz Video AI's duplicate-frame sensitivity of `10`, set **Duplicate frame sensitivity** to `0.01`. With Apollo, the output video track can end earlier than the source, especially with very short clips or higher duplicate-frame sensitivity.

### LLM and ComfyPilot

Connect `GX API Config → GX Generation Config → GX LLM Chat Completions`. Select a Chat Completions-compatible endpoint and model. For custom authentication, set `GX_LLM_<NAME>_KEY` and the matching `GX_LLM_<NAME>_BASE_URL` in the ComfyUI process environment. Select the key's environment variable name in the node. Keep model keys out of JSON examples and workflows. DSH profiles can be used when an existing local DSH installation is available.

ComfyPilot nodes use the separately installed service configured under **Settings → GXNodes → Enterprise**. Model requests send the selected context to the configured provider and may incur provider charges.

### Named saves

Connect a folder/loop loader's original filenames and image batch to `GX Save Images By Name`. The counts must match. The node saves into the ComfyUI output directory and uses numeric suffixes to preserve existing files. `Save Image Plus` supports prefixes and subfolders.

The [folder-loop workflow](https://github.com/gxwemedia/ComfyUI_GXNodes/blob/v0.1.2/workflows/GXSaveImagesByName.json) is available from GitHub. It uses ComfyUI-Easy-Use. Choose your own input directory after loading it.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| GXNodes does not appear | Check ComfyUI and Python versions, install dependencies with ComfyUI's Python, and check the startup log. |
| A JSON change seems ignored | Confirm the effective ComfyUI user directory, filename without `.example`, JSON syntax, and a restart. |
| A worker is missing | Verify LAN reachability, the Shared Compute toggle, and the TCP/UDP firewall rules. |
| Topaz cannot start | Check the bundled executable path, local license, installed model, and NVENC support. |

Report issues with the versions, error log, and a minimal workflow. Remove credentials and private inputs before sharing. See [MIT license](LICENSE).
