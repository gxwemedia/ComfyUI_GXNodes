# GXNodes

Custom nodes for ComfyUI, with LLM workflows, LAN shared compute, Topaz Video AI processing, and image/video saving utilities.

[中文说明](README.zh-CN.md)

## Installation

Requires ComfyUI 0.44.2 or later and Python 3.11 or later. GXNodes uses the ComfyUI V3 node API. ComfyUI supplies media dependencies, and the LLM nodes additionally use the OpenAI Python SDK and HTTPX2.

From your ComfyUI directory, run the following command, then restart ComfyUI and refresh the browser.

```bash
git clone https://github.com/gxwemedia/ComfyUI_GXNodes.git custom_nodes/ComfyUI_GXNodes
python -m pip install -r custom_nodes/ComfyUI_GXNodes/requirements.txt
```

Run the installation command with the Python interpreter used by your ComfyUI instance.

Registry package ID is `gxnodes`. Registry availability is shown on the [package page](https://registry.comfy.org/nodes/gxnodes).

## Features

### LLM workflows

Connect `GX API Config` to `GX Generation Config`, then connect its request configuration to `GX LLM Chat Completions`. Configure an OpenAI Chat Completions-compatible service and model. Optional image/video inputs and `GX Skill Config` supply visual context and local skill text.

API credentials are referenced through environment variables. For custom providers, use `GX_LLM_<NAME>_KEY` and set `GX_LLM_<NAME>_BASE_URL` to the same base URL as the node. DSH integration reads an existing local `.comfypilot/dsh` configuration. Requests send prompts and selected context to the configured provider, which may charge for usage.

### LAN shared compute

Open the Shared Compute sidebar, select an instance, review its requirements and the assets to transfer, then submit the workflow. Completed results can be loaded from the task list.

Use trusted networks. Discovery does not authenticate devices and LAN HTTP traffic is unencrypted. The optional shared token protects GX endpoints. ComfyUI's native endpoints require their own network access controls. Configure the same token on participating instances through Settings → GXNodes.

### Topaz Video AI

Three nodes provide frame interpolation, enhancement, and combined interpolation/enhancement. Each accepts images or a local video path and outputs images and a video path.

Requires a licensed local installation of **Topaz Video AI 6.2.1**, downloaded models, and an NVIDIA GPU with NVENC support. Configure the Topaz FFmpeg executable under Settings → GXNodes → Topaz Video AI. The executable and encoding/device configuration are managed through settings and backend defaults.

Enhancement includes Proteus and Rhea. Interpolation includes Chronos, Chronos Fast, and Apollo. Add Noise uses the Topaz UI scale, with a value of 1 mapping to `prenoise=0.01`. Model availability depends on the local installation.

### Saving and utility nodes

`GX Save Images By Name` saves image batches with one filename per image. Connect filenames from your folder/loop loader to preserve the source naming. `Save Image Plus` supports filename prefixes and subfolders. Output is saved within the ComfyUI output directory.

The [folder-loop example](workflows/GXSaveImagesByName.json) uses `easy loadImagesForLoop` from ComfyUI-Easy-Use. Choose your own input folder after loading the example.

Additional nodes support video saving, image sizing, sharpening, video reversal, and text display. Compatibility nodes remain available for saved workflows.

### ComfyPilot integration

Optional ComfyPilot nodes connect to a separately installed local service. Configure its service URL and node visibility under the enterprise settings in Settings → GXNodes. A remote service requires enabling remote URLs.

## Settings and language

Settings → GXNodes contains enterprise integration, shared compute, and Topaz configuration. Settings are stored in ComfyUI's `user/GXNodes/settings.json`. Node definitions, tooltips, and custom interfaces include English and Chinese localizations.

## Support

Report issues with your ComfyUI version, GXNodes version, error log, and a minimal workflow. Remove credentials and private media before sharing.

## License

MIT. See [LICENSE](LICENSE).
