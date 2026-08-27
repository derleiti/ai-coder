from aicoder.executor import LOCAL_FILE_EDIT_SCHEMA, build_tool_desc


def test_text_tool_description_includes_conditional_file_edit_contract():
    desc = build_tool_desc([LOCAL_FILE_EDIT_SCHEMA])
    assert "replace: use old_text + new_text" in desc
    assert "create/write/append: use content" in desc
