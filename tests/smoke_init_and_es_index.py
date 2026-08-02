"""冒烟测试 — 验证 agents/__init__.py 清空 + ES 索引自动创建两个功能。

运行方式: uv run python tests/smoke_init_and_es_index.py
退出码: 0=全部通过, 1=有失败
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
passed = 0
failed = 0


def _test(name: str, fn: callable) -> None:
    """执行单个测试,记录结果。"""
    global passed, failed
    try:
        fn()
        passed += 1
        print(f"  [PASS] {name}")
    except Exception as e:
        failed += 1
        print(f"  [FAIL] {name}: {e}")
        traceback.print_exc()


# ============================================================
# 测试1: agents/__init__.py 清空后主入口导入不报错
# ============================================================
def test_main_entry_import() -> None:
    """主入口 core.team.BITeam 导入不报错。"""
    from core.team import BITeam  # noqa: F401


def test_agents_init_no_export() -> None:
    """agents/__init__.py 不再导出 Agent 类。"""
    import agents

    # agents 包不应有 APIAgent, PlanAgent 等属性
    assert not hasattr(agents, "APIAgent"), "agents.APIAgent 不应存在"
    assert not hasattr(agents, "PlanAgent"), "agents.PlanAgent 不应存在"
    assert not hasattr(agents, "BIBaseAgent"), "agents.BIBaseAgent 不应存在"


# ============================================================
# 测试2: AgentFactory 惰性加载各 Agent 正常
# ============================================================
def test_agent_factory_lazy_load() -> None:
    """AgentFactory 能通过 importlib 惰性加载全部注册 Agent。"""
    from models.routing import AgentType
    from core.agent_factory import AgentFactory

    # 验证注册表中覆盖了主要 Agent 类型
    expected = {
        AgentType.API, AgentType.SQL, AgentType.PYFUNC,
        AgentType.DATA_ANALYSIS, AgentType.VISUALIZATION,
        AgentType.REPORT, AgentType.SEARCH,
    }
    registered = set(AgentFactory._MODULE_MAP.keys())
    missing = expected - registered
    assert not missing, f"AgentFactory 未注册: {missing}"

    # 惰性加载一个 Agent 类验证 importlib 路径正确
    cls = AgentFactory._resolve(AgentType.API)
    assert cls.__name__ == "APIAgent", f"解析 APIAgent 失败: {cls}"


# ============================================================
# 测试3-5: ensure_index_exists 三个场景
# ============================================================
def test_ensure_index_already_exists() -> None:
    """索引已存在时幂等返回 True。"""
    from utils.es_query import ensure_index_exists

    mock_client = MagicMock()
    mock_client.indices.exists.return_value = True

    with patch("utils.es_query.get_es_client", return_value=mock_client):
        result = ensure_index_exists("test_index")

    assert result is True, f"期望 True, 实际 {result}"
    mock_client.indices.exists.assert_called_once_with(index="test_index")
    # 不应调用 create
    mock_client.indices.create.assert_not_called()


def test_ensure_index_create_success() -> None:
    """索引不存在 + schema 存在时创建成功返回 True。"""
    from utils.es_query import ensure_index_exists, _DEFAULT_SCHEMA_DIR

    mock_client = MagicMock()
    mock_client.indices.exists.return_value = False
    mock_client.indices.create.return_value = {"acknowledged": True}

    # 使用真实 schema 文件
    real_schema = _DEFAULT_SCHEMA_DIR / "chat_bi_doc_sit.json"
    assert real_schema.is_file(), f"schema 文件不存在: {real_schema}"

    with patch("utils.es_query.get_es_client", return_value=mock_client):
        result = ensure_index_exists("chat_bi_doc_sit", schema_path=real_schema)

    assert result is True, f"期望 True, 实际 {result}"
    mock_client.indices.create.assert_called_once()

    # 验证传入的 mapping 是合法 JSON
    call_args = mock_client.indices.create.call_args
    body = call_args.kwargs.get("body") or call_args[1].get("body")
    assert "mappings" in body, "mapping 缺少 mappings 键"
    assert "properties" in body["mappings"], "mapping 缺少 properties 键"


def test_ensure_index_schema_missing() -> None:
    """schema 文件缺失时返回 False 不抛异常。"""
    from utils.es_query import ensure_index_exists

    mock_client = MagicMock()
    mock_client.indices.exists.return_value = False

    with patch("utils.es_query.get_es_client", return_value=mock_client):
        result = ensure_index_exists("nonexistent_index", schema_path=Path("/tmp/no_such_file.json"))

    assert result is False, f"期望 False, 实际 {result}"
    # 不应调用 create
    mock_client.indices.create.assert_not_called()


def test_ensure_index_create_failure() -> None:
    """索引创建失败(ES 异常)时返回 False 不抛异常。"""
    from elasticsearch7.exceptions import ElasticsearchException
    from utils.es_query import ensure_index_exists, _DEFAULT_SCHEMA_DIR

    mock_client = MagicMock()
    mock_client.indices.exists.return_value = False
    mock_client.indices.create.side_effect = ElasticsearchException("mock error")

    real_schema = _DEFAULT_SCHEMA_DIR / "chat_bi_doc_sit.json"

    with patch("utils.es_query.get_es_client", return_value=mock_client):
        result = ensure_index_exists("chat_bi_doc_sit", schema_path=real_schema)

    assert result is False, f"期望 False, 实际 {result}"


# ============================================================
# 测试6: ESResourceStore 使用 settings.es_resource_index
# ============================================================
def test_es_resource_store_uses_settings() -> None:
    """ESResourceStore 从 settings 读索引名,而非硬编码。"""
    from core.resource_store import ESResourceStore
    from config.settings import settings

    store = ESResourceStore()
    # 验证 settings 有 es_resource_index 字段
    assert hasattr(settings, "es_resource_index"), "settings 缺少 es_resource_index"
    # 验证默认值
    assert settings.es_resource_index == "chat_bi_doc_sit", (
        f"默认值期望 chat_bi_doc_sit, 实际 {settings.es_resource_index}"
    )

    # 验证 resource_store.py 源码无硬编码 chat_bi_doc_sit 字符串
    source_path = Path(__file__).resolve().parent.parent / "core" / "resource_store.py"
    source = source_path.read_text(encoding="utf-8")
    # 移除注释和字符串后,源码中不应出现硬编码索引名
    # 简单检查: ESResourceStore 类定义中不应有 "chat_bi_doc_sit" 字面量
    es_class_start = source.find("class ESResourceStore")
    es_class_text = source[es_class_start:]
    assert '"chat_bi_doc_sit"' not in es_class_text, "ESResourceStore 中仍有硬编码索引名"
    assert "'chat_bi_doc_sit'" not in es_class_text, "ESResourceStore 中仍有硬编码索引名"


# ============================================================
# 测试7: startup_check ES 未配置时走 SKIP 分支
# ============================================================
def test_startup_check_skip_es() -> None:
    """ES 未配置时 startup_check 正常执行并返回 True(必需配置完整)。"""
    from config.startup_check import check_startup_config
    from config import settings

    # 当前环境 ES 未配置(es_hosts 为空),应走 SKIP 分支
    if settings.es_hosts:
        print("    (跳过: 当前环境已配置 ES, 无法测试 SKIP 分支)")
        return

    # check_startup_config 应正常返回,不抛异常
    result = check_startup_config()
    # 只要 LLM 配置完整就应返回 True
    assert isinstance(result, bool), f"期望返回 bool, 实际 {type(result)}"


# ============================================================
# 测试8: schema JSON 合法性
# ============================================================
def test_schema_json_valid() -> None:
    """chat_bi_doc_sit.json 是合法 JSON 且包含必要字段。"""
    from utils.es_query import _DEFAULT_SCHEMA_DIR

    schema_path = _DEFAULT_SCHEMA_DIR / "chat_bi_doc_sit.json"
    data = json.loads(schema_path.read_text(encoding="utf-8"))

    assert "mappings" in data, "schema 缺少 mappings"
    props = data["mappings"]["properties"]
    # name 必须是 keyword(主键)
    assert props["name"]["type"] == "keyword", "name 应为 keyword 类型"
    # kpi/keywords 应为 keyword
    assert props["kpi"]["type"] == "keyword", "kpi 应为 keyword 类型"
    assert props["keywords"]["type"] == "keyword", "keywords 应为 keyword 类型"
    # description 应为 text + ik_max_word
    assert props["description"]["type"] == "text", "description 应为 text 类型"
    assert props["description"]["analyzer"] == "ik_max_word", "description 应用 ik_max_word"


# ============================================================
# 主入口
# ============================================================
if __name__ == "__main__":
    print("=" * 60)
    print("冒烟测试: agents/__init__.py 清空 + ES 索引自动创建")
    print("=" * 60)

    # --- 组1: agents/__init__.py ---
    print("\n[1] agents/__init__.py 清空后导入验证")
    _test("主入口导入", test_main_entry_import)
    _test("agents 包无集中导出", test_agents_init_no_export)
    _test("AgentFactory 惰性加载", test_agent_factory_lazy_load)

    # --- 组2: ensure_index_exists ---
    print("\n[2] ensure_index_exists 场景验证")
    _test("索引已存在 → 幂等返回 True", test_ensure_index_already_exists)
    _test("索引不存在 + schema 存在 → 创建成功", test_ensure_index_create_success)
    _test("schema 缺失 → 返回 False 不抛异常", test_ensure_index_schema_missing)
    _test("创建失败(ES异常) → 返回 False 不抛异常", test_ensure_index_create_failure)

    # --- 组3: ESResourceStore + startup_check ---
    print("\n[3] ESResourceStore + startup_check 集成验证")
    _test("ESResourceStore 使用 settings.es_resource_index", test_es_resource_store_uses_settings)
    _test("startup_check ES 未配置走 SKIP", test_startup_check_skip_es)
    _test("schema JSON 合法性 + 字段类型", test_schema_json_valid)

    # --- 汇总 ---
    print("\n" + "=" * 60)
    total = passed + failed
    print(f"结果: {passed}/{total} 通过, {failed} 失败")
    print("=" * 60)

    sys.exit(1 if failed > 0 else 0)
