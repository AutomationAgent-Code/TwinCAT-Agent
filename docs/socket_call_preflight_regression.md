# Socket call preflight regression (2026-09-12)

The latest XAE run successfully wrote declarations, then blocked implementation.
The remaining causes were missing public string aliases, unsupported byte-array
address compatibility, and genuine double FB calls. A call with named inputs
already executes the instance; a subsequent empty call is not required.

Changes:

- Live Tc2_System Type entries receive the two documented public aliases
  T_AmsNetID = STRING(23), T_IPv4Addr = STRING(15). Same-named project/custom
  library types are not automatically treated as these aliases.
- A POINTER TO ARRAY[...] OF BYTE can supply a POINTER TO BYTE buffer.
  This is not permission to pass INT arrays or proof of cbLen bounds, lifetime,
  successful transmission, or safe online changes.
- Batch preflight reuses write-gate fb-call-count/fb-call-zone rules and
  classifies their blocking findings consistently. This does not claim every
  style/delta/baseline policy is identical: actual writes still check live state.
- Model contracts distinguish input assignment from FB invocation, require
  edge-history updates after edge evaluation, and distinguish send requests
  from successful send completion. Documentation guidance prefers matching
  TwinCAT 3 libraries and full pages over mixed-version search snippets.

Evidence sources:
https://infosys.beckhoff.com/content/1033/tcplclib_tc2_system/31059723.html
https://infosys.beckhoff.com/content/1031/tcplclib_tc2_system/31065867.html
https://infosys.beckhoff.com/content/1033/tf6310_tc3_tcpip/84203915.html

The official example includes a different state-dependent call arrangement;
the once-per-cycle constraint here is the user's project policy, not a claim
that all other arrangements are forbidden TwinCAT syntax.

Read-only live regression used the failed candidate from run
30447231e42d4dd28f05212af9448ec5 against existing XAE PID 19584.
Alias/address findings disappeared; remaining findings were three double calls
and one unused close instance. In an in-memory test only, removing the redundant
empty calls and adding one close call made preflight pass. No user source was
written, saved, built, or deployed. This test is not a complete TCP client.
