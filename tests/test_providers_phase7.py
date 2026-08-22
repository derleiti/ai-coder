from __future__ import annotations

import json
import unittest
from datetime import date

from aicoder.model_capabilities import normalize_model_info, load_model_info
from aicoder.providers import credential_status, provider_status


class FakeClient:
    def list_models(self):
        return [
            {'id':'openrouter/vendor/model-a','provider':'openrouter','capabilities':['chat','tool_calling'],'context_window':131072,'available':True},
            {'id':'google/model-b','provider':'google','capabilities':['chat','vision'],'context_length':'1000000'},
            {'id':'mystery/model-c','provider':'mystery','capabilities':['chat']},
        ]


class ProviderDoctorTests(unittest.TestCase):
    def test_credential_values_are_never_returned(self):
        secret='DO_NOT_LEAK_123456'
        rows=credential_status(environ={'OPENAI_API_KEY':secret,'GOOGLE_GEMINI_KEY':secret},today=date(2026,8,22))
        text=json.dumps(rows)
        self.assertNotIn(secret,text)
        openai=next(row for row in rows if row['provider']=='openai')
        self.assertEqual(openai['environment_variables_present'],['OPENAI_API_KEY'])
        self.assertFalse(openai['credential_value_exposed'])
        google=next(row for row in rows if row['provider']=='google')
        self.assertEqual(google['legacy_variables_present'],['GOOGLE_GEMINI_KEY'])
        self.assertTrue(any('September 2026' in warning for warning in google['warnings']))

    def test_google_precedence_warning_when_both_official_vars_present(self):
        rows=credential_status(environ={'GOOGLE_API_KEY':'a','GEMINI_API_KEY':'b'},today=date(2026,8,22))
        google=next(row for row in rows if row['provider']=='google')
        self.assertTrue(any('GOOGLE_API_KEY' in warning and 'precedence' in warning for warning in google['warnings']))

    def test_backend_availability_comes_from_model_metadata(self):
        rows=provider_status(FakeClient(),environ={},today=date(2026,8,22))
        openrouter=next(row for row in rows if row['provider']=='openrouter')
        self.assertEqual(openrouter['backend_model_count'],1)
        self.assertEqual(openrouter['credential_source'],'backend')
        mystery=next(row for row in rows if row['provider']=='mystery')
        self.assertEqual(mystery['backend_model_count'],1)
        self.assertIn('backend model metadata',mystery['notes'])


class ModelInfoTests(unittest.TestCase):
    def test_normalizes_provider_capabilities_context_and_availability(self):
        info=normalize_model_info({'id':'openrouter/x/y','provider':'openrouter','capabilities':['chat','tool_calling','vision'],'context_window':'262144','available':False})
        self.assertEqual(info.id,'openrouter/x/y')
        self.assertEqual(info.provider,'openrouter')
        self.assertEqual(info.context_window,262144)
        self.assertFalse(info.available)
        self.assertIn('tool_calling',info.capabilities)
        self.assertIn('vision',info.capabilities)

    def test_load_model_info_keeps_unknown_provider_metadata(self):
        infos=load_model_info(FakeClient())
        self.assertEqual(infos['mystery/model-c'].provider,'mystery')
        self.assertEqual(infos['google/model-b'].context_window,1000000)


if __name__=='__main__': unittest.main()
