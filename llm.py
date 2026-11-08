"""模型调用统一入口：重试、模型回退、JSON 模式、用量统计。

供应商：智谱开放平台。模型按 MODEL_CHAIN 顺序回退，第一个成功的写入 contents.model。
"""

import json
from typing import Any

import httpx

from config import MODEL_CHAIN, ZHIPU_API_KEY, ZHIPU_ENDPOINT

# 每 token 成本（元）。flash 系列当前免费，按 0 计；字段保留，后续改价只动这一个常量。
COST_PER_TOKEN = 0.0


class LLMError(Exception):
    """模型调用失败的统一异常。"""


class LLMNotConfigured(LLMError):
    """未配置 API Key。"""


class LLMJSONError(LLMError):
    """模型输出无法解析为 JSON。"""


class _Retryable(Exception):
    """可重试错误：超时、429、5xx。"""


class _Fatal(Exception):
    """不可重试错误：401/403/400，直接换下一个模型。"""


def _strip_fence(text: str) -> str:
    """去掉模型可能附带的 markdown 代码围栏。"""
    s = (text or "").strip()
    if s.startswith("```"):
        lines = s.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        s = "\n".join(lines).strip()
    return s


def _call_once(
    model: str,
    messages: list[dict],
    temperature: float,
    timeout: float,
    json_mode: bool,
) -> tuple[str, dict, str]:
    """单次请求。成功返回 (内容, usage, 实际模型名)。"""
    headers = {
        "Authorization": f"Bearer {ZHIPU_API_KEY}",
        "Content-Type": "application/json",
    }
    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}

    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(ZHIPU_ENDPOINT, headers=headers, json=body)
    except (httpx.TimeoutException, httpx.TransportError) as e:
        raise _Retryable(f"网络异常 {type(e).__name__}") from e

    if resp.status_code >= 500 or resp.status_code == 429:
        raise _Retryable(f"HTTP {resp.status_code}: {resp.text[:200]}")
    if resp.status_code != 200:
        raise _Fatal(f"HTTP {resp.status_code}: {resp.text[:300]}")

    try:
        payload = resp.json()
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, ValueError) as e:
        raise _Fatal(f"响应结构异常: {resp.text[:300]}") from e

    usage = payload.get("usage") or {}
    used_model = payload.get("model") or model
    return content, usage, used_model


def chat(
    messages: list[dict],
    temperature: float = 0.7,
    timeout: float = 45,
    json_mode: bool = False,
) -> dict:
    """统一调用入口。返回 {content, model, usage, total_tokens, cost}。

    失败策略：单个模型超时或 5xx 重试 1 次；仍失败换下一个模型；全部失败抛 LLMError。
    """
    if not ZHIPU_API_KEY:
        raise LLMNotConfigured("未配置 ZHIPU_API_KEY")

    errors: list[str] = []
    total_tokens = 0

    for model in MODEL_CHAIN:
        for attempt in (1, 2):
            try:
                content, usage, used_model = _call_once(
                    model, messages, temperature, timeout, json_mode
                )
                total_tokens += int(usage.get("total_tokens") or 0)
                return {
                    "content": content,
                    "model": used_model,
                    "usage": usage,
                    "total_tokens": total_tokens,
                    "cost": round(total_tokens * COST_PER_TOKEN, 6),
                }
            except _Retryable as e:
                errors.append(f"{model} 第{attempt}次 {e}")
                # 第一次失败后重试一次；第二次失败则跳出，换下一个模型
                continue
            except _Fatal as e:
                errors.append(f"{model} 第{attempt}次 {e}")
                break

    raise LLMError("模型调用失败：" + "；".join(errors[-3:]))


def chat_json(
    messages: list[dict],
    temperature: float = 0.7,
    timeout: float = 45,
) -> tuple[dict, dict]:
    """要求模型返回纯 JSON。解析失败重试 1 次，仍失败抛 LLMJSONError。"""
    for attempt in (1, 2):
        result = chat(messages, temperature=temperature, timeout=timeout, json_mode=True)
        text = _strip_fence(result["content"])
        try:
            return json.loads(text), result
        except json.JSONDecodeError:
            if attempt == 2:
                raise LLMJSONError("模型输出非 JSON")
    raise LLMJSONError("模型输出非 JSON")
