
"""PromptManager：基于 Jinja2 + __PLACEHOLDER__ 的两阶段 prompt 渲染引擎。

Phase 1: Jinja2 解析结构性组合（include/macro/条件）
Phase 2: str.replace 注入运行时数据（避免 {{ }} 与 JSON 冲突）
"""

from __future__ import annotations

import logging
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined, TemplateError

logger = logging.getLogger(__name__)

# 默认 prompts 目录
_DEFAULT_PROMPTS_DIR = Path(__file__).parent / "prompts"


class PromptManager:
    """加载和渲染 agent prompt 的统一管理器。

    使用方式::

        pm = PromptManager()
        prompt = pm.render("plan_agent", DATE="2026年07月24日", API_LIST=api_list, SKILLS=skills)
    """

    def __init__(self, prompts_dir: Path | None = None) -> None:
        self.prompts_dir = prompts_dir or _DEFAULT_PROMPTS_DIR
        self._env = Environment(
            loader=FileSystemLoader(str(self.prompts_dir)),
            undefined=StrictUndefined,  # 变量未定义直接报错，防止静默失败
            keep_trailing_newline=True,
            trim_blocks=True,  # 去除 block 标签后的空行
            lstrip_blocks=True,  # 去除 block 标签前的空白
        )

    def render(self, agent_name: str, **runtime_vars: str) -> str:
        """渲染 agent prompt。

        Args:
            agent_name: agent 名称，对应 agents/{agent_name}.md.j2 文件。
                       也支持 "routing/{name}" 格式用于路由分类等非 agent prompt。
            **runtime_vars: 运行时变量，通过 __KEY__ 占位符注入。
                           例如 DATE="2026年07月24日" 会替换 __DATE__。

        Returns:
            渲染后的完整 prompt 字符串。

        Raises:
            TemplateError: Jinja2 模板渲染失败（如 include 文件不存在）。
        """
        # 确定模板路径
        if "/" in agent_name:
            template_path = f"{agent_name}.md.j2"
        else:
            template_path = f"agents/{agent_name}.md.j2"

        # Phase 1: Jinja2 结构渲染（include/macro）
        try:
            template = self._env.get_template(template_path)
            result = template.render()
        except TemplateError as e:
            logger.error("[PromptManager] 模板渲染失败: %s, 路径: %s", e, template_path)
            raise

        # Phase 2: str.replace 数据注入
        for key, value in runtime_vars.items():
            placeholder = f"__{key}__"
            result = result.replace(placeholder, value)

        return result

    def render_routing(self, name: str, **runtime_vars: str) -> str:
        """渲染路由分类 prompt（routing/ 目录下）。

        Args:
            name: 路由 prompt 名称（不含路径和后缀）。
            **runtime_vars: 运行时变量。
        """
        return self.render(f"routing/{name}", **runtime_vars)

    def reload(self) -> None:
        """清除模板缓存，用于热更新场景。"""
        self._env.cache.clear()
        logger.info("[PromptManager] 模板缓存已清除")

    def list_templates(self) -> list[str]:
        """列出所有可用的 prompt 模板。"""
        return self._env.list_templates()


# 全局单例，避免重复创建 Environment
_prompt_manager: PromptManager | None = None


def get_prompt_manager() -> PromptManager:
    """获取全局 PromptManager 单例。"""
    global _prompt_manager
    if _prompt_manager is None:
        _prompt_manager = PromptManager()
    return _prompt_manager