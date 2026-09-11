from pathlib import Path


def test_pyinstaller_bundles_huggingface_hub_for_local_model_picker():
    spec = Path("aicoder.spec").read_text(encoding="utf-8")
    assert "copy_metadata('huggingface_hub')" in spec
    assert "*collect_submodules('huggingface_hub')" in spec
