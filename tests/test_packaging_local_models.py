from pathlib import Path


def test_pyinstaller_bundles_huggingface_hub_for_local_model_picker():
    spec = Path("aicoder.spec").read_text(encoding="utf-8")
    assert "copy_metadata('huggingface_hub')" in spec
    assert "*collect_submodules('huggingface_hub')" in spec


def test_pyinstaller_bundles_gui_design_tokens_on_linux_and_windows():
    linux_spec = Path("aicoder.spec").read_text(encoding="utf-8")
    windows_spec = Path("aicoder-windows.spec").read_text(encoding="utf-8")

    expected = "collect_data_files('aicoder.gui', includes=['design_tokens.json'])"
    assert expected in linux_spec
    assert expected in windows_spec
    assert "datas=platform_datas + gui_datas" in windows_spec

    workflow = Path(".github/workflows/build-release.yml").read_text(encoding="utf-8")
    assert '--add-data "aicoder/gui/design_tokens.json;aicoder/gui"' in workflow
