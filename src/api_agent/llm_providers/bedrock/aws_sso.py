"""IAM Identity Center session acquisition through the shared AWS login owner."""
from api_agent.llm_providers.bedrock.aws_login import AwsLoginAcquirer


def factory(config, services):
    return AwsLoginAcquirer(config, method='sso')
