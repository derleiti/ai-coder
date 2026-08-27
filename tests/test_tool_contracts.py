from aicoder.executor import (
    LOCAL_CODE_READ_SCHEMA, LOCAL_FILE_EDIT_SCHEMA, LOCAL_GIT_SCHEMA, LOCAL_TEST_SCHEMA,
    build_system_prompt, build_tool_desc,
)


def test_text_tool_description_includes_conditional_file_edit_contract():
    desc = build_tool_desc([LOCAL_FILE_EDIT_SCHEMA])
    assert "replace: use old_text + new_text" in desc
    assert "create/write/append: use content" in desc


def test_text_tool_description_exposes_types_enums_and_field_meaning():
    desc = build_tool_desc([LOCAL_GIT_SCHEMA, LOCAL_CODE_READ_SCHEMA])
    assert "action*:string enum[status|diff|log|show|branch]" in desc
    assert "args:array[string]" in desc
    assert "start_line:integer range[1..inf]" in desc
    assert "start_line and end_line are inclusive and 1-based" in desc


def test_test_tool_contract_prevents_duplicate_project_prefix_and_unchanged_fail_loop():
    desc = build_tool_desc([LOCAL_TEST_SCHEMA])
    assert "cwd is already the project working directory" in desc
    assert "do not prefix a relative test path with the project directory name again" in desc
    assert "failed test requires a corrective code/test change" in desc
    assert "zero tests collected is not successful behavior verification" in desc


def test_system_prompt_makes_host_approval_state_authoritative(tmp_path):
    crawl = {
        "name": "crawl",
        "description": "Crawl a web page",
        "inputSchema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    }
    prompt = build_system_prompt([crawl], str(tmp_path))
    assert "Do not ask the user for tool permission in prose first" in prompt
    assert "No approval dialog is NOT evidence of denial" in prompt
    assert "NEVER claim that the user approved, denied, rejected, or failed to approve" in prompt
    assert "CALL the exact active tool" in prompt
    assert "crawl(url*)" in prompt
    assert "schema: url*:string" in prompt


def test_system_prompt_does_not_require_unavailable_research_tools(tmp_path):
    prompt = build_system_prompt([], str(tmp_path))
    assert "if memory_search is active" in prompt
    assert "skip names not listed under ## Tools" in prompt
    assert "Use ONLY tools listed under ## Tools" in prompt
