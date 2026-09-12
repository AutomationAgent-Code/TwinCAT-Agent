# Library function signature subset

The live signature reader now supports Function entries when exactly one
Outputs/Output matching the function name provides a supported return type.
It emits a FUNCTION declaration and excludes that return from ordinary output
arguments. Duplicate, missing or complex return signatures remain unsupported;
unknown layouts are not fabricated. Default argument semantics and general
method/inheritance resolution are not made complete by this extension.

Only recognized scalar-source conversions use the built-in conversion shortcut.
Names such as HSOCKET_TO_STRING must resolve their actual library interface,
not bypass argument checks merely because they end with TO_STRING.

Read-only validation against existing XAE found 87 supported function entries:
Tc2_System 56, Tc3_Module 25, Tc2_TcpIp 6. This is a local observation, not a
guarantee for other versions. The named hSocket argument of HSOCKET_TO_STRING
passed; a Wrong argument was rejected after resolving the real signature.
No source was written, saved or compiled. No extra XAE instance was opened.

Tests cover positional/named input, invalid argument names/types/directions,
return-type use, duplicate/unsupported return signatures and conversion-shaped
library names. Installed backend synchronization is a separate step.

## Extended type preservation

`interface_types.py` preserves simple named types, POINTER TO named types and
fixed positive numeric STRING/WSTRING capacities. The same grammar is used for
parameters and return values. Constant-expression lengths, references, arrays
and subranges are not admitted by this extension. A pointer result retains its
pointee; it is never reduced to the word POINTER or marked memory-safe.
Function/method return variables also preserve these supported full types.

Read-only current-XAE observation after this extension: 113 supported functions
(Tc2_Standard 18, Tc2_System 60, Tc3_Module 29, Tc2_TcpIp 6), including standard
string functions and several pointer-returning functions. No inherited methods
were present in the inspected XML schema; support is not inferred from that.
This inspection did not invoke any library function in the PLC runtime.
