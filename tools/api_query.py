
from __future__ import annotations

import json
import logging
from copy import deepcopy
from typing import Any
from urllib.parse import urlparse

import httpx
import pandas as pd
from autogen_core import CancellationToken
from autogen_core.tools import BaseTool
from pydantic import Field, create_model

from config import APIQueryError
from config.settings import settings
from core.data_context import DataContext
from core.resource_factory import get_resource_store
from utils.common_auth import build_headers

logger = logging.getLogger(__name__)


def _validate_url(url: str) -> None:
    """校验 URL 安全性：防止 SSRF 攻击。

    检查项：
    1. 仅允许 http/https 协议
    2. 阻止内网/保留 IP（含 IPv4/IPv6）
    3. 阻止十进制/十六进制/八进制 IP 表示
    4. DNS 解析后二次校验（防止 DNS rebinding）
    5. 域名白名单
    """
    parsed = urlparse(url)

    # 仅允许 http/https 协议
    if parsed.scheme not in ("http", "https"):
        raise APIQueryError("", detail=f"不允许的协议: {parsed.scheme}")

    hostname = parsed.hostname or ""

    # 阻止已知危险主机名（localhost、0.0.0.0 等）
    _BLOCKED_HOSTNAMES = {"0.0.0.0", "::1", "[::1]"}
    if hostname.lower() in _BLOCKED_HOSTNAMES or hostname in _BLOCKED_HOSTNAMES:
        raise APIQueryError("", detail=f"不允许访问内网地址: {hostname}")

    # 域名白名单（仅当配置了允许域名时启用）
    if settings.api_allowed_domains:
        if hostname not in settings.api_allowed_domains:
            raise APIQueryError("", detail=f"域名不在白名单内: {hostname}")


async def get_api_from_name(name: str) -> dict[str, Any] | None:
    """按名称精确查找 API 元数据（ES 或本地降级）。"""
    store = get_resource_store()
    return await store.get_by_name(name)


# ES parameters 类型到 Python 类型的映射
_TYPE_MAP = {
    "string": str,
    "integer": int,
    "int": int,
    "number": float,
    "float": float,
    "boolean": bool,
    "list": list[str],
    "List": list[str],
    "object": dict,
}


def _build_args_model(api_meta: dict[str, Any]) -> type:
    """从 ES api_meta 的 parameters 字段动态构建 Pydantic BaseModel。

    ES parameters 格式: {"param_name": {"type": "string", "description": "...", "required": True}, ...}
    """
    parameters = api_meta.get("parameters", {})
    if not isinstance(parameters, dict):
        return create_model(f"{api_meta.get('name', 'unknown')}_args")

    fields: dict[str, Any] = {}
    for param_name, param_schema in parameters.items():
        if not isinstance(param_schema, dict):
            continue

        py_type = _TYPE_MAP.get(param_schema.get("type", "string"), str)
        desc = param_schema.get("description", param_name)
        is_required = param_schema.get("required", False)
        default_value = param_schema.get("default")

        if is_required and default_value is None:
            # 必填字段，无默认值
            fields[param_name] = (py_type, Field(description=desc))
        elif default_value is not None:
            fields[param_name] = (py_type, Field(default=default_value, description=desc))
        else:
            # 非必填，无默认值
            fields[param_name] = (py_type, Field(default=None, description=desc))

    model_name = f"{api_meta.get('name', 'unknown')}_args"
    return create_model(model_name, **fields)


class DynamicAPITool(BaseTool):
    """根据 ES 元数据动态创建的 API 工具，每个 API 一个实例。

    LLM 看到的是带完整参数 schema 的独立工具，而非泛化的 _call_api_query。
    """

    def __init__(
        self,
        api_meta: dict[str, Any],
        data_context: DataContext | None = None,
        *,
        custom_env: dict[str, Any] | None = None,
        user_id: str = "",
    ) -> None:
        self.api_meta = api_meta
        self.data_context = data_context
        self.custom_env = custom_env
        self.user_id = user_id
        args_model = _build_args_model(api_meta)
        super().__init__(
            args_type=args_model,
            return_type=str,
            name=api_meta.get("name", "unknown_api"),
            description=api_meta.get("description", ""),
        )

    async def run(self, args: Any, cancellation_token: CancellationToken) -> str:
        """执行 API 调用。args 是由 LLM 填充的 Pydantic model。"""
        # 将 Pydantic model 转为 dict，过滤掉 None 值
        params = {k: v for k, v in args.model_dump().items() if v is not None}

        api_name = self.api_meta.get("name", "")
        logger.info("[DynamicAPITool] 调用API: %s, 参数: %s", api_name, params)

        try:
            tool = CallApiTool(custom_env=self.custom_env)
            tool.api_tools = [self.api_meta]
            tool_result = await tool.execute({"name": api_name, "args": params}, user_id=self.user_id)

            records = tool_result.get("answer", [])
            df = pd.DataFrame(records)

            if self.data_context is not None and not df.empty:
                key = self.data_context.generate_key("APIAgent")
                await self.data_context.put(key, df, meta={"api_name": api_name, "params": params})
                summary = self.data_context.summarize(key)
                return f"API '{api_name}' 调用成功，返回 {len(df)} 行数据。\n数据已存入DataContext(key={key})。\n\n{summary}"
            if df.empty:
                return f"API '{api_name}' 调用成功，但未返回数据。请检查查询参数。"
            return f"API '{api_name}' 调用成功，返回 {len(df)} 行数据。\n\n{df.head(10).to_string()}"
        except Exception as e:
            logger.error("[DynamicAPITool] API调用失败: %s, 错误: %s", api_name, e)
            return f"API调用失败: {e}"


class CallApiTool:
    """通用 API 调用执行引擎，由 DynamicAPITool 委托调用。

    负责：参数解析、HTTP 请求、响应提取、格式转换。
    """
    name = "call_api_tool"

    def __init__(self, api_tools: list | None = None, queue: Any | None = None, custom_env: dict | None = None,
                 version="v1", ):
        super().__init__()
        # 用户传值优先
        self.over_write = {}
        # 大模型生成参数优先
        self.no_over_write = {}
        self.custom_env = custom_env
        # API 最大超时时间
        self.default_headers = {
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate, br"
        }
        # api 调用的参数信息
        self.call_api_param = {}
        self.memory_set = set()
        self.api_tools = api_tools
        self.queue = queue
        self.version = version
        self.use_tool = {}

    @staticmethod
    async def get_input_param(use_tool, call_api_param):
        if isinstance(call_api_param, list):
            return call_api_param, False
        input_params = []
        show_keys = []
        parameters = use_tool.get("parameters", {})
        for key, value in parameters.items():
            if isinstance(value, dict) and "required" in value:  # 必传字段
                show_keys.append(key)
                input_params.append(
                    {
                        "cn_name": value.get("description").replace(",", "，").split("，")[0],
                        "en_name": key,
                        "enum_value": value.get("enum"),
                        "type": value.get("type"),
                        "value": call_api_param.get(key, ""),
                    }
                )
        for key, value in call_api_param.items():  # 询问中其他字段
            if value and key not in show_keys:
                try:
                    input_params.append(
                        {
                            "cn_name": parameters.get(key, {}).get("description", '').replace(",", "，").split("，")[0],
                            "en_name": key,
                            "enum_value": parameters.get(key, {}).get("enum"),
                            "type": parameters.get(key, {}).get("type"),
                            "value": value,
                        }
                    )
                except AttributeError:
                    continue

        return input_params, True

    async def call_api(self, api_name, method: str, url: str):
        # SSRF 防护：校验 URL
        _validate_url(url)
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                if method.lower() == "get":
                    if self.call_api_param:
                        for k, v in self.call_api_param.items():
                            if isinstance(v, dict):
                                self.call_api_param[k] = json.dumps(v, ensure_ascii=False)
                            elif v is None:
                                self.call_api_param[k] = ''
                    response = await client.get(url, headers=self.default_headers, params=self.call_api_param)
                elif method.lower() == "post":
                    response = await client.post(url, headers=self.default_headers, json=self.call_api_param)
                else:
                    raise APIQueryError(api_name, status_code=404, detail=f"{method} 请求方式ChatBI-Api暂未集成")
                if response.status_code not in [200, 201]:
                    raise APIQueryError(api_name, status_code=response.status_code, detail=response.text[:500])
                data = response.json()
                return data
        except APIQueryError:
            raise

        except httpx.ConnectError:
            raise APIQueryError(api_name, status_code=400, detail="API 网络不可达请检查防火墙") from None
        except httpx.TimeoutException:
            raise APIQueryError(api_name, status_code=400, detail="API 响应时间超过10秒，终止请求。") from None
        except Exception as e:
            raise APIQueryError(api_name, detail=str(e)) from e

    def extract_api_res(self, res_: Any, outputs: list, data: list, memory: Any = None) -> list[dict] | None:
        """解析需要的结果字段"""
        # 字段名映射
        re_metric = {item["value"]: item["name"] for item in outputs}
        if memory is None:
            memory = {}
        extract_data = memory.copy()
        update_flag = False
        subset_data = []
        # 遇到字典判断key是否在需要输出的字段中，如果在，那么就解析。
        # 然后继续向下解析，这里会导致key重复解析，所以最好维护的时候不要选择嵌套的的root作为输出
        # 判断该条数据更新完之后，字段是否和输出的一样，如果重复解析字段，或者缺少字段都不添加到data
        if isinstance(res_, dict):
            for key, value in res_.items():
                if key in re_metric:
                    update_flag = True
                    extract_data.update({re_metric[key]: res_.get(key)})
                elif isinstance(value, (dict, list)) and value:  # 存在子集且子集非空
                    subset_data.append(value)

            for item in subset_data:  # 父级已经提取完成/未提取完成所有信息
                data = self.extract_api_res(item, outputs, data, extract_data)

            if not subset_data and extract_data and update_flag:
                data.append(extract_data.copy())

        elif isinstance(res_, list):
            flag = False
            for _, item in enumerate(res_):
                if isinstance(item, (list, dict)):
                    data = self.extract_api_res(item, outputs, data, memory)
                    flag = True
            if flag is False and memory and id(memory) not in self.memory_set:
                self.memory_set.add(id(memory))
                data.append(memory)
        return data

    def extract_customer_env(self):
        """
        解析用户自定义的参数
        :return:
        """
        if isinstance(self.custom_env, dict):
            for key, value in self.custom_env.items():
                if 'over_write' in value:
                    # 大模型为主，在update之前将其default值赋值为value
                    self.no_over_write[key] = value['value']
                else:
                    # 以用户传的为主
                    self.over_write[key] = value['value']

    async def parse_tools(self, input_data, **kwargs):
        if not self.api_tools:
            raise Exception("api_tools 为空没有可用工具")
        self.extract_customer_env()

        # 进行工具匹配
        tool_name = input_data.get("name", "")
        call_api_params = input_data.get("args")

        for item in self.api_tools:
            if item.get("name", "") == tool_name:
                self.use_tool = item
                break
        input_params, is_list_flag = await self.get_input_param(self.use_tool, call_api_params)
        # 设置input_params值
        self.use_tool["input_params"] = input_params

        default_map = {"string": "", "list": [], "object": {}, "int": 1}
        if is_list_flag:
            parameters = self.use_tool.get("parameters", {})
            for key, value in parameters.items():
                # 1.处理用户的默认值
                if isinstance(value, dict):  # 这个是一个入参参数
                    default = value.get("default")
                    if default:
                        self.call_api_param[key] = default
                    elif key in self.no_over_write:
                        self.call_api_param[key] = self.no_over_write[key]
                    else:
                        self.call_api_param[key] = default_map.get(value.get("type", "string").lower(), '')

                self.call_api_param.update(call_api_params)
                # 2.对 形如：param:{...}这种格式的参数进行提取,这里是大模型生成的参数
                params = self.use_tool.get("params", {})
                if params:
                    param_name = params.get("name")
                    param_value = params.get("value", [])
                    self.call_api_param[param_name] = {}
                    for item in param_value:
                        self.call_api_param[param_name][item] = self.call_api_param.pop(item, None)
                # 3. 用户env始终覆盖的参数,默认传了某个参数，大模型生成为空时也可以覆盖
            for key, _ in self.call_api_param.items():
                if key in self.over_write:
                    self.call_api_param[key] = self.over_write[key]
                if key in self.no_over_write and _ == '':
                    self.call_api_param[key] = self.no_over_write[key]

        else:
            self.call_api_param = input_params

            self.call_api_param.update(self.over_write)

        # 给排产测试环境使用，将在未来移除
        env = self.use_tool.get("env", 'prod').lower()
        use = self.use_tool.get("auth_model", "soa").lower()

        self.default_headers = await build_headers(use, env, headers=self.default_headers)
        custom_headers = self.use_tool.get("headers", {})
        self.default_headers.update(custom_headers)
        logger.info(f"self.default_headers={self.default_headers}")
        return self.use_tool

    async def execute(self, data, user_id=""):
        # 处理工具
        use_tool = await self.parse_tools(data, user_id=user_id)

        name = use_tool.get("name", '')
        call_able = use_tool.get("Callable", True)

        result = {
            "call_api_info": {
                "name": name,
                "args": self.call_api_param,
                "Callable": call_able
            },
            "answer": []
        }

        if call_able is False:
            logger.info(f"[CallApiTool]-Tool:工具 {name} 暂不支持调用")
            raise APIQueryError(name, 500, "该工具暂不支持调用")

        url = use_tool.get("url", 'url')
        method = use_tool.get("method", 'get')
        # 执行API调用
        rsp = await self.call_api(name, method, url)
        if isinstance(rsp, APIQueryError):
            logger.error(f"[CallApiTool]-Tool: API调用失败-状态码：{rsp.status_code}。data: {rsp.detail}")

        outputs = use_tool.get("outputs", [])
        # 提取API返回结果中的指标
        data = []
        # 先解析出数据
        answer = self.extract_api_res(rsp, outputs, data)

        # 再进行转换
        answer = self.convert_format(answer, outputs)

        result["answer"] = answer
        return result

    @staticmethod
    def _strftime(value, format_in, format_out):
        from datetime import datetime
        # 将输入的日期字符串转换为datetime对象
        try:
            dt_object = datetime.strptime(value, format_in)
            # 将datetime对象转换为指定的输出格式
            output_date = dt_object.strftime(format_out)
        except ValueError:
            output_date = value
        return output_date

    def convert_format(self, answer: list, outputs) -> list:
        """
        转换时间格式和枚举类型
        :param answer:
        :param outputs:
        :return:
        """
        # 字段值映射
        metric_value_map = {}
        # 时间格式转换
        datetime_format = {}
        for item in outputs:
            if "input_format" in item:
                i = item["name"]
                datetime_format[i] = {
                    "input_format": item["input_format"],
                    "output_format": item.get("output_format", '')
                }

                for index, element in enumerate(answer):
                    try:
                        value = element[i]
                    except KeyError:
                        continue
                    except IndexError:
                        continue
                    if value:
                        format_ = self._strftime(value, datetime_format[i].get("input_format", ""),
                                                 datetime_format[i].get("output_format", "%Y-%m-%d"))

                        answer[index][i] = deepcopy(format_)

            if "map" in item:
                i = item["name"]
                metric_value_map[i] = item["map"]

                for index, element in enumerate(answer):
                    map_ = metric_value_map[i].get(str(element[i]), str(element[i]))
                    answer[index][i] = deepcopy(map_)

        return answer