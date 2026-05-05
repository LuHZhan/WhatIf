import json
import os
from functools import lru_cache
from typing import Type, TypeVar, Iterator

# [json_repair] 修复 LLM 输出的残缺 JSON（截断、缺括号等）
import json_repair

# [litellm] 统一 LLM 调用层，屏蔽各家 provider 的 SDK 差异
import litellm
from litellm import completion, get_supported_openai_params, supports_response_schema

# [pydantic] 运行时数据校验，BaseModel 是所有结构化输出的基类
from pydantic import BaseModel, ValidationError

# [dotenv] 从 .env 文件读取 API Key 等环境变量到 os.environ
from dotenv import load_dotenv

import config

# 将 backend/.env 中的变量注入到当前进程的环境变量中
load_dotenv()

# [litellm] 关闭"自动丢弃不支持参数"行为
# False = 遇到不支持的参数直接报错，而非静默丢弃（便于调试）
litellm.drop_params = False

# 延迟导入游戏日志，Preprocessing 阶段不启动 Runtime，此时 glog 为 None
try:
    from runtime.game_logger import glog as _glog
except Exception:
    _glog = None

# [pydantic] TypeVar 约束：T 必须是 BaseModel 的子类
# 等价于 C++ template<typename T> requires std::derived_from<T, BaseModel>
T = TypeVar("T", bound=BaseModel)


# [functools.lru_cache] 对 (model, custom_provider) 组合做 memoization
# 每个模型只查询一次"是否支持 native JSON schema"，结果缓存在 heap 上
# maxsize=64 表示最多缓存 64 个不同的模型字符串组合
@lru_cache(maxsize=64)
def _check_native_schema(model: str, custom_provider: str | None) -> bool:
    try:
        # [litellm] 获取该模型支持的 OpenAI 兼容参数列表
        supported = (
            get_supported_openai_params(
                model=model, custom_llm_provider=custom_provider
            )
            or []
        )
        if "response_format" not in supported:
            return False
        # [litellm] 进一步检查是否支持 structured output（JSON schema 约束）
        return bool(
            supports_response_schema(model=model, custom_llm_provider=custom_provider)
        )
    except Exception:
        return False


# 部分模型有输出 token 上限，超出会被截断，在此做硬性保护
_MODEL_MAX_OUTPUT: dict[str, int] = {
    "dashscope/qwen-max-latest": 8192,
    "dashscope/qwen-max": 8192,
}


class LLMClient:
    def __init__(self):
        pass

    @staticmethod
    def _budget_to_effort(thinking_budget: int | None) -> str | None:
        """将数值型 thinking_budget 映射为 OpenAI reasoning_effort 字符串枚举"""
        if thinking_budget is None or thinking_budget == 0:
            return None
        if thinking_budget == -1:
            return "medium"
        if thinking_budget <= 256:
            return "low"
        if thinking_budget <= 2048:
            return "medium"
        return "high"

    def _build_reasoning_params(
        self,
        model: str,
        thinking_budget: int | None,
        extra_params: dict | None,
    ) -> dict:
        """
        各家 provider 的 thinking/reasoning 参数格式完全不同，此方法统一抹平差异。
        等价于 C++ 虚函数表的运行时 dispatch，但用 model 字符串前缀做路由。

        - extra_params 不为空时直接透传，跳过自动推断（调用方完全自控）
        - dashscope：通过 extra_body.enable_thinking + thinking_budget 控制
        - anthropic：通过 thinking.budget_tokens 控制
        - 其他（OpenAI / DeepSeek）：通过 reasoning_effort 字符串控制
        """
        if extra_params:
            return extra_params

        # 从 "dashscope/qwen-max" 中提取 provider 前缀 "dashscope"
        prefix = model.split("/", 1)[0] if "/" in model else ""

        if prefix == "dashscope":
            if thinking_budget and thinking_budget != 0:
                params: dict = {"enable_thinking": True}
                if thinking_budget > 0:
                    params["thinking_budget"] = thinking_budget
                return {"extra_body": params}
            return {"extra_body": {"enable_thinking": False}}

        if thinking_budget is None or thinking_budget == 0:
            return {}

        if prefix == "anthropic":
            budget = max(1024, thinking_budget if thinking_budget > 0 else 2048)
            return {"thinking": {"type": "enabled", "budget_tokens": budget}}

        # OpenAI / DeepSeek 等其他 provider 使用 reasoning_effort 字段
        return {"reasoning_effort": self._budget_to_effort(thinking_budget)}

    def _needs_prompt_only_json(self, model: str) -> bool:
        """检查该模型是否不支持 native structured output，需要改用 prompt 指令方式"""
        cp = model.split("/", 1)[0] if "/" in model else None
        # [lru_cache] _check_native_schema 结果已缓存，此处为 O(1) 查表
        return not _check_native_schema(model, cp)

    def _load_json_system_template(self) -> str:
        """从磁盘加载 JSON 输出的 system prompt 模板文件"""
        template_path = config.CORE_PROMPTS_DIR / "json_output_system.txt"
        return template_path.read_text(encoding="utf-8")

    def _build_json_system_prompt(self, response_model: Type[T]) -> str:
        """
        将 Pydantic 模型的 JSON Schema 注入 system prompt。
        用于不支持 native structured output 的模型（如部分 DashScope 版本）。
        [pydantic] model_json_schema() 自动从类型注解生成标准 JSON Schema
        """
        template = self._load_json_system_template()
        schema = response_model.model_json_schema()
        schema_str = json.dumps(schema, ensure_ascii=False, indent=2)
        return template.replace("{schema_str}", schema_str)

    def _clean_json_response(self, content: str) -> str:
        """
        去除 LLM 在 JSON 前后包裹的 markdown 代码块标记。
        部分模型即使被要求输出纯 JSON，仍会返回 ```json ... ``` 格式。
        """
        content = content.strip()

        if content.startswith("```json"):
            content = content[7:]
        elif content.startswith("```"):
            content = content[3:]

        if content.endswith("```"):
            content = content[:-3]

        return content.strip()

    def _parse_or_repair(self, content: str, response_model: type[T]) -> T:
        """
        两阶段 JSON 解析：先正常 parse，失败后用 json_repair 自动修复再 parse。
        LLM 偶尔输出截断 JSON（token 超限）或格式错误时触发修复路径。

        [pydantic] model_validate_json() — 从 JSON 字符串解析并按字段类型校验
        [pydantic] ValidationError — 字段类型不匹配或缺少必填字段时抛出
        [json_repair] repair_json() — 补全缺失的括号/引号，修复常见 JSON 残缺
        """
        try:
            return response_model.model_validate_json(content)
        except ValidationError:
            if _glog:
                _glog.log(
                    "LLM_JSON_REPAIR",
                    {
                        "model": response_model.__name__,
                        "content_preview": content[:200],
                    },
                )
            repaired = json_repair.repair_json(content)
            return response_model.model_validate_json(repaired)

    def generate(
        self,
        prompt: str,
        model: str = "dashscope/qwen3.5-flash",
        temperature: float = 0.3,
        thinking_budget: int | None = None,
        extra_params: dict | None = None,
        api_base: str | None = None,
        api_key_env: str | None = None,
        log: bool = True,
        caller: str | None = None,
    ) -> str:
        """
        普通文本生成，返回原始字符串。
        用于 setup_orchestrator 读取 writing_guidance 等非结构化场景。
        [litellm] completion() — 统一调用各家 LLM，自动处理 API 格式差异
        """
        params = self._build_reasoning_params(model, thinking_budget, extra_params)

        call_params = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            **params,  # 将 reasoning/thinking 参数展开合并
        }
        if api_base:
            call_params["api_base"] = api_base
        if api_key_env:
            # [os] 从环境变量读取 API Key，避免硬编码
            call_params["api_key"] = os.getenv(api_key_env)

        response = completion(**call_params)
        result = response.choices[0].message.content

        if _glog and log:
            _glog.log(
                "LLM_CALL",
                {
                    "method": "generate",
                    "caller": caller,
                    "model": model,
                    "temperature": temperature,
                    "thinking_budget": thinking_budget,
                    "extra_params": extra_params,
                    "prompt_len": len(prompt),
                    "response_len": len(result) if result else 0,
                    "prompt": prompt,
                    "response": result,
                },
            )

        return result

    def generate_structured(
        self,
        prompt: str,
        response_model: Type[T],
        model: str = "dashscope/qwen3.5-flash",
        temperature: float = 0.2,
        thinking_budget: int | None = None,
        extra_params: dict | None = None,
        api_base: str | None = None,
        api_key_env: str | None = None,
        max_tokens: int = 32768,
        caller: str | None = None,
    ) -> T:
        """
        结构化输出：要求 LLM 返回符合 response_model 定义的 JSON，解析后返回模型实例。
        用于 Extractor 提取 Character / Event / Transition 等数据。

        两条路径：
        - native schema（支持 response_format）：[litellm] 直接传 Pydantic 模型，API 原生约束输出
        - prompt only（不支持）：将 JSON Schema 注入 system prompt，靠 LLM 理解指令输出
        """
        params = self._build_reasoning_params(model, thinking_budget, extra_params)

        # [lru_cache] O(1) 判断当前模型是否支持 native structured output
        use_prompt_only = self._needs_prompt_only_json(model)

        # 部分模型有硬性输出上限，防止 max_tokens 超出导致 API 报错
        model_cap = _MODEL_MAX_OUTPUT.get(model)
        if model_cap and max_tokens > model_cap:
            max_tokens = model_cap

        if use_prompt_only:
            # 不支持 native schema：把 JSON Schema 描述塞进 system prompt
            system_prompt = self._build_json_system_prompt(response_model)
            call_params = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
                "temperature": temperature,
                "max_tokens": max_tokens,
                **params,
            }
        else:
            # 支持 native schema：[litellm] 传入 Pydantic 类，自动转换为 JSON Schema
            call_params = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": temperature,
                "max_tokens": max_tokens,
                "response_format": response_model,
                **params,
            }

        if api_base:
            call_params["api_base"] = api_base
        if api_key_env:
            call_params["api_key"] = os.getenv(api_key_env)

        response = completion(**call_params)
        content = response.choices[0].message.content

        if use_prompt_only:
            # prompt only 路径下，LLM 可能包裹 ```json 代码块，需清理
            content = self._clean_json_response(content)

        # [pydantic + json_repair] 解析并校验，失败时自动修复后重试
        parsed = self._parse_or_repair(content, response_model)

        if _glog:
            _glog.log(
                "LLM_CALL",
                {
                    "method": "generate_structured",
                    "caller": caller,
                    "model": model,
                    "temperature": temperature,
                    "thinking_budget": thinking_budget,
                    "extra_params": extra_params,
                    "response_model": response_model.__name__,
                    "prompt_len": len(prompt),
                    "response_len": len(content) if content else 0,
                    "prompt": prompt,
                    "response": content,
                    # [pydantic] model_dump() 将 Pydantic 实例转为普通 dict，供 JSON 序列化
                    "parsed": parsed.model_dump(),
                },
            )

        return parsed

    def generate_stream(
        self,
        prompt: str,
        model: str = "dashscope/qwen3.5-flash",
        temperature: float = 0.3,
        thinking_budget: int | None = None,
        extra_params: dict | None = None,
        api_base: str | None = None,
        api_key_env: str | None = None,
    ) -> Iterator[str]:
        """
        流式文本生成，返回 Python 生成器（≈ C++20 co_yield 协程）。
        调用方每次 next() 取一个 token 片段，用于 SSE 实时推送给前端。

        [litellm] stream=True 开启流式模式，completion() 返回可迭代的 chunk 序列
        yield 使本函数成为生成器，数据逐块从网络流入，不会一次性加载全部内容到内存
        """
        params = self._build_reasoning_params(model, thinking_budget, extra_params)

        call_params = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "stream": True,  # [litellm] 开启流式响应
            **params,
        }
        if api_base:
            call_params["api_base"] = api_base
        if api_key_env:
            call_params["api_key"] = os.getenv(api_key_env)

        response = completion(**call_params)
        for chunk in response:
            # delta.content 为本次 chunk 新增的文本片段，None 表示结束信号
            if chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content

    def generate_structured_with_cache(
        self,
        prompt: str,
        response_model: Type[T],
        cached_content: str,
        model: str = "dashscope/qwen3.5-flash",
        temperature: float = 0.2,
        cache_ttl: str = "3600s",
        thinking_budget: int | None = None,
        extra_params: dict | None = None,
        api_base: str | None = None,
        api_key_env: str | None = None,
        caller: str | None = None,
    ) -> T:
        """
        带 prompt cache 的结构化输出，用于 Lorebook 等超长上下文场景。
        cached_content（如完整 Lorebook）在 Anthropic 服务端缓存 cache_ttl 时长，
        后续请求命中缓存可大幅降低 token 费用和首 token 延迟。

        - 支持 native schema 的模型：cache_control 附加在 system message 的 content 块上
        - 不支持的模型：退化为 prompt only，将 cached_content 和 JSON Schema 合并进 system prompt
        """
        params = self._build_reasoning_params(model, thinking_budget, extra_params)

        use_prompt_only = self._needs_prompt_only_json(model)

        if use_prompt_only:
            # 退化路径：将 Lorebook 内容和 JSON Schema 拼入同一个 system prompt
            json_system = self._build_json_system_prompt(response_model)
            combined_system = f"{cached_content}\n\n{json_system}"
            messages = [
                {"role": "system", "content": combined_system},
                {"role": "user", "content": prompt},
            ]
            call_params = {
                "model": model,
                "messages": messages,
                "temperature": temperature,
                **params,
            }
        else:
            # native schema 路径：cache_control 标记告知服务端缓存此 content 块
            messages = [
                {
                    "role": "system",
                    "content": [
                        {
                            "type": "text",
                            "text": cached_content,
                            # [litellm/Anthropic] ephemeral cache，服务端缓存 cache_ttl 后失效
                            "cache_control": {"type": "ephemeral", "ttl": cache_ttl},
                        }
                    ],
                },
                {"role": "user", "content": prompt},
            ]
            call_params = {
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "response_format": response_model,
                **params,
            }

        if api_base:
            call_params["api_base"] = api_base
        if api_key_env:
            call_params["api_key"] = os.getenv(api_key_env)

        response = completion(**call_params)
        content = response.choices[0].message.content

        if use_prompt_only:
            content = self._clean_json_response(content)

        parsed = self._parse_or_repair(content, response_model)

        if _glog:
            _glog.log(
                "LLM_CALL",
                {
                    "method": "generate_structured_with_cache",
                    "caller": caller,
                    "model": model,
                    "temperature": temperature,
                    "extra_params": extra_params,
                    "response_model": response_model.__name__,
                    "prompt_len": len(prompt),
                    "cached_content_len": len(cached_content),
                    "response_len": len(content) if content else 0,
                    "prompt": prompt,
                    "response": content,
                    "parsed": parsed.model_dump(),
                },
            )

        return parsed
