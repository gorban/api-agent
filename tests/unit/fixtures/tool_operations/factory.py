"""Test-only owner for the operations@1 interface; records every call."""


class Owner:
    version = 1

    def __init__(self, events):
        self.events = events

    async def invoke(self, operation, arguments):
        self.events.append((operation, dict(arguments)))
        return {'operation': operation, 'arguments': dict(arguments)}

    async def aclose(self):
        self.events.append(('close',))


def create_tool(local_config, services):
    return Owner(services['events'])
