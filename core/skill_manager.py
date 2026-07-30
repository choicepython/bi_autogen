
"""SkillManager：本地业务技能的加载、匹配与格式化。

镜像 config/prompt_manager.py 的单例模式：扫描 skills/ 目录，解析 SKILL.md
frontmatter + body，按 API 名匹配，格式化为 prompt 注入文本。

使用方式::

    from core.skill_manager import get_skill_manager
    sm = get_skill_manager()
    skills = sm.match_skills_by_api_names(["lps_getDailyUnpack"])
    text = sm.format_skills_for_prompt(skills)
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import yaml

from models.skill import Skill

logger = logging.getLogger(__name__)

# 项目根目录（core/ 的父目录）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_SKILLS_DIR = _PROJECT_ROOT / "skills"

# 从文本中提取 API 工具名的正则（覆盖 description JSON 数组 / body ass_resource 行 / body 自然语言）
# 限定 ASCII 字符，避免 \w 匹配到尾部中文字符
_API_NAME_PATTERN = re.compile(r"lps_[a-zA-Z0-9_]+")

# 从 body 溯源信息 section 提取字段的正则
_SOURCE_SITE_PATTERN = re.compile(r"^source_site:\s*(.+)$", re.MULTILINE)
_KEY_ID_PATTERN = re.compile(r"^key_id:\s*(.+)$", re.MULTILINE)


def _parse_skill_md(content: str) -> tuple[dict[str, str], str]:
    """解析 SKILL.md：分离 frontmatter（YAML）和 body（markdown）。

    Returns:
        (frontmatter_dict, body_str) — frontmatter 解析失败时返回空 dict + 原文。
    """
    if not content.startswith("---"):
        # 无 frontmatter，整体作为 body
        return {}, content

    # 找第二个 --- 作为 frontmatter 结束
    end_idx = content.find("\n---", 3)
    if end_idx == -1:
        # 只有开头 ---，无闭合，整体作为 body
        return {}, content

    frontmatter_text = content[3:end_idx].strip()
    body = content[end_idx + 4:].lstrip("\n")

    try:
        frontmatter = yaml.safe_load(frontmatter_text) or {}
        if not isinstance(frontmatter, dict):
            return {}, content
        # 统一为 str 值
        return {k: str(v) if v is not None else "" for k, v in frontmatter.items()}, body
    except yaml.YAMLError as e:
        logger.warning("[SkillManager] frontmatter 解析失败: %s", e)
        return {}, content


def _extract_ass_resource(description: str, body: str) -> list[str]:
    """从 description + body 全文提取 API 工具名（去重保序）。"""
    text = f"{description}\n{body}"
    seen: set[str] = set()
    result: list[str] = []
    for match in _API_NAME_PATTERN.finditer(text):
        name = match.group(0)
        if name not in seen:
            seen.add(name)
            result.append(name)
    return result


def _parse_skill_file(path: Path) -> Skill | None:
    """解析单个 SKILL.md 文件为 Skill 对象。"""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("[SkillManager] 读取失败: %s — %s", path, e)
        return None

    frontmatter, body = _parse_skill_md(raw)
    name = frontmatter.get("name") or path.parent.name
    description = frontmatter.get("description", "")
    ass_resource = _extract_ass_resource(description, body)

    # 从 body 溯源信息 section 提取
    source_site = ""
    key_id = ""
    source_site_match = _SOURCE_SITE_PATTERN.search(body)
    if source_site_match:
        source_site = source_site_match.group(1).strip()
    key_id_match = _KEY_ID_PATTERN.search(body)
    if key_id_match:
        key_id = key_id_match.group(1).strip()

    return Skill(
        name=name,
        description=description,
        ass_resource=ass_resource,
        content=body.strip(),
        source_site=source_site,
        key_id=key_id,
    )


class SkillManager:
    """本地业务技能管理器——加载、匹配、格式化。

    单例模式（通过 get_skill_manager() 获取），加载结果缓存直到显式 reload。
    """

    def __init__(self, skills_dir: Path | None = None) -> None:
        self.skills_dir = skills_dir or _DEFAULT_SKILLS_DIR
        self._skills: list[Skill] | None = None

    def load_skills(self, *, force_reload: bool = False) -> list[Skill]:
        """加载 skills/ 目录下所有 SKILL.md。

        Args:
            force_reload: True 时强制重新扫描（清除缓存）。

        Returns:
            Skill 列表（按目录名排序）。目录不存在时返回空列表。
        """
        if self._skills is not None and not force_reload:
            return self._skills

        skills: list[Skill] = []
        if not self.skills_dir.is_dir():
            logger.warning("[SkillManager] skills 目录不存在: %s", self.skills_dir)
            self._skills = skills
            return skills

        for skill_dir in sorted(self.skills_dir.iterdir()):
            if not skill_dir.is_dir():
                continue
            skill_file = skill_dir / "SKILL.md"
            if not skill_file.is_file():
                continue
            skill = _parse_skill_file(skill_file)
            if skill is not None:
                skills.append(skill)

        logger.info("[SkillManager] 加载了 %d 个 Skill", len(skills))
        self._skills = skills
        return skills

    def match_skills_by_api_names(self, api_names: list[str]) -> list[Skill]:
        """按 API 工具名交集匹配 Skill。

        Args:
            api_names: ES 召回的 API 工具名列表。

        Returns:
            ass_resource 与 api_names 有交集的 Skill 列表（保持加载顺序）。
        """
        if not api_names:
            return []
        skills = self.load_skills()
        api_set = set(api_names)
        return [s for s in skills if api_set & set(s.ass_resource)]

    def format_skills_for_prompt(self, skills: list[Skill]) -> str:
        """格式化 Skill 列表为 prompt 注入文本。

        Returns:
            注入 __SKILLS__ 占位符的文本。空列表返回空串（占位符被替换为空）。
        """
        if not skills:
            return ""
        lines = ["\n**业务技能是指导工作执行步骤的重要依据！！！**\n"]
        for skill in skills:
            lines.append(skill.to_prompt_text())
            lines.append("")  # Skill 间空行分隔
        return "\n".join(lines)

    def reload(self) -> None:
        """清除缓存，下次 load_skills/match 时重新扫描。"""
        self._skills = None
        logger.info("[SkillManager] 缓存已清除")


# 全局单例
_skill_manager: SkillManager | None = None


def get_skill_manager() -> SkillManager:
    """获取全局 SkillManager 单例。"""
    global _skill_manager
    if _skill_manager is None:
        _skill_manager = SkillManager()
    return _skill_manager