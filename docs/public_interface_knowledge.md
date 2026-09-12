# Public interface knowledge (curated subset)

`tc_template/interface_knowledge.py` stores reviewed public API facts separately
from the parser. It is not a general web-page importer and never executes
instructions in retrieved documents. Each record has a source URL and an
explicit public-documentation version scope, not a claim of exact-version ABI.

Current coverage: Tc2_System T_AmsNetID/T_IPv4Addr aliases and Tc2_TcpIp
FB_SocketSend/Receive buffer conventions. Binding requires the actual library
name; buffer records additionally require the live pointer and cbLen input
types to match. Dependencies returned to the Agent include this usage evidence.
The caller still reads full official documentation for broader behavior.

Checks cover a literal byte count against a directly supplied ADR of a
one-dimensional BYTE array with literal bounds, including nonzero lower bounds.
Known oversize buffers are rejected. Dynamic sizes, named-constant bounds,
offset pointer arithmetic, retained inputs and memory lifetime are not proven.
They do not get a fabricated safe result; pointer_safety_verified remains false.

Explicit STRING(n) checks reject plain ASCII literal truncation, including the
documented aliases. IEC escape decoding, non-ASCII byte encoding and dynamic
string lengths are intentionally outside this exact check.

References:
- https://infosys.beckhoff.com/content/1033/tcplclib_tc2_system/31059723.html
- https://infosys.beckhoff.com/content/1031/tcplclib_tc2_system/31065867.html
- https://infosys.beckhoff.com/content/1033/tf6310_tc3_tcpip/84149131.html
- https://infosys.beckhoff.com/content/1033/tf6310_tc3_tcpip/84150667.html

Only software tests were run for this stage. No user PLC writes, saves, builds,
runtime changes, or installed-backend update are part of it.
