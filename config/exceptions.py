class BIAgentError(Exception):
    """Base exception for BI agent system."""


class ToolExecutionError(BIAgentError):
    """Raised when a tool execution fails."""

    def __init__(self, tool_name: str, detail: str = "") -> None:
        self.tool_name = tool_name
        self.detail = detail
        super().__init__(f"Tool '{tool_name}' execution failed: {detail}")


class PlanParsingError(BIAgentError):
    """Raised when PlanAgent output cannot be parsed."""

    def __init__(self, raw_output: str, detail: str = "") -> None:
        self.raw_output = raw_output
        self.detail = detail
        super().__init__(f"Failed to parse plan output: {detail}")


class DataContextError(BIAgentError):
    """Raised when DataContext operation fails."""

    def __init__(self, key: str, operation: str, detail: str = "") -> None:
        self.key = key
        self.operation = operation
        self.detail = detail
        super().__init__(f"DataContext {operation} failed for key '{key}': {detail}")


class PythonExecError(BIAgentError):
    """Raised when Python code execution fails."""

    def __init__(self, detail: str, returncode: int = -1) -> None:
        self.returncode = returncode
        super().__init__(f"Python execution failed (rc={returncode}): {detail}")


class APIQueryError(BIAgentError):
    """Raised when API query fails."""

    def __init__(self, api_name: str, status_code: int = -1, detail: str = "") -> None:
        self.api_name = api_name
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"API '{api_name}' query failed (status={status_code}): {detail}")


class ReportGenerateError(BIAgentError):
    """Raised when report generation fails."""

    def __init__(self, format_type: str, detail: str = "") -> None:
        self.format_type = format_type
        super().__init__(f"Report generation failed for format '{format_type}': {detail}")


class SQLQueryError(BIAgentError):
    """Raised when SQL query validation or execution fails."""

    def __init__(self, detail: str = "", stage: str = "") -> None:
        self.stage = stage
        self.detail = detail
        prefix = f"SQL {stage} failed" if stage else "SQL query failed"
        super().__init__(f"{prefix}: {detail}")


class DataAnalysisError(BIAgentError):
    """Raised when data analysis tool fails."""

    def __init__(self, tool_name: str, detail: str = "") -> None:
        self.tool_name = tool_name
        super().__init__(f"Data analysis tool '{tool_name}' failed: {detail}")


class VisualizationError(BIAgentError):
    """Raised when visualization tool fails."""

    def __init__(self, tool_name: str, detail: str = "") -> None:
        self.tool_name = tool_name
        super().__init__(f"Visualization tool '{tool_name}' failed: {detail}")


class SearchError(BIAgentError):
    """Raised when search tool fails."""

    def __init__(self, search_type: str, detail: str = "") -> None:
        self.search_type = search_type
        super().__init__(f"Search '{search_type}' failed: {detail}")