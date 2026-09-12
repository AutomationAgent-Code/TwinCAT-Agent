"""User-selected offline editing policy, independent of ADS Run/Stop."""


def check_source_offline(args, call):
    path = str(args.get('tree_path') or args.get('path') or '')
    runtime = str(args.get('runtime') or '')
    if not runtime and path.startswith('TIPC^') and len(path.split('^')) >= 3:
        runtime = path.split('^')[1]
    try:
        if runtime:
            names = [runtime]
        else:
            inventory = call('plc-runtimes')
            names = [p['name'] for p in inventory.get('plcs', [])]
            if not names:
                raise ValueError('PLC project scope is unavailable')
        states = [{'name':name, **call('plc-online-state', runtime=name)} for name in names]
        if all(s.get('logged_in') is False for s in states):
            return None
        online = [s['name'] for s in states if s.get('logged_in') is True]
        return {'states':states, 'online_runtimes':online}
    except Exception as exc:
        return {'error':str(exc), 'online_runtimes':[]}
