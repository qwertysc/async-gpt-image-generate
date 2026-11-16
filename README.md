# async-gpt-image-generate

通过 TokenToken 的 sub2api 异步接口生成或编辑图片，支持 Codex、DeepSeek Harness（dsh）和 Claude Code。任务记录与图片保存在当前会话的工作目录，Home 下只保存配置。

- 默认 Base URL：`https://edge.1token-store.com`
- 默认模型：`gpt-image-2.6`
- Python 3.9+，仅使用标准库，无需 pip 依赖
- Codex 和支持的 DeepSeek Harness 桌面端可直接使用自带运行时；其他宿主检测已有 Python

## 普通用户使用流程

把技能文件夹提供给当前 Agent，然后说：

> 帮我安装这个技能。

Agent 会选择自己的技能目录并检查运行环境。已配置过的密钥可以继续使用；同一系统用户下的这些 Agent 默认共享本技能的配置文件。

首次配置只需：

1. 打开 <https://1token-store.com/keys> 申请 API Key。
2. 申请时选择 **`ChatGPT/Codex` 分组**。使用 dsh 或 Claude Code 时也选这个服务商分组。
3. 把完整密钥发送到当前 Agent 对话框。

Agent 会自动保存配置并执行无费用检查，不会在回复中复述密钥，也不会为了检查而生成测试图片。首次保存或修改 Home 配置时，宿主可能要求一次文件写入授权；日常生图只读取配置，产物直接写入当前会话目录。

配置完成后，直接提出需求：

> 生成一张雨后的上海街道，电影感摄影，横版。

> 用这张参考图，把背景改成雪山，保持人物和构图不变。

> 使用 gpt-image-2.6-flare 生成一张海边日落。

> 继续等待刚才的异步生图任务。

图片编辑通过宿主正常的审批流程提交。如果审批因为上传照片到 TokenToken 而拒绝，Agent 会说明原因并询问是否同意上传这张照片；同意后重试原定编辑，未同意时停止。生成成功后直接展示和交付图片，除非用户要求分析或后续编辑需要，否则不额外调用图片理解模型。

## 文件保存位置

```text
~/async-gpt-image-generate/
└── config.json                    # API Key、接口地址、用户配置

<当前会话工作目录>/async-gpt-image-generate/
├── tasks/                         # 任务记录，包含恢复所需信息
├── outputs/                       # 生成或编辑后的图片
└── tmp/                           # 临时工作目录
```

下载图片采用原子写入，临时文件位于 `outputs/` 中并在完成后清理；不会借此向 Home 写入运行文件。只有显式执行 `configure` 才写配置。`doctor` 只读取配置并报告路径，不创建目录、不试写，也不调用 API；生图提交前才检查运行目录是否可写。

Agent 会从当前会话上下文取得工作目录，并明确传入运行目录，防止执行脚本前切换目录导致文件落错位置。同一个任务后续使用原来的目录恢复；多个聊天如果使用同一个工作目录，就共享其产物目录。

如果宿主要求中间文件放在 `work/`，运行目录改为 `<当前会话>/work/async-gpt-image-generate/`；宿主要求交付文件放在独立 `outputs/` 时，完成后复制到该目录交付。Python 调用统一使用 `-B`，包括诊断脚本，避免在技能安装目录产生 `__pycache__`。

运行目录不能位于配置目录或技能代码目录内。如果当前项目就是本技能的源码，使用会话工作区内另一个产物位置。配置路径和运行路径都在脚本返回值中明确显示，密钥不会写入任务记录。

## 模型选择

可用模型列表：

| 模型 | 默认行为 |
|---|---|
| `gpt-image-2.6` | 默认模型 |
| `gpt-image-2.6-flare` | 按用户指定使用 |
| `gpt-image-2.6-sunburst` | 按用户指定使用 |
| `gpt-image-2.5` | 按用户指定使用 |
| `gpt-image-2.5-flare` | 按用户指定使用 |
| `gpt-image-2.5-sunburst` | 按用户指定使用 |
| `gpt-image-2` | 按用户指定使用，也可在失败后选择尝试 |

模型优先级是：**本次请求的 `--model` → 配置中固定的模型 → 内置默认 `gpt-image-2.6`**。模型后缀不代表本技能对速度、质量或价格作出承诺。

新配置默认跟随内置模型，不会自动把默认模型固定写入 `config.json`。旧配置中已有的 `model` 会保留，包括旧版自动保存的 `gpt-image-2.5`，因为无法判断其是否为用户主动选择。

如果希望旧配置也跟随当前默认模型，可以直接对 Agent 说：

> 把这个技能的模型设置改为跟随默认模型，保留现有密钥和其他配置。

`--model` 仍接受列表之外、服务商支持的模型 ID。发生错误时会返回原始原因；脚本不会自动换模型重新提交付费请求。已有任务的轮询、下载或落盘失败应通过 `wait` 恢复，避免重复提交。

## 安装与运行环境

安装说明和宿主差异见 [references/agent-hosts.md](references/agent-hosts.md)。普通用户无需阅读下面的命令；由 Agent 执行即可。

| 宿主 | 用户级目录 | 项目级目录 |
|---|---|---|
| Codex | 新安装优先 `~/.agents/skills/`；已有 `~/.codex/skills/` 安装原位更新 | `<项目>/.agents/skills/` |
| DeepSeek Harness | `~/.dsh/skills/`；也支持共享的 `~/.agents/skills/` | `<项目>/.dsh/skills/` |
| Claude Code 本地会话 | `~/.claude/skills/` | `<项目>/.claude/skills/` |

共用标准 `SKILL.md` 和 Python 脚本。`agents/openai.yaml` 是 Codex 的可选界面元数据，运行核心不依赖它。Claude Code 的本地 Code 会话与 Cowork、云端会话有不同的安装及文件环境，不能用本机安装路径替代云端安装。

安装器的调用方式如下，`RUNTIME_PYTHON` 表示已确认可用的解释器，脚本使用绝对路径：

```text
RUNTIME_PYTHON -B <技能目录>/scripts/install_skill.py --agent codex
RUNTIME_PYTHON -B <技能目录>/scripts/install_skill.py --agent dsh
RUNTIME_PYTHON -B <技能目录>/scripts/install_skill.py --agent claude
RUNTIME_PYTHON -B <技能目录>/scripts/install_skill.py --agent claude --scope project --project-dir <项目根目录>
RUNTIME_PYTHON -B <技能目录>/scripts/install_skill.py --agent generic --skills-root <宿主支持的技能父目录>
```

更新已有安装加 `--force`。`--skills-root` 优先于自动选择的目录。安装器会识别现有 Codex/dsh 安装；发现多个同名安装时要求明确目标，避免更新错位置。保留已有的无关文件，不复制仓库元数据、测试或用户密钥。安装后刷新宿主技能列表；未显示时再重启。

## 开发者命令与路径优先级

所有命令使用同一个已确认的 Python 3.9+。路径含中文或空格时，按当前 shell 规则引用整个参数。配置和运行目录独立解析：

| 用途 | 从高到低的优先级 |
|---|---|
| 配置 | `--config-dir` → `ASYNC_GPT_IMAGE_CONFIG_DIR` → 旧 `ASYNC_GPT_IMAGE_HOME` → `~/async-gpt-image-generate` |
| 任务和图片 | `--data-dir` → `ASYNC_GPT_IMAGE_DATA_DIR` → 当前工作目录下的 `async-gpt-image-generate` |

`--data-dir` 是完整产物目录，不会在指定路径后再自动添加目录名。Agent 应显式传入当前会话下的绝对路径。配置根目录以执行环境的当前用户 Home 为准；远程、WSL、容器需要使用各自环境的路径。

内部命令（前面加解释器、`-B` 和主脚本绝对路径）：

```text
configure --from-stdin --json
configure --keep-key --use-default-model --json
configure --keep-key --model gpt-image-2.6-flare --json
doctor --data-dir <绝对产物目录> --json
generate --prompt <描述> --detach --data-dir <绝对产物目录> --json
edit --image <参考图片> --prompt <修改要求> --detach --data-dir <绝对产物目录> --json
status <task_id> --data-dir <原任务目录> --json
wait <task_id> --yield-after 30 --data-dir <原任务目录> --json
```

`configure --from-stdin` 通过非 TTY 标准输入接收一行密钥。`--keep-key` 使用已经保存的密钥更新其他配置。保留对既有 `ASYNC_GPT_IMAGE_API_KEY` 和手动 `--from-env` 的兼容支持，但 Agent 的对话配置流程采用标准输入，不把新密钥写进命令或环境变量。

成功结果以及已知任务 ID 的超时、落盘错误会返回 `task_id`、`data_dir`、`task_file`、`config_path`、`resume_args`。`resume_args` 是传给同一个主脚本的参数数组，包含原目录；切换工作目录后也可据此恢复。保存失败时 `task_file` 可能尚不存在。收到远端任务 ID 后，即使本地保存失败，也会把 ID 返回给调用方，不自动重复提交。

## 进度与下载恢复

Agent 推荐先用 `--detach` 提交，再用 `wait --yield-after 30` 短时等待。返回 `ok: true`、`completed: false`、`yielded: true` 表示本次时间片结束，可以按 `resume_args` 继续等待。整体等待预算保留，默认 900 秒；不要在每次继续时重复传 `--max-wait`，该参数会主动开启新预算。达到真正的等待上限或下载重试上限后停止自动循环，保留原任务供后续恢复。

进度在 stderr 输出，stdout 仍为一份 JSON。脚本会报告“生成中、下载中、下载失败、完成”等阶段；Agent 应在长时间等待时约每 30～60 秒给用户一次简短反馈。网络读取受超时约束，短时等待可能延长到当前网络操作结束。直接命令行使用者仍可省略 `--detach` / `--yield-after` 进行前台等待；`--quiet-progress` 可关闭进度输出。

远端完成后先保存结果信息，再下载；下载失败不会重新生图，多图任务逐张保存，恢复时跳过已完成的图片。默认每张图片最多尝试 3 次，同一路径最多 2 次，整个下载恢复阶段预算 120 秒；签名链接疑似过期时最多查询一次原任务刷新地址。图片下载错误明确标记为 `error_stage: download`，与生成失败区分。

| 图片下载策略 | 行为 |
|---|---|
| `auto`（默认） | 沿用宿主网络；通过代理发生传输故障时，在次数限制内尝试直连 |
| `system` | 始终沿用宿主网络，适合明确要求使用代理的环境 |
| `direct` | 图片下载直接连接 |

可在本次命令中传 `--download-network system`，该选择随任务和恢复参数保留；长期设置可用 `configure --keep-key --download-network system`。模式只影响结果图片下载，不改变 API 提交、参考图上传、任务查询或系统代理；保留 TLS 校验和宿主权限控制。当前会话或宿主明确要求必须走代理时，Agent 应选择 `system`。

配置字段、进度事件及恢复协议见 [references/api-contract.md](references/api-contract.md)。正常执行不需要修改配置文件。

## 旧版升级

- 旧 Home 下的 `config.json` 继续使用，不自动重写密钥、接口地址或固定模型。
- 旧 `ASYNC_GPT_IMAGE_HOME` 只作为配置目录别名；运行文件改由新规则定位，并返回兼容提示。
- `configure --data-dir` 会给出明确错误，改用 `configure --config-dir` 指定配置位置。旧的自定义目录或项目回退配置可以通过 `--config-dir` 继续读取，不会静默搬动密钥。
- `--allow-project-fallback` 暂时兼容接受，但已无作用；新调用不再需要它。
- `status` / `wait` 优先读取当前运行目录的任务，未找到时可以只读加载配置目录下的旧 `tasks/`。后续状态和新下载写入会话目录。
- 旧图片和任务文件不会自动搬动或删除；已有完整图片可从原路径交付。没有任何本地记录时，可用已知任务 ID 和匹配的服务商配置恢复。

## 没有可用 Python 时

先检查宿主提供的运行时，再检查系统已有 Python 3.9+。Codex / DeepSeek Harness 的工具不可用时也可以使用已有系统 Python；不能要求其他 Agent 调用不存在的 Codex 工具。

确实没有兼容解释器时，由 Agent 根据操作系统引导一次性配置：Windows/macOS 可使用 [Python 官方下载](https://www.python.org/downloads/)；Linux 可使用系统软件源安装 Python 3。不要执行来源不明的一键脚本。本技能不需要 `requests` 或任何 pip 包。

## 验证

在仓库根目录使用选定解释器执行：

```text
RUNTIME_PYTHON -B -m unittest discover -s tests -v
```

测试使用临时目录、测试密钥和本地模拟 API，覆盖配置只读、会话隔离、下载恢复、代理策略、多图部分成功、短时等待预算、进度输出、付费请求前的写入检查、模型选择及各宿主安装路径。macOS 有 `sandbox-exec` 时，还验证只允许写入会话运行目录的真实沙箱场景；不需要真实生图请求。
