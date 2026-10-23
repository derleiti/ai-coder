"""AICoder must not advertise IDs missing from a tier-scoped TriForce response."""
import os
from aicoder.gui.settings_widget import _ModelLoader


class StubClient:
    def model_catalog(self):
        return {
            'tier': 'registered',
            'models': ['ollama/qwen3:8b', 'nvidia/google/gemma-4-31b-it'],
            'model_details': [
                {'id': 'ollama/qwen3:8b', 'free_badge': True},
                {'id': 'nvidia/google/gemma-4-31b-it', 'free_endpoint': True,
                 'free_status': 'unknown', 'free_badge': False},
                {'id': 'paid/unlisted', 'free_badge': True},
            ],
        }


def test_loader_preserves_tier_authorized_ids(monkeypatch):
    monkeypatch.setattr('aicoder.gui.settings_widget.linked_account_catalog', lambda: {'models': []})
    loader = _ModelLoader(StubClient())
    messages = []
    loader.loaded.connect(lambda models, source: messages.append((models, source)))
    loader.run()
    assert messages
    ids = messages[0][0]
    assert ids == ['nvidia/google/gemma-4-31b-it', 'ollama/qwen3:8b']
    assert 'paid/unlisted' not in loader.model_details
    assert not loader.model_details['nvidia/google/gemma-4-31b-it']['free_badge']


def test_nvidia_build_free_endpoint_is_not_a_free_badge(monkeypatch):
    monkeypatch.setattr('aicoder.gui.settings_widget.linked_account_catalog', lambda: {'models': []})
    loader = _ModelLoader(StubClient())
    loader.run()
    nvidia = loader.model_details['nvidia/google/gemma-4-31b-it']
    assert nvidia['free_endpoint'] is True
    assert nvidia['free_status'] == 'unknown'
    assert nvidia['free_badge'] is False


def test_loader_does_not_expand_a_paid_tier_from_details(monkeypatch):
    class Client:
        def model_catalog(self):
            return {
                'tier': 'guest',
                'models': ['ollama/qwen3:8b'],
                'model_details': [
                    {'id': 'nvidia/nvidia/nemotron-3.5-lightning-30b-a3b',
                     'free_endpoint': True, 'free_badge': True},
                ],
            }
    monkeypatch.setattr('aicoder.gui.settings_widget.linked_account_catalog', lambda: {'models': []})
    loader = _ModelLoader(Client())
    seen = []
    loader.loaded.connect(lambda models, _: seen.extend(models))
    loader.run()
    assert seen == ['ollama/qwen3:8b']
    assert loader.model_details == {}
