# Process ownership fixture prerequisites

- [x] Establish bounded semantic worker readiness through the existing output-reader thread and a queue before inducing an inspection monitor release failure. The monitor exclusively owns child reaping; fixture setup must not race it with a raw wait.
- [x] Establish an exited but unreaped actual child before injecting interruption into cleanup bookkeeping. Declare this retained-child test Linux/Darwin applicable and retain a direct POSIX skip guard. Retain signal receipt, pending ownership, no-repeat-signal and eventual disposal assertions.
- [x] Use a minimal system shell for kernel child-reservation snapshot tests so interpreter imports do not become an unrelated fixture prerequisite. Declare these kernel tests applicable to Linux and Darwin, retaining a direct POSIX skip guard. Preserve actual child identity and strict live, missing and foreign group rejection controls.

Native execution remains a separate acceptance gate. These changes do not alter production deadlines, ownership checks or retry behavior.
