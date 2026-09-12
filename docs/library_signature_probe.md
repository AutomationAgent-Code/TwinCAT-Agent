# Live library signature probe — 2026-09-12

Read-only test against existing XAE PID 19584, TwinCAT Project2.sln,
PLC root `TIPC^Untitled1^Untitled1 Project`. No new IDE instance, save,
build, PLC source mutation, or runtime operation was performed.

## Working entry point

The installed `Components/Base/TypeLib/3.4/TCatSysManager.tlb` describes
`ITcPlcLibraryManager2.ProduceLibrarySignatures(pLibRef)`.
QueryInterface succeeded on the IEC project and its References node.
The References node also exposes it through IDispatch (DISPID 113), so the
existing late-bound x86 bridge can call it without a new COM dependency.

Enumerate `references.References`, retain the actual library reference object,
then call `references.ProduceLibrarySignatures(library)`.
Do not substitute a library filename, guessed ID, or newest installed version.

For actual reference Tc2_TcpIp the result was XML with version 3.4.4.0,
60 TypeSignature entries and 66,249 characters. A repeated warm call took
0.002 seconds (method only, not total bridge latency; no general speed promise).
FB_SocketConnect/Send/Receive/Close all included named Inputs and Outputs
with DataType and comments. Send.pSrc and Receive.pDest were POINTER TO BYTE.

## Limits and next integration

The tested T_HSOCKET Type entry only contains name/comment, not its fields
or complete underlying type. Do not fabricate a STRUCT or mark all library
semantics verified. Defaults, constants, inheritance and dependent type layouts
need separate completeness handling. Placeholder references were enumerated
but not tested for signature generation in this probe.

ITcLibraryManager and ITcLibraryManager2 exist in the typelib, but returned
E_NOINTERFACE on the three tested targets (system manager, IEC project,
References). This is not proof that no other service exposes them.
Official overview: https://infosys.beckhoff.com/content/1031/tc3_automationinterface/21840625419.html

`scripts/probe_library_interfaces.py` reproduces the narrowly scoped experiment.
The reader is now wired into Agent dependency preflight in the source workspace;
the installed backend has not been updated in this stage.
Production integration should bind PID/solution/reference version, parse a
bounded schema, cache signatures, preserve unknown type evidence, and test
invalid parameter names/directions/types without modifying a user PLC project.

## Integrated supported subset

`library-signatures` is a native-only, explicitly PID-bound internal command.
Resolution first checks exact project objects; only a complete empty search may
fall back to library signatures. Ambiguous project matches do not fall back.
Library data is cached only within the review (batch preflight shares its call
cache), not persisted across projects, processes, or versions. Incomplete library
enumeration and ambiguous library names remain unresolved. The parser currently
converts simple FB input/output/in-out declarations only; unsupported fields,
type layouts, functions and methods do not become invented declarations.
Library FB parameter types are not recursively expanded. Live Type entries may
be used as opaque nominal types: declaration and same-type assignment/passing
are allowed without inventing fields. Member access and unverified cross-type
conversion still require additional interface evidence. A symbol absent from
both the project and actual library type entries remains unresolved.

Live integrated reader check: Tc2_Standard 3.4.7.0 (13 supported FBs),
Tc2_System 3.10.2.0 (59), Tc3_Module 3.4.7.0 (2), Tc3_GlobalTypes 1.0.0.0 (0),
Tc2_TcpIp 3.4.4.0 (24). Placeholder references worked in this check.
This is interface evidence, not a compiler or runtime safety guarantee.

Regression of the user's blocked four-Socket-FB declaration: the same candidate
now passes read-only preflight against the current XAE; no PLC write was made.
The model contract requires official/knowledge-base usage guidance before calls.
For example, official Tc2_System documentation describes T_AmsNetID as STRING(23)
and T_IPv4Addr as STRING(15); these pages are evidence for a later focused alias
check, not permission to guess all named library types as strings:
https://infosys.beckhoff.com/content/1033/tcplclib_tc2_system/31059723.html
https://infosys.beckhoff.com/content/1031/tcplclib_tc2_system/31065867.html
