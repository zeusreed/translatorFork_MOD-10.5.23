import unittest

from gemini_translator.api import config as api_config


class CustomProviderModelsTests(unittest.TestCase):
    def setUp(self):
        api_config.initialize_configs()
        self._previous_custom_models = api_config.custom_provider_models_snapshot()

    def tearDown(self):
        api_config.set_custom_provider_models(self._previous_custom_models)

    def test_custom_models_are_merged_into_runtime_provider_config(self):
        api_config.set_custom_provider_models(
            {
                "openmodel": {
                    "My DeepSeek Alias": {
                        "id": "deepseek-v4-flash",
                        "rpm": 10,
                        "max_concurrent_requests": 1,
                    }
                }
            }
        )

        provider = api_config.api_providers()["openmodel"]
        all_models = api_config.all_models()

        self.assertIn("My DeepSeek Alias", provider["models"])
        self.assertEqual(provider["models"]["My DeepSeek Alias"]["id"], "deepseek-v4-flash")
        self.assertTrue(provider["models"]["My DeepSeek Alias"]["user_defined"])
        self.assertEqual(all_models["My DeepSeek Alias"]["provider"], "openmodel")


if __name__ == "__main__":
    unittest.main()
