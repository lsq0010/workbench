#!/usr/bin/env python3
"""平台共享能力：AI 调用、身份、工具函数。

为什么放在平台层
  AI 不该是每个功能各自配一遍 key。它应该是**平台提供的能力**：
  功能只写"要问什么"，不关心 key 存哪、走哪家、怎么流式解析。

功能怎么用
  import sys, os
  sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "lib"))
  from platform_lib import ai

  for piece in ai.stream([{"role":"user","content":"..."}]):
      print(piece, end="")

key 从哪来（按顺序找第一个能用的）
  1. 平台目录 ai.json
  2. 环境变量 DEEPSEEK_API_KEY / OPENAI_API_KEY
  3. 抓包工作台的 credentials.json（它已经配好了）
"""
import json
import os
import re
import urllib.error
import urllib.request

LIB_DIR = os.path.dirname(os.path.abspath(__file__))
PLATFORM_DIR = os.path.dirname(LIB_DIR)
PLATFORM_AI = os.path.join(PLATFORM_DIR, "ai.json")
PLATFORM_IDENTITY = os.path.join(PLATFORM_DIR, "identity.json")
REGISTRY = os.path.join(PLATFORM_DIR, "registry.json")

PROVIDERS = {
    "deepseek": {"name": "DeepSeek", "base_url": "https://api.deepseek.com/v1",
                 "models": ["deepseek-chat", "deepseek-reasoner"]},
    "openai": {"name": "OpenAI", "base_url": "https://api.openai.com/v1",
               "models": ["gpt-4o-mini"]},
    "moonshot": {"name": "月之暗面", "base_url": "https://api.moonshot.cn/v1",
                 "models": ["moonshot-v1-8k"]},
    "custom": {"name": "自定义", "base_url": "", "models": []},
}


def _read(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default if default is not None else {}


def _capture_creds():
    """从注册表里找到抓包工作台，读它的凭据（它已经配好了能用的 key）"""
    reg = _read(REGISTRY, {"features": []})
    for f in reg.get("features", []):
        if f.get("id") == "capture":
            return _read(os.path.join(f.get("path", ""), "credentials.json"), {})
    return {}


def config():
    """实际生效的 AI 配置"""
    a = _read(PLATFORM_AI, {})
    if not a.get("api_key"):
        for env in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "MOONSHOT_API_KEY"):
            if os.environ.get(env):
                a = {"provider": "deepseek" if "DEEPSEEK" in env else "openai",
                     "api_key": os.environ[env]}
                break
    if not a.get("api_key"):
        a = _capture_creds() or a
    prov = a.get("provider") or "deepseek"
    preset = PROVIDERS.get(prov, PROVIDERS["custom"])
    return {
        "provider": prov,
        "provider_name": preset["name"],
        "base_url": (a.get("base_url") or preset["base_url"] or "").rstrip("/"),
        "model": a.get("model") or (preset["models"] or [""])[0],
        "api_key": a.get("api_key") or "",
    }


def ready():
    c = config()
    return bool(c["api_key"] and c["base_url"] and c["model"])


def error_text(exc):
    """把错误翻译成能照做的话"""
    if isinstance(exc, urllib.error.HTTPError):
        code = exc.code
        if code == 401:
            return "API Key 无效 —— 重新填一个"
        if code == 402:
            return "账户余额不足 —— 去服务商控制台充值"
        if code == 404:
            return "接口地址或模型名不对 —— 检查 Base URL（通常要带 /v1）和模型名"
        if code == 429:
            return "请求太频繁，稍后再试"
        if code >= 500:
            return f"服务商暂时故障（HTTP {code}）"
        return f"请求被拒绝（HTTP {code}）"
    if isinstance(exc, urllib.error.URLError):
        return f"连不上服务商 —— 检查网络（{exc.reason}）"
    return f"{type(exc).__name__}: {exc}"


def stream(messages, temperature=0.3, timeout=120):
    """流式对话，逐块 yield 文本。没配 key 时 yield 一句人话，不抛异常。"""
    c = config()
    if not ready():
        yield "（还没有配置 AI。AI 是平台能力：在平台 or 抓包工具里填一次 key，所有功能都能用。）"
        return
    body = {"model": c["model"], "messages": messages, "stream": True,
            "temperature": temperature}
    req = urllib.request.Request(
        c["base_url"] + "/chat/completions", data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + c["api_key"],
                 "Accept": "text/event-stream"})
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except Exception as exc:
        yield "⚠️ " + error_text(exc)
        return
    try:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except Exception:
                continue
            piece = ((obj.get("choices") or [{}])[0].get("delta") or {}).get("content") or ""
            if piece:
                yield piece
    except Exception as exc:
        yield "\n\n⚠️ 读取中断：" + str(exc)[:150]
    finally:
        try:
            resp.close()
        except Exception:
            pass



# ── 看图（多模态）──────────────────────────────────────────────
#
# 哪些模型能看：实测 deepseek-flash 能（准确读出图上的数字），
# deepseek-v4-pro 不能（它看到的是 [Unsupported Image]）。
# 所以看图要单独指定模型，不能用 config 里那个通用的。
VISION_MODEL = "deepseek-flash"


def vision_model():
    """当前用哪个模型看图 —— 可以在平台设置里覆盖"""
    c = config()
    return (c.get("vision_model") or "").strip() or VISION_MODEL


def supports_vision(model=None):
    """这个模型能不能看图。名字里带 flash 的能，其他保守地说不能。"""
    m = (model or vision_model()).lower()
    return "flash" in m


def image_part(path=None, b64=None, mime="image/png"):
    """把一张图变成 API 要的格式。"""
    if b64 is None:
        if not path:
            return None
        import base64 as _b64
        with open(path, "rb") as f:
            b64 = _b64.b64encode(f.read()).decode()
        low = str(path).lower()
        if low.endswith(".jpg") or low.endswith(".jpeg"):
            mime = "image/jpeg"
        elif low.endswith(".webp"):
            mime = "image/webp"
        elif low.endswith(".gif"):
            mime = "image/gif"
    return {"type": "image_url",
            "image_url": {"url": "data:%s;base64,%s" % (mime, b64)}}


def stream_vision(question, images, system=None, model=None, max_tokens=3000,
                  temperature=0.2, timeout=180):
    """看图回答问题。

    images: 图片列表，每项是文件路径（str）或 ("b64", 数据, mime) 元组

    max_tokens 默认给 3000 —— **不能给少**：模型会先输出 reasoning_content，
    给少了思考就把额度吃光，content 会是空的（我就是这么误判"不支持图片"的）。
    """
    c = config()
    if not ready():
        yield "（还没有配置 AI。AI 是平台能力：在平台 or 抓包工具里填一次 key，所有功能都能用。）"
        return

    mdl = model or vision_model()
    if not supports_vision(mdl):
        yield ("⚠️ 当前看图模型是 %s，它不支持图片。"
               "这个平台里 `deepseek-flash` 是能看图的（实测），"
               "可以在平台「设置」里把「看图模型」改成它。" % mdl)
        return

    content = []
    for im in (images or []):
        if isinstance(im, str):
            try:
                part = image_part(path=im)
            except Exception as exc:
                yield "⚠️ 读不了图片 %s：%s" % (im, exc)
                return
        elif isinstance(im, (tuple, list)) and len(im) >= 2:
            part = image_part(b64=im[1], mime=(im[2] if len(im) > 2 else "image/png"))
        else:
            part = im
        if part:
            content.append(part)
    if not content:
        yield "⚠️ 没有可用的图片"
        return
    content.append({"type": "text", "text": question or "看看这张图。"})

    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": content})

    body = {"model": mdl, "messages": msgs, "stream": True,
            "temperature": temperature, "max_tokens": max_tokens}
    req = urllib.request.Request(
        c["base_url"] + "/chat/completions", data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + c["api_key"],
                 "Accept": "text/event-stream"})
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except Exception as exc:
        yield "⚠️ " + error_text(exc)
        return
    got_any = False
    try:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except Exception:
                continue
            delta = ((obj.get("choices") or [{}])[0].get("delta") or {})
            piece = delta.get("content") or ""
            if piece:
                got_any = True
                yield piece
    except Exception as exc:
        yield "⚠️ 读到一半断了：" + str(exc)[:200]
    if not got_any:
        yield ("（模型没给出内容。多半是 max_tokens 太小、被思考占光了 —— "
               "看图时思考也占额度，给足一些。）")

def ask(messages, temperature=0.3, timeout=120):
    """一次性拿完整回复（不流式），出错返回 (False, 错误说明)"""
    return "".join(stream(messages, temperature, timeout))


def test():
    if not ready():
        return False, "还没配置 key"
    try:
        out = ask([{"role": "user", "content": "回复两个字：可用"}])
        if out.startswith("⚠️"):
            return False, out
        return True, out.strip()[:40]
    except Exception as exc:
        return False, error_text(exc)


def identity():
    """平台级身份：所有功能的 user_id 应该是一致的"""
    d = _read(PLATFORM_IDENTITY, {})
    if d.get("user_id"):
        return d
    import uuid
    from datetime import datetime
    d = {"version": 1, "user_id": "u_" + uuid.uuid4().hex[:16],
         "kind": "anonymous-device",
         "created_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
         "note": "平台级身份。所有功能共用同一个 user_id。"}
    with open(PLATFORM_IDENTITY, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(PLATFORM_IDENTITY, 0o600)
    except Exception:
        pass
    return d


def user_id():
    return identity().get("user_id", "")


def find_keys():
    """扫本机可能存 key 的地方（给"一键填写"用）"""
    out, seen = [], set()
    sources = [
        ("平台 ai.json", PLATFORM_AI),
        ("~/.zshrc", os.path.expanduser("~/.zshrc")),
        ("~/.bash_profile", os.path.expanduser("~/.bash_profile")),
        ("DSH 配置", os.path.expanduser("~/.dsh/settings.yaml")),
    ]
    pat = re.compile(r"""(?:api[_-]?key|API[_-]?KEY)["']?\s*[:=]\s*["']?(sk-[A-Za-z0-9_\-]{16,})""")
    for label, path in sources:
        try:
            txt = open(path, encoding="utf-8", errors="replace").read()
        except Exception:
            continue
        for k in pat.findall(txt):
            if k not in seen:
                seen.add(k)
                out.append((label, k))
    for env in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
        k = os.environ.get(env)
        if k and k not in seen:
            seen.add(k)
            out.append(("环境变量 " + env, k))
    return out


if __name__ == "__main__":
    c = config()
    print("AI 配置:")
    print("  provider:", c["provider_name"], "| model:", c["model"])
    print("  base_url:", c["base_url"])
    print("  key     :", (c["api_key"][:7] + "…" + c["api_key"][-4:]) if c["api_key"] else "(未设置)")
    print("  就绪    :", ready())
    ok, msg = test()
    print("  连通性  :", "✅" if ok else "❌", msg)
    print("\n候选 key:", [(l, k[:7] + "…") for l, k in find_keys()])
    print("\n平台身份:", user_id())
