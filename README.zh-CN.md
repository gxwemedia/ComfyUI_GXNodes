# GXNodes

GXNodes 为 ComfyUI 提供 LLM 工作流、局域网共享算力、Topaz Video AI 处理和图像、视频保存工具。

[English](README.md)

## 安装

需要 ComfyUI 0.44.2 或更高版本、Python 3.11 或更高版本。节点使用 ComfyUI V3 API，媒体依赖由 ComfyUI 环境提供，LLM 节点额外使用 OpenAI Python SDK 和 HTTPX2。

在 ComfyUI 目录执行以下命令，安装后重启 ComfyUI 并刷新浏览器。

```bash
git clone https://github.com/gxwemedia/ComfyUI_GXNodes.git custom_nodes/ComfyUI_GXNodes
python -m pip install -r custom_nodes/ComfyUI_GXNodes/requirements.txt
```

请使用当前 ComfyUI 实例的 Python 解释器执行安装命令。

Registry 包 ID 为 `gxnodes`，上架情况以 [Registry 包页面](https://registry.comfy.org/nodes/gxnodes)为准。

## 功能

### LLM 工作流

连接 `GX API Config → GX Generation Config → GX LLM Chat Completions`，配置兼容 OpenAI Chat Completions 的服务地址和模型。按需连接图片、视频和 `GX Skill Config`，提供视觉上下文和本地技能文本。

自定义服务的密钥通过 `GX_LLM_<NAME>_KEY` 环境变量引用，`GX_LLM_<NAME>_BASE_URL` 需要与节点中的服务地址一致。DSH 集成读取已有的 `.comfypilot/dsh` 配置。提示词及选中的上下文会发送给所配置的服务，费用遵循该服务的规则。

### 局域网共享算力

在共享算力侧栏选择实例，核对运行条件和待传输素材后提交工作流。任务完成后，可从任务列表加载结果。

请在可信网络内使用。设备发现不验证身份，局域网 HTTP 传输采用明文。可选共享令牌用于 GX 接口访问校验，ComfyUI 原生接口需要单独配置网络访问控制。参与共享的实例在设置中使用相同令牌。

### Topaz Video AI

三个节点分别提供补帧、增强和补帧加增强，接收图像或本地视频路径，输出图像和视频路径。

需要已授权的 **Topaz Video AI 6.2.1**、已下载模型及支持 NVENC 的 NVIDIA 显卡。在设置 → GXNodes → Topaz Video AI 中配置 Topaz 自带的 FFmpeg。程序路径、编码和设备配置由设置与后台默认值管理。

增强支持 Proteus、Rhea，补帧支持 Chronos、Chronos Fast、Apollo。Add Noise 按 Topaz UI 的刻度填写，值为 1 时对应 `prenoise=0.01`。可用模型取决于本机安装。

### 保存和实用节点

`GX Save Images By Name` 按每张图像对应一个文件名保存批次。连接文件夹遍历或循环加载节点提供的原文件名，即可沿用来源命名。`Save Image Plus` 支持文件名前缀和子目录，输出保存在 ComfyUI output 目录内。

[文件夹循环示例](workflows/GXSaveImagesByName.json)依赖 ComfyUI-Easy-Use 的 `easy loadImagesForLoop`。载入后选择自己的输入文件夹。

其他节点提供视频保存、图像尺寸调整、锐化、视频倒放和文本展示。兼容节点继续支持已保存工作流。

### ComfyPilot 集成

可选的 ComfyPilot 节点需要单独安装本机服务。在设置 → GXNodes 的企业版功能中配置服务地址和节点显示。连接远程服务时需启用远程 URL。

## 设置与语言

设置 → GXNodes 提供企业集成、共享算力和 Topaz 配置，设置存放在 ComfyUI 的 `user/GXNodes/settings.json`。节点定义、悬浮帮助及自定义界面提供中英文。

## 问题反馈

提交 Issue 时请提供 ComfyUI 版本、GXNodes 版本、报错日志和最小工作流。分享前请移除密钥和私人素材。

## 许可证

MIT，详见 [LICENSE](LICENSE)。
