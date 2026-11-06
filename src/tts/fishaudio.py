"""Fish Audio 云端 TTS(https://api.fish.audio)适配 —— 2026-11-06 接入, 海外部署无 GPU 场景。

能力(全部 2026-11-06 实测, 见 /tmp/fish-model.json 探测记录):
- 音色: **训练持久音色**(POST /model, train_mode=fast → 秒级 trained → reference_id)。
  零样本直传(references 内联)方案实测可用但弃用 —— 按"明确单一方案、不留 fallback"原则。
- 合成: POST /v1/tts, response 直接要 `format=wav + sample_rate=24000` → 与桥的
  s16le/24kHz/mono 精确匹配(audio.decode_to_pcm 原样透传, 实测 3.7s 音频 ~3.7s 耗时)。
- 模型头: s2.1-pro(生产推荐, 付费)/s2.1-pro-free(测价 $0)。config tts.fishaudio.model 可切。

音色预设库(与 omnivoice 同目录约定): voices_dir 下子目录含 **fishaudio.txt**
(首行非空 = reference_id)即成一个 fish 音色; 可选 label.txt 显示名。

与 OmniVoiceTTS 的接口契约(bridge/engine 直接读): synth_pcm / active_base /
recent_ms / fail_streak / restart_count / slow_recent / cfg_base。
注意: 云端 TTS 不需要也不能被看门狗"重启" —— bridge._tts_health_loop 对 fish
模式只巡检记录, 不触发 _restart_omnivoice_sync。
"""
from __future__ import annotations

import asyncio
import collections
import pathlib
import time

import httpx

from . import audio

AUDIO_EXTS = {".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac"}


# ───────────────────────── 音色预设库 ─────────────────────────

def scan_voices(voices_dir: str) -> dict[str, dict]:
    """扫描 fish 音色目录 → {name: preset}。

    子目录含 fishaudio.txt: 首行=reference_id(必填), 其余行忽略;
    可选 label.txt = 页面显示名。ref 音频文件可有可无(仅人肉参考用, 请求不用它)。"""
    out: dict[str, dict] = {}
    base = pathlib.Path(voices_dir)
    if not base.is_dir():
        return out
    for entry in sorted(base.iterdir()):
        if not entry.is_dir():
            continue
        marker = entry / "fishaudio.txt"
        if not marker.is_file():
            continue
        rid = ""
        for line in marker.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                rid = line
                break
        if not rid:
            continue
        label = entry.name
        label_file = entry / "label.txt"
        if label_file.is_file():
            try:
                label = label_file.read_text(encoding="utf-8", errors="ignore").strip() or entry.name
            except Exception:                          # noqa: BLE001 标签读失败不影响音色
                label = entry.name
        out[entry.name] = {"name": entry.name, "label": label, "kind": "fish",
                           "reference_id": rid}
    return out


def create_voice_model(api_key: str, audio_text_pairs: list[tuple[str, str]],
                       title: str = "voice") -> str:
    """训练持久音色(独立工具, 一次性使用): [(wav路径或bytes, 逐字稿), ...] → model_id。

    要求: 每条 ≥10s 音频 + **逐字一致**的文本; train_mode=fast 秒级出结果。
    返回的 id 写进 voices/<名字>/fishaudio.txt 即成一档音色。"""
    part_files: list = []
    part_files += [("voices", (f"v{i}.wav", a if isinstance(a, bytes)
                               else pathlib.Path(a).read_bytes(), "audio/wav"))
                   for i, (a, _) in enumerate(audio_text_pairs)]
    part_files += [("texts", (None, t)) for _, t in audio_text_pairs]
    r = httpx.post("https://api.fish.audio/model",
                   headers={"Authorization": f"Bearer {api_key}"},
                   data={"type": "tts", "title": title, "visibility": "private",
                         "train_mode": "fast", "enhance_audio_quality": "true"},
                   files=part_files, timeout=120)
    if r.status_code != 201:
        raise RuntimeError(f"POST /model {r.status_code}: {r.text[:300]}")
    mid = r.json().get("_id") or r.json().get("id")
    # fast 训练实测秒级: 回查一次终态, 没到 trained 就等几秒
    for _ in range(10):
        m = httpx.get(f"https://api.fish.audio/model/{mid}",
                      headers={"Authorization": f"Bearer {api_key}"}, timeout=30).json()
        if m.get("state") in ("trained", "failed"):
            if m.get("state") == "failed":
                raise RuntimeError(f"音色训练失败: {r.text[:200]}")
            break
        time.sleep(4)
    return str(mid)


# ───────────────────────── 合成引擎 ─────────────────────────

class FishAudioTTS:
    def __init__(self, cfg: dict):
        c = cfg.get("fishaudio") or {}
        self.api_key = str(c.get("api_key") or "").strip()
        self.model_header = str(c.get("model") or "s2.1-pro").strip()
        self.reference_id = str(c.get("reference_id") or "").strip()
        self.latency = str(c.get("latency") or "balanced").strip()
        self.normalize = bool(c.get("normalize", True))
        self.api_base = str(c.get("api_base") or "https://api.fish.audio").rstrip("/")
        self.cfg_base = self.api_base        # bridge settings 路由会赋值该属性(对 fish 无效)
        self.timeout_sec = float(c.get("timeout_sec", 90))
        # 看门狗兼容字段(bridge 直接读; 云端模式只用于观测, 不触发重启)
        self.recent_ms: collections.deque = collections.deque(maxlen=12)
        self.restart_count = 0
        self.last_restart_at = 0.0
        self.fail_streak = 0
        self._loop_lock = asyncio.Lock()
        self._client: httpx.AsyncClient | None = None

    # ---- 合成 ----

    async def synth_pcm(self, text: str, preset: dict) -> bytes:
        """文本 → PCM s16le/24kHz/mono。异常向上抛, 由引擎决定如何收尾。"""
        if not text.strip():
            return b""
        if not self.api_key:
            raise RuntimeError("Fish Audio 未配置 api_key (config tts.fishaudio.api_key)")
        rid = str((preset or {}).get("reference_id") or self.reference_id or "")
        if rid.startswith("#") or not rid:
            raise RuntimeError("Fish Audio 音色缺 reference_id "
                               "(voices/<name>/fishaudio.txt 或 config tts.fishaudio.reference_id)")
        body = {"text": text, "reference_id": rid, "format": "wav",
                "sample_rate": 24000, "latency": self.latency,
                "normalize": self.normalize}
        async with self._loop_lock:
            t0 = time.monotonic()
            try:
                if self._client is None:
                    self._client = httpx.AsyncClient(timeout=httpx.Timeout(self.timeout_sec))
                r = await self._client.post(
                    f"{self.api_base}/v1/tts",
                    headers={"Authorization": f"Bearer {self.api_key}",
                             "model": self.model_header, "Content-Type": "application/json"},
                    json=body)
                if r.status_code != 200:
                    self.fail_streak += 1
                    raise RuntimeError(f"Fish Audio TTS HTTP {r.status_code}: {r.text[:300]}")
                self.fail_streak = 0
                wav = r.content
            except RuntimeError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.fail_streak += 1
                await self._reset()
                raise RuntimeError(f"Fish Audio 合成失败: {type(exc).__name__}: {exc}") from exc
            finally:
                self.recent_ms.append(int((time.monotonic() - t0) * 1000))
            loop = asyncio.get_running_loop()
            pcm = await loop.run_in_executor(
                None, audio.decode_to_pcm, None, wav, "audio/wav")
        return pcm

    def slow_recent(self, limit_ms: int, n: int) -> bool:
        """最近 n 次合成是否都超过 limit_ms(看门狗用, 与 OmniVoiceTTS 同语义)。"""
        if n <= 0:
            return False
        vals = list(self.recent_ms)[-n:]
        return len(vals) >= n and all(v > limit_ms for v in vals)

    # ---- 关闭 ----

    async def _reset(self):
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:  # noqa: BLE001
                pass
            self._client = None

    def active_base(self) -> str:
        return self.api_base
