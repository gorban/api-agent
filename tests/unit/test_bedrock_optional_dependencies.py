"""The provider remains discoverable and constructible without its SDK extra."""
import subprocess
import sys


def test_factory_and_cleanup_do_not_import_optional_sdk():
    script = '''
import asyncio
import importlib.abc
import sys

class NoSDK(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'boto3', 'botocore', 'openai', 'httpx2', 'awscrt'}:
            raise AssertionError('Optional SDK imported: ' + fullname)

sys.meta_path.insert(0, NoSDK())
from api_agent.llm_providers.bedrock.factory import factory
provider = factory({'BEDROCK_REGION': 'us-east-1', 'BEDROCK_API_MODE': 'responses'}, {})
assert provider.api_mode == 'responses'
assert provider.sessions == []
asyncio.run(provider.aclose())
'''
    result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
