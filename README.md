# async-gpt-image-generate

Codex 异步生图技能。它通过 1Token 的 sub2api 异步接口提交 GPT Image 生成或编辑任务，等待完成，并把图片保存到本地。

- 默认 Base URL：`https://edge.1token-store.com`
- 默认模型：`gpt-image-2.5`
- 使用 Codex 自带的 Python 运行时
- 只使用 Python 标准库，不需要安装 Python、`requests` 或其他 pip 包

## 普通用户使用流程

把技能文件夹提供给 Codex，然后说：

> 帮我安装这个技能。

Codex 会自行完成安装和运行环境检查。普通用户不需要打开终端、输入命令或安装 Python。

首次使用只需：

1. 打开 <https://1token-store.com/keys> 申请 API Key。
2. 申请时选择 `ChatGPT/Codex` 分组。
3. 把完整密钥发送到当前 Codex 对话框。

Codex 会自动写入配置并执行无费用校验。它不会在回复中复述密钥，也不会为了校验而生成测试图片。

配置完成后，直接用自然语言提出需求即可，例如：

> 生成一张雨后的上海街道，电影感摄影，横版。

> 用这张参考图，把背景改成雪山，保持人物和构图不变。

> 查询刚才异步生图任务的状态。

## 模型选择

推荐模型列表：

- `gpt-image-2.5`：默认、推荐模型。
- `gpt-image-2`：兼容模型，默认模型失败时建议尝试。

普通用户可以直接在对话中说明模型，例如：

> 使用 gpt-image-2 生成一张雨后的上海街道。

上层工作流或开发者可以在生图和图片编辑命令中传入 `--model`：

```text
generate --prompt "图片描述" --model gpt-image-2
edit --image input.png --prompt "修改要求" --model gpt-image-2
```

`--model` 不限制为上述列表，仍可传入服务商支持的其他模型 ID。模型运行失败时，脚本会返回原始错误、当前模型、推荐模型列表和建议模型，但不会自动再次提交付费请求。用户或上层工作流可根据提示决定是否使用 `gpt-image-2` 降级重试。

## Codex 如何运行

Codex 会先通过 `load_workspace_dependencies` 获取当前版本、当前平台对应的捆绑 Python 路径，再用该运行时调用技能脚本。技能不会写死 Codex 缓存目录，也不会默认调用客户自行安装的 Python。

运行数据优先保存在：

```text
~/async-gpt-image-generate/
├── config.json
├── tasks/
├── outputs/
└── tmp/
```

如果沙箱阻止写入这个目录，Codex 会先申请该目录的文件访问权限并重试。授权后如果仍然无法写入，Codex 会询问用户是否回退到当前项目或会话目录下的 `async-gpt-image-generate/`，并等待用户确认；只有用户明确同意后才使用 `--allow-project-fallback`。不会把密钥、任务或图片静默写入当前项目。密钥仅保存在 `config.json`，任务记录和日志不会保存密钥。

生图或编辑成功后，Codex 会根据命令返回的本地路径立即展示和交付图片；除非用户明确要求分析图片或后续编辑确实需要，否则不会再调用图片理解模型检查生成结果。

## 开发者和手动安装

普通用户不需要阅读本节。

Codex 可以直接复制整个技能目录到 `$CODEX_HOME/skills/` 或 `~/.codex/skills/`。仓库中的 `scripts/install_skill.py` 是开发者备用安装器；运行它时也应使用 `load_workspace_dependencies` 返回的 Python，而不是要求客户安装 Python：

```text
RUNTIME_PYTHON scripts/install_skill.py
```

更新已有安装时添加 `--force`。安装后需要重启 Codex 或重新加载技能列表。

脚本支持以下内部命令：

- `doctor`：无费用检查环境和配置。
- `configure --from-stdin`：接收 Codex 传入的密钥并写入配置。
- `generate`：提交文本生图任务并等待结果。
- `edit`：提交参考图或遮罩编辑任务。
- `status`：查询一次任务状态。
- `wait`：恢复轮询已有任务。

失败时的 JSON 输出包含模型选择信息，例如：

```json
{
  "ok": false,
  "model": "gpt-image-2.5",
  "error": "原始错误信息",
  "recommended_models": ["gpt-image-2.5", "gpt-image-2"],
  "suggested_model": "gpt-image-2",
  "hint": "可指定其他模型重新运行。建议降级使用 gpt-image-2。"
}
```

## 仅在 Codex 捆绑运行时不可用时

通常不需要额外安装 Python。只有 `load_workspace_dependencies` 没有返回可用 Python，并且系统也没有 Python 3.9+ 时，才参考下面的备用方案。

中国大陆网络环境下，优先使用可信的国内镜像，不要执行来源不明的一键安装脚本。

### Windows

先尝试：

```powershell
winget install -e --id Python.Python.3.12
```

如果 Microsoft Store 或 `winget` 无法访问，可使用 Python 官方安装包，或使用清华大学开源软件镜像站的 [Miniconda 镜像](https://mirrors.tuna.tsinghua.edu.cn/anaconda/miniconda/)。

### macOS

已安装 Homebrew 时可运行：

```bash
brew install python
```

Homebrew 下载缓慢时，参考中国科学技术大学镜像站的 [Homebrew 镜像帮助](https://mirrors.ustc.edu.cn/help/brew.git.html)。

### Ubuntu / Debian

```bash
sudo apt update
sudo apt install python3
```

软件源访问缓慢时，参考清华大学开源软件镜像站的 [Ubuntu 镜像帮助](https://mirrors.tuna.tsinghua.edu.cn/help/ubuntu/)。

本技能本身不需要 pip。如果其他项目确实需要 Python 包，可以只为单次命令指定清华或阿里云镜像，避免永久修改客户配置：

```bash
python3 -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple 包名
python3 -m pip install -i https://mirrors.aliyun.com/pypi/simple/ 包名
```

## 安全和费用

- 配置文件会尽量设置为仅当前用户可读写。
- 密钥不会写入任务记录、命令行或错误日志。
- `--detach` 和等待超时都会保留任务 ID，可稍后恢复，避免重复提交。
- 安装、配置和 `doctor` 不调用真实生图任务；只有用户要求的 `generate` 或 `edit` 会产生生图请求。
