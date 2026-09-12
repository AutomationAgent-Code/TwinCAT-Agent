from types import SimpleNamespace as NS
from unittest.mock import patch, Mock
import pytest

from tc_template.member_baseline import capture
from tc_template import _native_bridge as nb
from tc_agent import agent_core as ac

PATH = 'TIPC^PLC1^PLC1 Project^POUs^FB_PID'


class Node:
    def __init__(self, name, kind=609, children=()):
        self._name = name
        self.ItemType = kind
        self.DeclarationText = 'METHOD ' + name
        self.ImplementationText = 'x := 1;'
        self.children = list(children)
        self.deleted = []
    def __iter__(self):
        return iter(self.children)
    @property
    def Name(self):
        return self._name
    @Name.setter
    def Name(self, value):
        self.DeclarationText = self.DeclarationText.replace('METHOD ' + self._name, 'METHOD ' + value)
        self._name = value
    def DeleteChild(self, name):
        self.deleted.append(name)
        self.children = [n for n in self.children if n.Name != name]


@pytest.fixture
def fixture():
    parent = Node('FB_PID', 604, [Node('First'), Node('Second')])
    dte = NS(Solution=NS(FullName='C:/P.sln'), MainWindow=NS(HWnd=42))
    with patch.object(nb, '_system_manager', return_value=object()), \
         patch.object(nb, '_find_object', return_value=(parent, PATH)), \
         patch.object(nb, '_symbol_references', return_value=[]):
        yield dte, parent


def args(token, name='First'):
    return {'pou': 'FB_PID', 'name': name, 'type': 'method', 'path': PATH,
            'expected_member_baseline': token}


def test_two_deletes_use_new_baseline_without_save(fixture):
    dte, parent = fixture
    token, _ = capture(dte, parent, PATH)
    result = nb._delete_member(dte, args(token))
    assert result['verified'] and result['written']
    stale = nb._delete_member(dte, args(token, 'Second'))
    assert stale['not_executed'] and parent.deleted == ['First']
    second = nb._delete_member(dte, args(result['member_baseline'], 'Second'))
    assert second['verified'] and parent.deleted == ['First', 'Second']


def test_property_delete_refreshes_invalidated_parent_then_continues():
    # Simulate XAE keeping a stale enumerator on the original parent wrapper.
    prop = Node('IsIdle',611,[Node('Get',613)])
    parent = Node('FB_PID',604,[prop,Node('Reset')])
    fresh = Node('FB_PID',604,[Node('Reset')])
    dte = NS(Solution=NS(FullName='C:/P.sln'),MainWindow=NS(HWnd=42))
    token,_=capture(dte,parent,PATH)
    def delete(name):
        assert name=='IsIdle'
        parent.deleted.append(name)
        # The old parent still enumerates the invalidated property.
        prop.ImplementationText=None
        prop.DeclarationText=None
    parent.DeleteChild=delete
    with patch.object(nb,'_system_manager',return_value=object()), \
         patch.object(nb,'_find_object',side_effect=[(parent,PATH),(fresh,PATH),(fresh,PATH),(fresh,PATH)]), \
         patch.object(nb,'_symbol_references',return_value=[]):
        result=nb._delete_member(dte,{**args(token,'IsIdle'),'type':'property'})
        assert result['verified'],result
        assert parent.deleted==['IsIdle']
        result2=nb._delete_member(dte,args(result['member_baseline'],'Reset'))
        assert result2['verified'] and fresh.children==[]


def test_refresh_failure_after_delete_stays_uncertain(fixture):
    dte,parent=fixture
    token,_=capture(dte,parent,PATH)
    with patch.object(nb,'_find_object',side_effect=[(parent,PATH),RuntimeError('disconnected')]):
        result=nb._delete_member(dte,args(token))
    assert result['status']=='uncertain' and result['written']
    assert parent.deleted==['First']


@pytest.mark.parametrize('change', ['parent', 'member', 'sibling', 'add', 'identity'])
def test_changes_block_before_delete(fixture, change):
    dte, parent = fixture
    token, _ = capture(dte, parent, PATH)
    if change == 'parent': parent.DeclarationText += ' changed'
    if change == 'member': parent.children[0].ImplementationText += ' changed'
    if change == 'sibling': parent.children[1].ImplementationText += ' changed'
    if change == 'add': parent.children.append(Node('Third'))
    if change == 'identity': dte.MainWindow.HWnd = 43
    result = nb._delete_member(dte, args(token))
    assert result['not_executed'] and not parent.deleted


def test_reference_gate_still_blocks(fixture):
    dte, parent = fixture
    token, _ = capture(dte, parent, PATH)
    with patch.object(nb, '_symbol_references', return_value=[{'pou': 'MAIN'}]):
        result = nb._delete_member(dte, args(token))
    assert result['status'] == 'blocked' and not parent.deleted


def test_rename_verifies_definition_and_new_baseline(fixture):
    dte, parent = fixture
    token, _ = capture(dte, parent, PATH)
    result = nb._rename_member(dte, {'pou': 'FB_PID', 'old': 'First', 'new': 'Third',
                                    'path': PATH, 'expected_member_baseline': token})
    assert result['verified'] and result['member_baseline'] != token


def test_dirty_precondition_allows_only_guarded_structure(fixture):
    dte, parent = fixture
    token, _ = capture(dte, parent, PATH)
    context = {'pid': 42, 'solution': 'C:/P.sln',
               'active_document': {'full_name': 'C:/FB_PID.TcPOU', 'saved': False}}
    request = {'pou': 'FB_PID', 'name': 'First', 'member_type': 'method', 'path': PATH}
    with patch.object(ac, 'ps_com', side_effect=lambda command, **kw:
                      {'logged_in':False} if command=='plc-online-state' else context), \
         patch.dict(ac._BY_NAME['plc_delete_member'], {'run': Mock(return_value={'status': 'deleted'})}):
        assert ac.run_tool('plc_delete_member', request, 42)['not_executed']
        assert ac.run_tool('plc_delete_member', {**request, 'expected_member_baseline': token}, 42)['status'] == 'deleted'


def test_unreadable_text_not_converted_to_empty(fixture):
    dte, parent = fixture
    token, _ = capture(dte, parent, PATH)
    parent.children[0].ImplementationText = None
    result = nb._delete_member(dte, args(token))
    assert result['not_executed'] and not parent.deleted


def test_interface_accessor_unknown_type_never_probed(fixture):
    dte, parent = fixture
    parent.children.append(Node('Get', 0))
    with pytest.raises(ValueError, match='Unsupported'):
        capture(dte, parent, PATH)


def test_nested_folder_member_delete(fixture):
    dte, parent = fixture
    parent.children = [Node('Folder', 601, [Node('M')])]
    token, _ = capture(dte, parent, PATH)
    result = nb._delete_member(dte, args(token, 'Folder.M'))
    assert result['verified'] and parent.children[0].children == []


def test_post_write_failure_is_uncertain_not_safe_retry(fixture):
    dte, parent = fixture
    token, _ = capture(dte, parent, PATH)
    original = parent.DeleteChild
    def changed(name):
        original(name)
        parent.ImplementationText += ' user edit'
    parent.DeleteChild = changed
    result = nb._delete_member(dte, args(token))
    assert result['status'] == 'uncertain' and result['written'] and not result['retry_safe']


def test_baseline_survives_context_compaction(fixture):
    dte, parent = fixture
    token, tree = capture(dte, parent, PATH)
    result = ac.tool_result_for_context('plc_read', {}, {'member_baseline': token, 'member_tree': tree})
    assert result['member_baseline'] == token


def test_guarded_dispatch_never_uses_legacy_fallback():
    from tc_template import _ps_bridge as bridge
    with bridge.tool_target(42), patch.object(bridge, '_native_request', return_value={'status': 'conflict'}) as native, \
         patch.object(bridge, '_ps_com_raw', side_effect=AssertionError('unsafe fallback')):
        bridge.ps_com('delete-member', path=PATH, expected_member_baseline={})
    assert native.call_args.args[2]['strictPid'] is True
