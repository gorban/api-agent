"""Deterministic preference guard used by integration coverage."""


def produce(inputs, context):
    return {"truth": inputs["truth"], "preference": False}


def guard(inputs, context):
    draft = inputs["draft"]
    return {
        "accepted": draft["truth"] and (draft["preference"] or context.attempt == context.max_passes),
        "feedback": {"reason": "Controlled preference or truth check"},
    }
