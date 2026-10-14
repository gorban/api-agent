"""Controlled preference fallback is separate from the permanent LLM fixture."""
import asyncio
import json
import runpy
from api_agent.workflows.execution import Workflow, NodeFunction, WorkflowError
from api_agent.workflows.layout import load_layout
from api_agent.workflows.evidence import publish
import pytest

CATALOG = '''
def produce(inputs, context):
    return {'truth': inputs['truth'], 'preference': False}

def guard(inputs, context):
    draft = inputs['draft']
    return {'accepted': draft['truth'] and (draft['preference'] or context.attempt == context.max_passes),
            'feedback': {'reason': 'Controlled preference or truth check'}}
'''
LAYOUT = '''# LangGraph Layout v1

## Flow

1.  [component answer](functions.py)
    1.  [node draft](functions.py)
    2.  [node review](functions.py)

## Components

| ID | Max passes | Guard |
| --- | --- | --- |
| answer | 2 | review |

## Functions

| ID | Registry | Input | Output |
| --- | --- | --- | --- |
| draft | produce | [input](input.json) | [draft](draft.json) |
| review | guard | [review](review.json) | [guard](guard.json) |

## Bindings

| Node | Input | From | Field | Default |
| --- | --- | --- | --- | --- |
| draft | truth | START | truth | - |
| review | draft | draft | * | - |

## Edges

## Prompts

## Timeouts
'''


def controlled_workflow(root, events):
    root.mkdir(exist_ok=True)
    (root/'functions.py').write_text(CATALOG)
    (root/'layout.md').write_text(LAYOUT)
    draft={'type':'object','properties':{'truth':{'type':'boolean'},'preference':{'type':'boolean'}},'required':['truth','preference'],'additionalProperties':False}
    schemas={'input':{'type':'object','properties':{'truth':{'type':'boolean'}},'required':['truth'],'additionalProperties':False},'draft':draft,
             'review':{'type':'object','properties':{'draft':draft},'required':['draft'],'additionalProperties':False},
             'guard':{'type':'object','properties':{'accepted':{'type':'boolean'},'feedback':{'type':'object'}},'required':['accepted','feedback'],'additionalProperties':False}}
    for name,schema in schemas.items():
        (root/(name+'.json')).write_text(json.dumps(schema))
    catalog=runpy.run_path(str(root/'functions.py'))
    layout=load_layout(root/'layout.md',base_root=root,pack_root=root)
    return Workflow(layout,{'produce':NodeFunction(catalog['produce']),'guard':NodeFunction(catalog['guard'])},services={'observe':events.append})


def test_final_pass_relaxes_only_preference(tmp_path):
    events=[]
    workflow=controlled_workflow(tmp_path/'preferences',events)
    values=asyncio.run(workflow.arun({'truth':True}))
    publish({'events':events})
    assert values['draft']=={'truth':True,'preference':False}
    assert [row['accepted'] for row in events if row['event']=='guard']==[False,True]


def test_truth_failure_stays_blocking_on_final_pass(tmp_path):
    events=[]
    workflow=controlled_workflow(tmp_path/'truth',events)
    with pytest.raises(WorkflowError,match='exhausted'):
        asyncio.run(workflow.arun({'truth':False}))
    publish({'events':events})
    assert [row['accepted'] for row in events if row['event']=='guard']==[False,False]
