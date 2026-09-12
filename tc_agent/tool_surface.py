"""Model-facing compatibility reduction, never an execution permission change."""
COMPATIBILITY_TOOLS = {
    'plc_list': ('plc_structure', 'Unbounded legacy object listing; use bounded structure or source catalog.'),
    'tc_hmi_delete_view': ('tc_hmi_item_delete', 'Unified native item deletion includes view/content protection.'),
    'tc_hmi_user_control_delete': ('tc_hmi_item_delete', 'Unified deletion includes paired UserControl files.'),
}


def model_selection(selected):
    # Do not expand a user's allocation or substitute write tools implicitly.
    return {name for name in selected if name not in COMPATIBILITY_TOOLS
            or COMPATIBILITY_TOOLS[name][0] not in selected}


def audit(registry):
    """Complete inventory; retained tools are not claimed to be interchangeable."""
    return [{'name': t['name'], 'category': t.get('category'),
             'readonly': t['readonly'],
             'disposition': 'compatibility' if t['name'] in COMPATIBILITY_TOOLS else 'retain',
             'replacement': COMPATIBILITY_TOOLS.get(t['name'], ('', ''))[0],
             'reason': COMPATIBILITY_TOOLS.get(t['name'], ('', 'Distinct contract retained'))[1]}
            for t in registry]
