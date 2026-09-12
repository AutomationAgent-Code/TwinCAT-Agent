import json
import unittest
from html import escape
from tc_template.hmi_events import HmiEventValidationError, plan_event, read_events, validate_actions


class EventsTests(unittest.TestCase):
    def setUp(self):
        self.catalog = {'controls': {'TcHmi.Controls.Beckhoff.TcHmiButton': {
            'events': [{'name': '.onPressed'}, {'name': '.onReleased'}]}}}
        self.actions = [{'objectType': 'JavaScript', 'sourceLines': ['window.demo = 1;']}]

    def markup(self, triggers):
        value = escape(json.dumps(triggers), quote=True)
        return '<div id="Test" data-tchmi-type="TcHmi.Controls.Beckhoff.TcHmiButton" data-tchmi-trigger="' + value + '"></div>'

    def test_upsert_keeps_other_events_and_metadata(self):
        other = {'event': 'Test.onReleased', 'actions': self.actions}
        existing = {'event': 'Test.onPressed', 'preventDefault': True, 'actions': self.actions}
        actions = [{'objectType': 'JavaScript', 'sourceLines': ['window.demo = 2;']}]
        result = plan_event(self.markup([other, existing]), 'Test', '.onPressed', actions, self.catalog)
        self.assertEqual(result[0], other)
        self.assertTrue(result[1]['preventDefault'])
        self.assertEqual(result[1]['event'], '.onPressed')
        self.assertEqual(result[1]['actions'], actions)

    def test_remove_only_selected_event(self):
        original = [{'event': '.onPressed', 'actions': self.actions}]
        self.assertEqual(plan_event(self.markup(original), 'Test', '.onPressed', [], self.catalog, 'remove'), [])

    def test_custom_placement_is_explicit_and_control_qualified(self):
        result = plan_event(
            self.markup([]), 'Test', '.onPressed', self.actions,
            self.catalog, placement='custom',
        )
        self.assertEqual('Test.onPressed', result[0]['event'])

    def test_installed_owner_context_format_migrates_short_native_event(self):
        from tc_template.hmi_events import OWNER_EVENT_PREFIX
        installed = dict(self.catalog, native_event_prefix=OWNER_EVENT_PREFIX)
        original = self.markup([{'event': '.onPressed', 'actions': self.actions}])
        result = plan_event(original, 'Test', '.onPressed', self.actions, installed)
        self.assertEqual(OWNER_EVENT_PREFIX + '.onPressed', result[0]['event'])
        custom = plan_event(original, 'Test', '.onPressed', self.actions, installed, placement='custom')
        self.assertEqual('Test.onPressed', custom[0]['event'])

    def test_owner_context_is_only_allowed_in_event_fields(self):
        from tc_template.hmi_symbols import check_symbol_value
        from tc_template.hmi_events import OWNER_EVENT_PREFIX
        check_symbol_value(json.dumps([{'event': OWNER_EVENT_PREFIX + '.onPressed', 'actions': self.actions}]))
        for value in [OWNER_EVENT_PREFIX + '.onPressed',
                      json.dumps({'value': OWNER_EVENT_PREFIX + '.onPressed'}),
                      json.dumps({'event': OWNER_EVENT_PREFIX + '.onPressed garbage'})]:
            with self.assertRaises(ValueError): check_symbol_value(value)

    def test_native_remove_does_not_delete_explicit_custom_trigger(self):
        original = [{'event': 'Test.onPressed', 'actions': self.actions}]
        with self.assertRaisesRegex(ValueError, 'does not exist'):
            plan_event(self.markup(original), 'Test', '.onPressed', [], self.catalog, 'remove')

    def test_reject_unknown_event(self):
        with self.assertRaises(HmiEventValidationError) as caught:
            plan_event(self.markup([]), 'Test', 'Press', self.actions, self.catalog)
        self.assertEqual(['.onPressed', '.onReleased'], caught.exception.details['available_events'])
        self.assertEqual(['.onPressed'], caught.exception.details['recommended_events'])

    def test_reject_duplicate_handlers(self):
        item = {'event': 'Test.onPressed', 'actions': self.actions}
        with self.assertRaises(ValueError):
            plan_event(self.markup([item,item]), 'Test', '.onPressed', self.actions, self.catalog)

    def test_reject_bad_write_action(self):
        with self.assertRaises(ValueError):
            validate_actions([{'objectType':'WriteToSymbol','symbolExpression':'bad','value':{}}])

    def test_json_xml_roundtrip(self):
        events=[{'event':'Test.onPressed','actions':[{'objectType':'JavaScript','sourceLines':['if (a < b && c) { x = "中文"; }']}]}]
        self.assertEqual(read_events(self.markup(events),'Test')[1],events)

    def test_script_backed_events_are_not_destroyed(self):
        with self.assertRaises(ValueError):
            read_events('<div id="Test"><script data-tchmi-target-attribute="data-tchmi-trigger">[]</script></div>', 'Test')


if __name__ == '__main__':
    unittest.main()
