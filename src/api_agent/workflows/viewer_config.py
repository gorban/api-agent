"""Declared renderer selection without application or provider initialization."""
from pathlib import Path


def resolve_renderer_path(context=None, *, argv=(), environ=None, root=None):
    from api_agent.config import load_declarations, read_dotenv, resolve_config
    from api_agent.config.declarations import ConfigSchema, shared_declaration_path
    schema = load_declarations(shared_declaration_path(getattr(context, 'base_root', None)))
    if context is not None and hasattr(context, 'pack_layers'):
        from api_agent.runtime.pack_composition import load_context_schema
        schema = load_context_schema(context, schema)
    name = 'API_AGENT_PLANTUML_JAR'
    from api_agent.config.resolve import _controllers
    names = {name}
    while True:
        previous = set(names)
        for field_name in previous:
            declaration = schema.fields[field_name]
            for predicate in (declaration.applies_when, declaration.required_when,
                              *(row['when'] for row in declaration.conditional_defaults)):
                names.update(_controllers(predicate))
        if names == previous:
            break
    schema = ConfigSchema({field_name: schema.fields[field_name] for field_name in names})
    root = Path.cwd() if root is None else Path(root).resolve()
    paths = (root / '.env',)
    anchors = {'owner_root': root, 'base_root': root, 'pack_root': root}
    if context is not None and hasattr(context, 'owner_root'):
        paths = (context.base_root / '.env', *context.shared_dotenv_paths,
                 (context.launcher_root or context.base_root) / '.env', context.dotenv_path)
        anchors = context.anchors
    dotenv = {}
    for path in dict.fromkeys(paths):
        dotenv.update(read_dotenv(path))
    result = resolve_config(schema, argv=argv, environ=environ, dotenv=dotenv, anchors=anchors,
                            pack_launcher=bool(getattr(context, 'external_pack', False)))
    result.raise_for_errors()
    value = result.values.get(name)
    return str(value) if value else ''
