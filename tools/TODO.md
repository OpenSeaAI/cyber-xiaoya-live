# TODO — 海外 Vidu 切换

> 2026-11-05 起，本部署只用海外版 Vidu（platform.vidu.com 充值 / `api.vidu.com`），
> 国内 `api.vidu.cn` 不再使用。协议与国内《详细参数》逐字一致（逐端点核对过，
> 详见 tools/vidu_overseas_probe.py 文件头）。

## 📝 历史决议（Vidu 海外迁移，2026-11-05）

- **上传通道 = 方案A（/tools/v2/files/uploads 三步式）**：桥（`src/bridge.py` 的 `upload_bytes` 路径）不用改，不引入 base64 兜底
- **prompt_operation（type=11）海外可用**：当场回 `type=12 prompt_operation_ack success=true`
  （⚠️ 海外成功回 ack，与国内"成功不回执"不同；`signaling.py` 已按 type=12 处理）
- **形象已建**：`avatar_id=1015924506738302976`（xiaoya-gen-01，已填 config）；90 天平台自动删，到期重建
- host 默认值已全部切海外：`src/vidu/client.py`、`src/engine.py`（两处）、config.json、config.example.json

## TTS —— Fish Audio 云端接入（2026-11-06 实测定案）✅

背景：本机无 NVIDIA GPU（驱动都没装），OmniVoice 本地方案不可行 → 改用云端 **Fish Audio**（已充值）。

- **决议：方案A = 训练持久音色**（`POST /model` multipart, train_mode=fast → 秒级 trained → `reference_id`）；
  零样本内联 `references` 方案（B）可用但弃用 —— "明确单一方案、不留 fallback"原则
- **实测记录**：2 对参考音频（35.6s）→ 模型 `adb94cd11e6c4b26b346d0c6a2f35065`（trained 秒出）；
  TTS `format=wav + sample_rate=24000` 直出 **s16le/24kHz/mono**，与桥要求的精确格式零转码；
  5.66s 音频合成耗时 2.35s（快于实时），`s2.1-pro` 付费模型
- **代码**：`src/tts/fishaudio.py`（`FishAudioTTS` + `scan_voices` + `create_voice_model` 工具）；
  `engine.py` 按 `tts.engine` 分派（`fishaudio` | 缺省 `omnivoice`）；
  `bridge.py` 看门狗对 fish 模式**只观测不重启**（云端无重启概念）
- **配置**：`tts.engine="fishaudio"`、`tts.voice="raiden-ei"`、`tts.fishaudio.api_key`（config.json，已 gitignore）；
  音色预设：`voices/raiden-ei/fishaudio.txt`（放 reference_id，首行）+ `label.txt`
- 试听：`/tmp/fish-clone-listen.mp3`（原始）、`/tmp/fish-bridge-e2e.wav`（桥内管线产物）
- 注意：`X-Fish-Audio-Usage` 头实测未返回（计费看平台后台）；`assets/audio` 里还有
  两条素材（`05` 无逐字稿、`07` 无音频）没进克隆——后续想加音色可补配对再建第二个模型

## LLM —— 本机 Ollama（2026-11-06 实测算案）✅

背景：无 DeepSeek key，本机跑 Ollama → 直连（OpenAI 兼容 `/v1`）。

- **配置**：`base_url=http://127.0.0.1:11434/v1`、`model=glm-5.3-flash:cloud`、
  `api_key="ollama-local"`（占位，`llm.py` 的 `api_key()` 已支持内联 key，优先级
  key_file > config 内联 > 环境变量）；fallback 用 `deepseek-v4-pro:cloud`（同为本地 ollama）
- **实测**：仓库 `LLMClient` 流式 TTFC **1.62s**、总 1.87s，回复口语化；history 写入正常；
  ollama 的思考走独立 `reasoning` 字段，仓库只读 `content` 不受影响
- ⚠️ 两个**不可用**参数（实测）：`think:false`（这个云端模型不认）、
  `reasoning_effort:"none"`（会把思考**写进正文**，有害勿用）——思考关不掉就让它关不掉，接受 ~1.5s
- usage 的 completion 计数含思考 token（统计面板数字会偏大，属正常）

## 动作库 —— 自建库建成（2026-11-06 实测）✅

- **库 `xiaoya-actions`（id=1016096575857893376）**，94 条动作，已填 `config.json action.library_id`
- **词表来源**：动作名 = `tools/dm-aliases.json` 的 88 个目标名 + `dm_router.STATIC_NAMES` 8 条
  外表类去重 —— 弹幕别名表 / T2 编译提示词 / 舞台快捷指令零适配即用（
  `bridge.py:73` 从该库拉动作名喂 dm_router，每 10 分钟刷新）
- **种子脚本**：`tools/build_action_library.py`（幂等：同名跳过；换账号/换 key 重建只需重跑；
  `--list` 可核对）。每条 description 用"怎么做+什么表情"一句话写法，服务端建库时改写优化
- **实测**：创建库+动作免费（不耗直播积分）；93 条首发 + 补 1 条重跑全成功（服务端单条 ~数秒）
- 礼物 VIP 模板（拿出"{item}"玩具贴贴）走自由文本直发，不需要专门入库

## ⏳ 待办

- [ ] （可选）T6 只验证了协议层回执，画面是否真换装需要将来开舞台页肉眼确认一次；
  动作同理（type9 动作是否按描述演，开播前手点几条快捷指令看一眼）
