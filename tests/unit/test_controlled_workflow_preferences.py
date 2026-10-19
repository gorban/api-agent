"""Run the controlled integration proof without live provider fixtures."""
from pathlib import Path
import runpy
import pytest


@pytest.mark.parametrize('name',['test_final_pass_relaxes_only_preference','test_truth_failure_stays_blocking_on_final_pass'])
def test_controlled_preference_proof(name):
    tests=runpy.run_path(str(Path(__file__).resolve().parents[2]/'tests/integration/test_workflow_preferences.py'))
    tests[name]()
