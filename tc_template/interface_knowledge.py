"""Curated public-interface evidence. Never execute text from documentation."""
from copy import deepcopy

_BASE = 'https://infosys.beckhoff.com/content/1033/'
_RECORDS = {
    ('tc2_system', 'MEMSET'): {
        'kind': 'memory', 'return_type': 'UDINT',
        'inputs': [('destAddr', 'PVOID'), ('fillByte', 'USINT'), ('n', 'UDINT')],
        'pointers': ['DESTADDR'], 'length': 'N',
        'url': _BASE + 'tcplclib_tc2_system/31042699.html'},
    ('tc2_system', 'MEMCPY'): {
        'kind': 'memory', 'return_type': 'UDINT',
        'inputs': [('destAddr', 'PVOID'), ('srcAddr', 'PVOID'), ('n', 'UDINT')],
        'pointers': ['DESTADDR', 'SRCADDR'], 'length': 'N',
        'url': _BASE + 'tcplclib_tc2_system/31041163.html'},
    ('tc2_system', 'MEMMOVE'): {
        'kind': 'memory', 'return_type': 'UDINT',
        'inputs': [('destAddr', 'PVOID'), ('srcAddr', 'PVOID'), ('n', 'UDINT')],
        'pointers': ['DESTADDR', 'SRCADDR'], 'length': 'N',
        'url': _BASE + 'tcplclib_tc2_system/31044235.html'},
    ('tc2_system', 'MEMCMP'): {
        'kind': 'memory', 'return_type': 'DINT',
        'inputs': [('pBuf1', 'PVOID'), ('pBuf2', 'PVOID'), ('n', 'UDINT')],
        'pointers': ['PBUF1', 'PBUF2'], 'length': 'N',
        'url': _BASE + 'tcplclib_tc2_system/31039627.html'},
    ('tc2_tcpip', 'T_HSOCKET'): {
        'kind': 'struct',
        'public_members': {'HANDLE':'UDINT','LOCALADDR':'ST_SOCKADDR','REMOTEADDR':'ST_SOCKADDR'},
        'url': _BASE + 'tf6310_tc3_tcpip/84182539.html',
        'usage': 'T_HSOCKET is a structure, not an integer. Do not compare or assign the whole handle to 0. Inspect its public handle field when appropriate; use FB completion/error state to track connections. Never invent bInit. Field values alone do not prove a live connection.'},
    ('tc2_tcpip', 'ST_SOCKADDR'): {
        'kind': 'struct', 'public_members': {'NPORT':'UDINT','SADDR':'STRING(15)'},
        'url': _BASE + 'tf6310_tc3_tcpip/84179467.html'},
    ('tc2_system', 'T_AMSNETID'): {
        'kind': 'alias', 'public_alias': 'STRING(23)',
        'url': _BASE + 'tcplclib_tc2_system/31059723.html'},
    ('tc2_system', 'T_IPV4ADDR'): {
        'kind': 'alias', 'public_alias': 'STRING(15)',
        'url': 'https://infosys.beckhoff.com/content/1031/tcplclib_tc2_system/31065867.html'},
    ('tc2_tcpip', 'FB_SOCKETSEND'): {
        'kind': 'buffer', 'pointer': 'PSRC', 'length': 'CBLEN',
        'url': _BASE + 'tf6310_tc3_tcpip/84149131.html',
        'usage': 'bExecute rising edge starts a send; wait for bBusy to clear and inspect bError. cbLen is bytes, not array elements.'},
    ('tc2_tcpip', 'FB_SOCKETRECEIVE'): {
        'kind': 'buffer', 'pointer': 'PDEST', 'length': 'CBLEN',
        'url': _BASE + 'tf6310_tc3_tcpip/84150667.html',
        'usage': 'cbLen is maximum bytes to receive; inspect nRecBytes and bError after completion. A receive request is not proof of received data.'},
}


def public_interface_knowledge(library, symbol):
    record = _RECORDS.get((str(library).casefold(), str(symbol).upper()))
    if not record:
        return None
    return {**deepcopy(record), 'source': 'Beckhoff InfoSys',
            'version_scope': 'public TwinCAT 3 library documentation, not exact-version ABI proof'}
