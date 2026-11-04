"""Shared history source, viewer and reporting preparation for both frontends."""


class AllureHistoryOwner:
    def _allure(self):
        if self.allure_viewer is None:
            from api_agent.testing.allure_viewer import ManagedAllureViewer
            values = self.session.result().values
            self.allure_viewer = ManagedAllureViewer(
                values.get('ALLURE_VIEWER_PORT', 5252), cli=values.get('ALLURE_CLI', 'allure'),
                history_count=values.get('ALLURE_HISTORY_DISPLAY_COUNT', 20))
        return self.allure_viewer

    async def prepare_allure_reporting(self, *, for_restore=False):
        values = self.session.result().values
        if (values.get('ALLURE_S3_BUCKET') and values.get('ALLURE_S3_USE_LOCALSTACK')
                and (for_restore or not values.get('ALLURE_VIEW_SECONDARY', False))):
            import os
            from api_agent.runtime.compose import plan_compose, ENGINE_ENVIRONMENT_KEYS
            from api_agent.runtime.ownership import StackScope
            from api_agent.runtime.tasks import offload
            environment = {key: os.environ[key] for key in ENGINE_ENVIRONMENT_KEYS if key in os.environ}
            config = {key: value for key, value in values.items()
                      if key.startswith('ALLURE_S3_') or key == 'ALLURE_LOCALSTACK_PORT'}
            plan = plan_compose(config, StackScope.from_roots(self.context.base_root, self.context.pack_root),
                                engine_environment=environment)
            await offload(self.reporting_sessions.acquire, plan, engine_environment=environment)

    def allure_source(self, *, primary=False):
        from api_agent.testing.allure_viewer import RunSource
        from api_agent.testing.storage import RunStorage
        from api_agent.testing.collection import repository_label
        values = self.session.result().values
        storage = None
        secondary = values.get('ALLURE_VIEW_SECONDARY', False) and not primary
        if values.get('ALLURE_S3_BUCKET') and not secondary:
            storage = RunStorage(values['ALLURE_S3_BUCKET'],
                prefix=values.get('ALLURE_S3_PREFIX', 'allure'),
                endpoint_url=values.get('ALLURE_S3_ENDPOINT'),
                region_name=values.get('ALLURE_S3_REGION'),
                localstack_port=values.get('ALLURE_LOCALSTACK_PORT', 4566),
                managed_local=values.get('ALLURE_S3_USE_LOCALSTACK', False))
        local_root = values.get('ALLURE_SECONDARY_DIR' if secondary else 'ALLURE_RESULTS_DIR')
        return RunSource(repository_label(self.context, self.context.owner_root),
                           local_root=local_root or None, storage=storage)

