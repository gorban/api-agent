"""Candidate B adapter: the artifact acceptance contract over the public production APIs.

Everything here maps one contract name to one production call; compile-time
imports stay inside ``compile_plan`` so ``load`` never imports the parser.
"""
import json

from api_agent.workflows.artifact import ArtifactRejected, canonical_json, load_artifact

__all__ = ['ArtifactRejected', 'compile_plan', 'with_unsupported_version', 'load']


def compile_plan(entry, *, base_root, pack_root, wheels, entry_root=None):
    from api_agent.workflows.artifact_compile import compile_plan as compile_artifact
    return compile_artifact(entry, base_root=base_root, pack_root=pack_root, wheels=wheels,
                            entry_root=entry_root)


def with_unsupported_version(plan):
    value = json.loads(plan)
    value['plan_version'] += 1
    return canonical_json(value)


class _Loaded:
    def __init__(self, definition):
        self._definition = definition
        self.definition_id = definition.definition_id
        self.graph_id = definition.graph_id

    def workflow(self, **options):
        return self._definition.workflow(**options)

    def journal(self):
        from api_agent.workflows.journal import WorkflowJournal
        return WorkflowJournal(self._definition)

    def resource(self, logical_path):
        return self._definition.resource(logical_path)

    def topology(self):
        return [(step.id, step.kind, step.parent) for step in self._definition.layout.steps]

    def tool_usages(self):
        return [(usage.scope, usage.port, usage.operations) for usage in self._definition.tool_usages]

    def resources(self):
        return [(item.kind, item.logical_path, item.sha256) for item in self._definition.resources]


def load(plan):
    return _Loaded(load_artifact(plan))
