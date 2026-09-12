"""PID/task-bound facades for saved HMI source indexing (no editor writes)."""
from tc_agent import hmi_source_index as index
from tc_agent.plc_cache import scoped_solution
from tc_template._ps_bridge import ps_com


def _solution():
    return scoped_solution() or str(ps_com('project-info').get('solution') or '')


def source_index(project='', refresh=False):
    return index.sync(_solution(), project, refresh)


def source_catalog(project='', **kwargs):
    return index.catalog(_solution(), project, **kwargs)


def read_smart(file, project='', **kwargs):
    return index.read(_solution(), file, project, **kwargs)
