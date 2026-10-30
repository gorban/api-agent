"""Private, disposable projection process for bounded evidence handoff."""
import json
import sys

_INPUT_LIMIT = 6 * 1024 * 1024
_OUTPUT_LIMIT = 9 * 1024 * 1024


def main():
    # Linux has an address-space cap; Darwin relies on the parent's sampled
    # resident-memory watchdog, which is explicitly best effort. Both use
    # bounded input, CPU time and a parent-owned wall deadline.
    try:
        if sys.platform not in ('linux', 'darwin'):
            return 1
        import resource
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        if sys.platform == 'linux':
            resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_CPU, (4, 4))
        payload = sys.stdin.buffer.read(_INPUT_LIMIT + 1)
        if len(payload) > _INPUT_LIMIT:
            return 1
        value = json.loads(payload)
        if (type(value) is not dict or set(value) - {'limit'} != {'rows', 'secrets', 'ids'}
                or ('limit' in value and (type(value['limit']) is not int or value['limit'] != 65536))):
            return 1
        from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
        from api_agent.providers.setup_transport import SetupServer
        import threading
        projection = EmissionBoundary(ContentPolicy(True), secrets=value['secrets'],
                                      assigned_ids=value['ids'], preserve_json_text=True)
        frames = SetupServer._prepare_cancellable(value['rows'], threading.Event(), projection, True,
                                                  **({'limit': value['limit']} if 'limit' in value else {}))
        result = json.dumps([[phase, status, frame.decode('utf-8')] for phase, status, frame in frames],
                            ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        if len(result) > _OUTPUT_LIMIT:
            return 1
        sys.stdout.buffer.write(result)
        return 0
    except BaseException:
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
