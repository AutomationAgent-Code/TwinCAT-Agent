import pytest
from tc_template.plc_syntax import syntax_findings


def rules(declaration):
    return {f['rule'] for f in syntax_findings({'declaration': declaration})}


@pytest.mark.parametrize('name', ['9abc', 'a-b', 'a b', 'a中文', 'aß'])
def test_invalid_identifier(name):
    assert 'syntax-identifier' in rules('VAR ' + name + ':INT; END_VAR')


@pytest.mark.parametrize('name', ['abc', '_abc', 'A_1', '__internal', 'a' * 300])
def test_no_invented_length_or_underscore_ban(name):
    assert not rules('VAR ' + name + ':INT; END_VAR')


def test_case_insensitive_duplicate_across_blocks():
    assert 'syntax-duplicate-variable' in rules('VAR_INPUT a:INT; END_VAR VAR A:INT; END_VAR')


def test_constant_initializer_required():
    assert 'syntax-constant-initializer' in rules('VAR CONSTANT x:INT; END_VAR')


def test_empty_string_initializer_is_present():
    assert not rules("VAR CONSTANT x:STRING := ''; END_VAR")


def test_readonly_reference_is_not_a_constant_variable():
    assert not rules('VAR_IN_OUT CONSTANT x:STRING; END_VAR')
    assert 'syntax-inout-initializer' in rules("VAR_IN_OUT CONSTANT x:STRING := ''; END_VAR")


def test_conditional_declaration_not_flattened():
    assert not rules('{IF defined (X)} VAR x:INT; END_VAR {ELSE} VAR x:INT; END_VAR {END_IF}')


def test_comment_does_not_supply_initializer():
    assert 'syntax-constant-initializer' in rules('VAR CONSTANT x:INT (* := 2 *); END_VAR')
