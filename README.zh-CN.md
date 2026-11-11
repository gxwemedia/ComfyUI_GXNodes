# GXNodes

ComfyUI 自定义节点合集，提供 **局域网发现与共享算力**、**Topaz Video AI 本地处理**、LLM 工作流及按原文件名保存图像。

[English](README.md) · [Comfy Registry](https://registry.comfy.org/nodes/gxnodes) · [GitHub v0.1.1](https://github.com/gxwemedia/ComfyUI_GXNodes/releases/tag/v0.1.1)

| 发布版本 | Registry 包名 | ComfyUI | Python |
| --- | --- | --- | --- |
| `0.1.1` | `gxnodes` | `0.44.2+` | `3.11+` |

## 安装和启动

**ComfyUI Manager** — 搜索 **GXNodes** 或 `gxnodes`，安装后重启 ComfyUI 并刷新浏览器。Registry 当前状态可在包页面查看。

**Git 安装** — 从 ComfyUI 目录执行以下命令。请使用启动 ComfyUI 的 Python 解释器安装依赖。

```bash
git clone --branch v0.1.1 https://github.com/gxwemedia/ComfyUI_GXNodes.git custom_nodes/gxnodes
python -m pip install -r custom_nodes/gxnodes/requirements.txt
```

重启后在节点菜单搜索 **GXNodes**。保存和实用节点可直接使用默认配置。Topaz、模型 API 和 ComfyPilot 各自需要对应的本机安装或服务配置。

## 通过 JSON 配置

在 **设置 → GXNodes** 修改设置，或直接编辑同一配置文件。以下路径相对于 ComfyUI 目录。启动时指定了 `--user-directory`，请使用该目录替代 `user/`。

| 节点包内的模板 | 复制到 ComfyUI | 用途 |
| --- | --- | --- |
| [settings.json.example](settings.json.example) | `user/GXNodes/settings.json` | Topaz、共享算力、清理周期和 ComfyPilot |
| [save-folders.json.example](save-folders.json.example) | `user/GXNodes/save-folders.json` | Save Image Plus 和保存路径节点的目录预设 |

创建 `user/GXNodes/`，复制模板并去掉文件名末尾的 `.example`，以 UTF-8 格式编辑 JSON。已有配置时保留原文件，只合并需要修改的字段。手动编辑后重启 ComfyUI，让节点可用状态和设置面板同步刷新。示例中的共享令牌和程序路径默认留空。

| `settings.json` 字段 | 默认值 | 用法 |
| --- | --- | --- |
| `enable_shared_compute` | `true` | 开启共享算力侧栏和设备发现。 |
| `allow_incoming_shared` | `true` | 接收新共享任务。仅用于派发任务的机器可设为 `false`。 |
| `shared_token` | `""` | 需要访问校验时，参与共享的实例填写同一个值。`GX_SHARED_TOKEN` 环境变量优先。 |
| `transfer_retention_hours` | `24` | 传输副本保留小时数，`0` 表示长期保留。 |
| `result_retention_days` | `0` | 共享结果保留天数，`0` 表示长期保留。 |
| `topaz_ffmpeg_path` | `""` | Topaz 自带 `ffmpeg.exe` 的本机绝对路径。留空时自动查找。 |
| `enable_comfypilot` | `true` | 显示可选的企业集成节点。 |
| `comfypilot_url` | `http://127.0.0.1:39112` | 单独安装的 ComfyPilot 服务地址。 |
| `allow_remote_comfypilot` | `false` | 使用非本机 ComfyPilot 地址时开启。 |

Windows 路径在 JSON 中可使用正斜杠，或对每个反斜杠转义。常见安装路径为 `C:/Program Files/Topaz Labs LLC/Topaz Video AI/ffmpeg.exe`，以本机实际位置为准。保存目录预设在 `save-folders.json` 的 `subfolder_names` 列表中修改，每项填一个目录名。如果以前改过 `nodes/config.json`，升级前将其中的 `subfolder_names` 列表复制到新的用户配置中。

## 功能与用法

### 局域网发现与共享算力

1. 在可信局域网内，以局域网可访问的地址启动各台 ComfyUI，启用 **共享算力**。在执行机器上允许接收共享任务。
2. 允许可信局域网访问执行机器的 ComfyUI TCP 端口，以及用于发现的 UDP `8765`。参与机器应处于同一广播网络。
3. 在派发机器打开 **共享算力**侧栏，选择执行机器，点击 **共享运行**。确认目标、运行条件和待传输素材后提交。
4. 在任务列表查看进度并载入结果。首次使用可从一个简单图像工作流开始，确认对方具备所需节点和模型。

发现使用局域网 UDP 广播，发现过程不验证设备身份。工作流和文件通过 HTTP 传输。请在可信网络内核对目标地址后提交。共享令牌用于 GX 接口访问校验，ComfyUI 原生接口的网络访问权限需要单独管理。

### Topaz Video AI 本地处理

需要已授权的 **Topaz Video AI 6.2.1**、所需模型文件及支持 NVENC 的 NVIDIA 显卡。在 `topaz_ffmpeg_path` 或 **设置 → GXNodes → Topaz Video AI** 中指定 Topaz 自带的程序路径。

| 节点 | 用途 |
| --- | --- |
| `GX Topaz Video AI Frame Interpolation` | 使用 Chronos、Chronos Fast 或 Apollo 补帧，改变帧率或设置慢动作。 |
| `GX Topaz Video AI Enhancement` | 使用 Proteus 或 Rhea 放大并增强画质。 |
| `GX Topaz Video AI Interpolation + Enhancement` | 一次处理完成补帧与增强。 |

在画布上把视频加载节点的帧序列接到 `images`。API 工作流也可以填写本机 `input_path`。节点输出 `images` 和 `video path`，处理在运行 ComfyUI 的机器上进行。首次可用短视频确认模型、帧率和分辨率。Add Noise 沿用 Topaz UI 刻度，`1` 对应 `prenoise=0.01`。

### LLM 和 ComfyPilot

连接 `GX API Config → GX Generation Config → GX LLM Chat Completions`，选择兼容 Chat Completions 的服务和模型。自定义服务凭据使用 ComfyUI 进程环境变量 `GX_LLM_<NAME>_KEY`，并设置对应的 `GX_LLM_<NAME>_BASE_URL`。节点中填写环境变量名。模板和工作流都只保留公开配置，密钥存放在本机环境变量中。已有本机 DSH 环境时也可选择 DSH 模型配置。

ComfyPilot 节点连接单独安装的服务，在 **设置 → GXNodes → 企业版功能** 中配置。模型调用会将所选上下文发送给配置的服务，费用遵循该服务的规则。

### 按原文件名保存

将文件夹遍历或循环加载节点输出的原文件名和图像批次接到 `GX Save Images By Name`。文件名和图像数量需一致。节点保存到 ComfyUI output 目录，同名文件会追加数字后缀。`Save Image Plus` 支持文件名前缀和子目录。

[文件夹循环工作流](https://github.com/gxwemedia/ComfyUI_GXNodes/blob/v0.1.1/workflows/GXSaveImagesByName.json)可在 GitHub 下载，依赖 ComfyUI-Easy-Use。载入后选择自己的输入目录。

## 排查问题

| 现象 | 检查项 |
| --- | --- |
| 找不到 GXNodes | 检查 ComfyUI 和 Python 版本，用 ComfyUI 的 Python 安装依赖并查看启动日志。 |
| 修改 JSON 后没生效 | 检查实际用户目录、文件名是否已去掉 `.example`、JSON 格式及是否已重启。 |
| 找不到共享实例 | 检查局域网连通性、共享算力开关和 TCP/UDP 防火墙规则。 |
| Topaz 启动失败 | 检查程序路径、本机授权、所需模型和 NVENC 支持。 |

反馈问题时请提供版本、错误日志和最小工作流。分享前请清理密钥和私人素材。项目采用 [MIT 许可证](LICENSE)。
