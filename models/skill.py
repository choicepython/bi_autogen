
"""Skill 数据模型——本地业务技能的结构化表示。

每个 Skill 对应 skills/<name>/SKILL.md 文件，包含 frontmatter（name/description）
和 markdown body（执行规则、名词释义、名称清单、溯源信息等）。
"""

from __future__ import annotations

from pydantic import BaseModel


class Skill(BaseModel):
    """业务技能——指导特定场景的执行步骤、计算规则或名称清单。

    Attributes:
        name: Skill 名称（frontmatter name 字段，同时也是目录名）。
        description: 触发条件描述（frontmatter description 字段）。
        ass_resource: 关联的 API 工具名列表，从 description + body 用正则提取。
        content: SKILL.md body 全文（frontmatter 之后的 markdown）。
        source_site: 溯源站点（从 body 溯源信息 section 解析，可能为空）。
        key_id: 溯源 key_id（从 body 溯源信息 section 解析，可能为空）。
    """

    name: str
    description: str = ""
    ass_resource: list[str] = []
    content: str = ""
    source_site: str = ""
    key_id: str = ""

    def to_prompt_text(self) -> str:
        """渲染为 prompt 注入文本块。"""
        parts = [f"### {self.name}"]
        if self.ass_resource:
            parts.append(f"关联工具: {self.ass_resource}")
        parts.append(self.content)
        return "\n".join(parts)